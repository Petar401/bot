"""Strategies and the Signal API.

A Strategy is a pure function over (bars, positions, config) -> list[Signal].
No I/O, no broker access — keeps strategies trivial to backtest and unit-test.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Mapping, Optional

import pandas as pd

from config import Config


Action = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class Signal:
    action: Action
    symbol: str
    size_pct: float                       # fraction of equity, 0.0–1.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reason: str = ""


@dataclass
class PositionView:
    """Read-only snapshot of a position passed to strategies."""
    symbol: str
    qty: float
    avg_price: float


class Strategy(ABC):
    name: str = "Strategy"

    @abstractmethod
    def generate_signals(
        self,
        bars: Mapping[str, pd.DataFrame],
        positions: Mapping[str, PositionView],
        config: Config,
    ) -> list[Signal]: ...


# ---------------------------------------------------------------------------
# SMA crossover
# ---------------------------------------------------------------------------
class SMACrossoverStrategy(Strategy):
    """Long-only SMA crossover.

    BUY  when fast SMA crosses above slow SMA and we have no position.
    SELL when fast SMA crosses below slow SMA and we are long.
    """

    name = "sma_crossover"

    def __init__(self, fast: int = 20, slow: int = 50, size_pct: float = 0.10) -> None:
        if fast >= slow:
            raise ValueError("fast must be < slow")
        self.fast = fast
        self.slow = slow
        self.size_pct = size_pct

    def generate_signals(
        self,
        bars: Mapping[str, pd.DataFrame],
        positions: Mapping[str, PositionView],
        config: Config,
    ) -> list[Signal]:
        signals: list[Signal] = []
        for symbol, df in bars.items():
            if len(df) < self.slow + 2:
                continue
            close = df["close"]
            fast_now = close.rolling(self.fast).mean().iloc[-1]
            slow_now = close.rolling(self.slow).mean().iloc[-1]
            fast_prev = close.rolling(self.fast).mean().iloc[-2]
            slow_prev = close.rolling(self.slow).mean().iloc[-2]
            if any(pd.isna(x) for x in (fast_now, slow_now, fast_prev, slow_prev)):
                continue

            crossed_up = fast_prev <= slow_prev and fast_now > slow_now
            crossed_dn = fast_prev >= slow_prev and fast_now < slow_now
            qty = positions[symbol].qty if symbol in positions else 0.0

            if crossed_up and qty == 0:
                signals.append(
                    Signal(
                        action="BUY",
                        symbol=symbol,
                        size_pct=self.size_pct,
                        reason=f"SMA{self.fast} crossed above SMA{self.slow}",
                    )
                )
            elif crossed_dn and qty > 0:
                signals.append(
                    Signal(
                        action="SELL",
                        symbol=symbol,
                        size_pct=1.0,            # close the whole position
                        reason=f"SMA{self.fast} crossed below SMA{self.slow}",
                    )
                )
        return signals


def build_strategy(cfg: Config) -> Strategy:
    return SMACrossoverStrategy(fast=cfg.sma_fast, slow=cfg.sma_slow)
