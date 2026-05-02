"""Portfolio state: cash, positions, equity curve, P&L, drawdown.

Pure in-memory bookkeeping with optional JSON persistence. Used by both the
offline simulator and (eventually) the connector-backed broker — the broker
adapter just needs to translate connector account snapshots into these types.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Mapping, Optional

import pandas as pd

from strategy import PositionView


@dataclass
class Position:
    symbol: str
    qty: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0

    def view(self) -> PositionView:
        return PositionView(symbol=self.symbol, qty=self.qty, avg_price=self.avg_price)


@dataclass
class Fill:
    timestamp: pd.Timestamp
    symbol: str
    side: str                # "BUY" or "SELL"
    qty: float
    price: float
    fees: float = 0.0
    reason: str = ""


@dataclass
class EquityPoint:
    timestamp: pd.Timestamp
    cash: float
    positions_value: float
    equity: float


class Portfolio:
    def __init__(self, starting_cash: float) -> None:
        self.starting_cash: float = starting_cash
        self.cash: float = starting_cash
        self.positions: dict[str, Position] = {}
        self.fills: list[Fill] = []
        self.equity_curve: list[EquityPoint] = []

    # ---- Mutation -------------------------------------------------------
    def apply_fill(self, fill: Fill) -> None:
        pos = self.positions.setdefault(fill.symbol, Position(symbol=fill.symbol))
        if fill.side == "BUY":
            new_qty = pos.qty + fill.qty
            if new_qty <= 0:
                raise ValueError("BUY fill produced non-positive qty")
            # Weighted average cost basis
            pos.avg_price = (
                (pos.avg_price * pos.qty) + (fill.price * fill.qty)
            ) / new_qty
            pos.qty = new_qty
            self.cash -= fill.qty * fill.price + fill.fees
        elif fill.side == "SELL":
            if fill.qty > pos.qty + 1e-9:
                raise ValueError(
                    f"SELL qty {fill.qty} exceeds position {pos.qty} for {fill.symbol}"
                )
            pos.realized_pnl += (fill.price - pos.avg_price) * fill.qty
            pos.qty -= fill.qty
            self.cash += fill.qty * fill.price - fill.fees
            if pos.qty <= 1e-9:
                pos.qty = 0.0
                pos.avg_price = 0.0
        else:
            raise ValueError(f"Unknown side: {fill.side}")
        self.fills.append(fill)

    # ---- Valuation ------------------------------------------------------
    def positions_value(self, marks: Mapping[str, float]) -> float:
        return sum(
            pos.qty * marks.get(sym, pos.avg_price)
            for sym, pos in self.positions.items()
            if pos.qty > 0
        )

    def equity(self, marks: Mapping[str, float]) -> float:
        return self.cash + self.positions_value(marks)

    def gross_exposure(self, marks: Mapping[str, float]) -> float:
        eq = self.equity(marks)
        if eq <= 0:
            return float("inf")
        return self.positions_value(marks) / eq

    def unrealized_pnl(self, marks: Mapping[str, float]) -> float:
        total = 0.0
        for sym, pos in self.positions.items():
            if pos.qty > 0:
                total += (marks.get(sym, pos.avg_price) - pos.avg_price) * pos.qty
        return total

    def realized_pnl(self) -> float:
        return sum(p.realized_pnl for p in self.positions.values())

    def record_equity(self, ts: pd.Timestamp, marks: Mapping[str, float]) -> None:
        pv = self.positions_value(marks)
        self.equity_curve.append(
            EquityPoint(timestamp=ts, cash=self.cash, positions_value=pv, equity=self.cash + pv)
        )

    # ---- Views for strategy --------------------------------------------
    def position_views(self) -> dict[str, PositionView]:
        return {sym: p.view() for sym, p in self.positions.items()}

    # ---- Persistence ----------------------------------------------------
    def equity_curve_df(self) -> pd.DataFrame:
        if not self.equity_curve:
            return pd.DataFrame(columns=["timestamp", "cash", "positions_value", "equity"]).set_index("timestamp")
        df = pd.DataFrame([asdict(p) for p in self.equity_curve])
        return df.set_index("timestamp").sort_index()

    def to_state_dict(self) -> dict:
        return {
            "starting_cash": self.starting_cash,
            "cash": self.cash,
            "positions": {s: asdict(p) for s, p in self.positions.items()},
            "fills": [
                {**asdict(f), "timestamp": f.timestamp.isoformat()} for f in self.fills
            ],
        }

    def save_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_state_dict(), indent=2, default=str))

    @classmethod
    def load_json(cls, path: Path) -> "Portfolio":
        state = json.loads(path.read_text())
        port = cls(starting_cash=state["starting_cash"])
        port.cash = state["cash"]
        port.positions = {
            s: Position(**{k: v for k, v in p.items() if k in Position.__annotations__})
            for s, p in state.get("positions", {}).items()
        }
        port.fills = [
            Fill(
                timestamp=pd.Timestamp(f["timestamp"]),
                symbol=f["symbol"],
                side=f["side"],
                qty=f["qty"],
                price=f["price"],
                fees=f.get("fees", 0.0),
                reason=f.get("reason", ""),
            )
            for f in state.get("fills", [])
        ]
        return port
