"""Broker layer.

- ``SimulatedBroker``  : offline fills against bar prices, persists state.
- ``ConnectorBroker``  : paper-only routing through a user-supplied connector.

Hard safety rules (enforced in both brokers and at module load):
  * ``cfg.LIVE_TRADING`` MUST be False.
  * Live/mainnet endpoints in env or connector config abort startup.
  * Risk + exposure limits checked before any order is sized down or rejected.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping, Optional

import pandas as pd

from config import Config, LiveTradingBlocked, assert_paper_only
from portfolio import Fill, Portfolio
from strategy import Signal


log = logging.getLogger(__name__)


@dataclass
class Order:
    timestamp: pd.Timestamp
    symbol: str
    side: str            # "BUY" or "SELL"
    qty: float
    limit_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reason: str = ""


class Broker(ABC):
    @abstractmethod
    def execute_signals(
        self,
        signals: list[Signal],
        marks: Mapping[str, float],
        portfolio: Portfolio,
        ts: pd.Timestamp,
    ) -> list[Fill]: ...


# ---------------------------------------------------------------------------
# Risk sizing — shared by both brokers
# ---------------------------------------------------------------------------
def size_order(
    signal: Signal,
    price: float,
    portfolio: Portfolio,
    marks: Mapping[str, float],
    cfg: Config,
) -> float:
    """Translate a signal into a share quantity respecting all risk limits.

    Returns 0 if the order would violate constraints.
    """
    if price <= 0:
        return 0.0
    equity = portfolio.equity(marks)
    if equity <= 0:
        return 0.0

    if signal.action == "SELL":
        held = portfolio.positions.get(signal.symbol)
        if held is None or held.qty <= 0:
            return 0.0
        return held.qty * max(0.0, min(signal.size_pct, 1.0))

    # --- BUY sizing ---------------------------------------------------------
    requested_pct = max(0.0, min(signal.size_pct, cfg.risk.max_position_pct))

    # Per-trade risk cap only kicks in when we have a stop loss to size against.
    if signal.stop_loss is not None and signal.stop_loss < price:
        risk_per_share = price - signal.stop_loss
        max_dollars_at_risk = equity * cfg.risk.max_risk_per_trade_pct
        risk_capped_qty = max_dollars_at_risk / risk_per_share
    else:
        risk_capped_qty = float("inf")

    requested_qty = (equity * requested_pct) / price

    # Total exposure cap across portfolio
    current_exposure_value = portfolio.positions_value(marks)
    headroom_value = max(0.0, equity * cfg.risk.max_total_exposure_pct - current_exposure_value)
    headroom_qty = headroom_value / price

    qty = min(requested_qty, risk_capped_qty, headroom_qty)
    qty = max(qty, 0.0)

    # Cash check
    cash_qty = portfolio.cash / price
    qty = min(qty, cash_qty)

    # Floor to whole shares for equities. Easy to relax for fractional/crypto.
    return float(int(qty))


# ---------------------------------------------------------------------------
# Offline simulator
# ---------------------------------------------------------------------------
class SimulatedBroker(Broker):
    """Fills at the bar's close price by default, with optional flat fees."""

    def __init__(self, cfg: Config, fee_per_trade: float = 0.0, slippage_bps: float = 0.0) -> None:
        assert_paper_only(cfg)
        self.cfg = cfg
        self.fee_per_trade = fee_per_trade
        self.slippage_bps = slippage_bps

    def _apply_slippage(self, side: str, price: float) -> float:
        if self.slippage_bps == 0:
            return price
        adj = price * self.slippage_bps / 10_000.0
        return price + adj if side == "BUY" else price - adj

    def execute_signals(
        self,
        signals: list[Signal],
        marks: Mapping[str, float],
        portfolio: Portfolio,
        ts: pd.Timestamp,
    ) -> list[Fill]:
        fills: list[Fill] = []
        for sig in signals:
            mark = marks.get(sig.symbol)
            if mark is None or mark <= 0:
                log.warning("No mark for %s at %s; skipping", sig.symbol, ts)
                continue
            qty = size_order(sig, mark, portfolio, marks, self.cfg)
            if qty <= 0:
                log.info("Sized to 0 for %s %s @ %s; skipping", sig.action, sig.symbol, mark)
                continue
            fill_price = self._apply_slippage(sig.action, mark)
            fill = Fill(
                timestamp=ts,
                symbol=sig.symbol,
                side=sig.action,
                qty=qty,
                price=fill_price,
                fees=self.fee_per_trade,
                reason=sig.reason,
            )
            portfolio.apply_fill(fill)
            fills.append(fill)
        return fills


# ---------------------------------------------------------------------------
# Connector-backed broker (paper API). Adapter stubs only.
# ---------------------------------------------------------------------------
class ConnectorBroker(Broker):
    """Routes orders through a user-supplied paper API.

    The two ``*_via_connector`` methods are the only network surface. The
    constructor double-checks that nothing in env / connector config points
    at a live venue.
    """

    def __init__(self, cfg: Config) -> None:
        assert_paper_only(cfg)
        if cfg.OFFLINE_MODE:
            raise RuntimeError(
                "ConnectorBroker requires OFFLINE_MODE=False. Use SimulatedBroker instead."
            )
        self.cfg = cfg

    # ---- Adapter methods -------------------------------------------------
    def submit_paper_order_via_connector(self, order: Order) -> Fill:
        """ADAPTER STUB. Wire up to your paper broker / testnet here.

        MUST verify the destination is a paper/testnet endpoint before
        sending. Anything else should raise ``LiveTradingBlocked``.
        """
        raise NotImplementedError(
            "ConnectorBroker.submit_paper_order_via_connector is not wired up. "
            "Implement against your paper API (e.g. Alpaca paper, Bybit testnet)."
        )

    def fetch_account_via_connector(self) -> dict:
        """ADAPTER STUB. Return account snapshot (cash, positions, equity)."""
        raise NotImplementedError(
            "ConnectorBroker.fetch_account_via_connector is not wired up."
        )

    # ---- Broker interface -----------------------------------------------
    def execute_signals(
        self,
        signals: list[Signal],
        marks: Mapping[str, float],
        portfolio: Portfolio,
        ts: pd.Timestamp,
    ) -> list[Fill]:
        if self.cfg.LIVE_TRADING:
            raise LiveTradingBlocked("LIVE_TRADING=True; refusing to send orders.")
        fills: list[Fill] = []
        for sig in signals:
            mark = marks.get(sig.symbol)
            if mark is None or mark <= 0:
                continue
            qty = size_order(sig, mark, portfolio, marks, self.cfg)
            if qty <= 0:
                continue
            order = Order(
                timestamp=ts,
                symbol=sig.symbol,
                side=sig.action,
                qty=qty,
                stop_loss=sig.stop_loss,
                take_profit=sig.take_profit,
                reason=sig.reason,
            )
            fill = self.submit_paper_order_via_connector(order)
            portfolio.apply_fill(fill)
            fills.append(fill)
        return fills


def build_broker(cfg: Config) -> Broker:
    if cfg.OFFLINE_MODE:
        return SimulatedBroker(cfg)
    return ConnectorBroker(cfg)
