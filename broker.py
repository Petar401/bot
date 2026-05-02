"""Broker layer.

- ``SimulatedBroker``  : offline fills against bar prices, persists state.
- ``ConnectorBroker``  : paper-only routing through Claude + your MCP server.

Hard safety rules (enforced in both brokers and at module load):
  * ``cfg.LIVE_TRADING`` MUST be False.
  * Live/mainnet endpoints in env or connector config abort startup.
  * Network calls refuse to run while ``cfg.OFFLINE_MODE=True``.
  * Risk + exposure limits checked before any order is sized down or rejected.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from typing import Any, Mapping, Optional

import pandas as pd

from config import Config, LiveTradingBlocked, assert_paper_only, require_online
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

    return _floor_qty(signal.symbol, qty)


# Crypto quote-currency suffixes that imply fractional sizing.
_FRACTIONAL_QUOTE_SUFFIXES: tuple[str, ...] = ("USDT", "USDC", "BUSD", "FDUSD", "DAI", "TUSD")


def _floor_qty(symbol: str, qty: float) -> float:
    """Round trade qty to a venue-realistic precision.

    - Crypto pairs (suffix in ``_FRACTIONAL_QUOTE_SUFFIXES``): 4 decimals.
    - Everything else (equities): whole shares.
    """
    if any(symbol.upper().endswith(s) for s in _FRACTIONAL_QUOTE_SUFFIXES):
        return float(int(qty * 10_000)) / 10_000
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
    """Routes paper orders through Claude + your MCP broker server.

    The two ``*_via_connector`` methods are the only network surface, and both
    refuse to run while ``cfg.OFFLINE_MODE=True``. The constructor also
    double-checks that nothing in env / connector config points at a live venue.
    """

    def __init__(self, cfg: Config) -> None:
        assert_paper_only(cfg)
        if cfg.OFFLINE_MODE:
            raise RuntimeError(
                "ConnectorBroker requires OFFLINE_MODE=False. Use SimulatedBroker instead."
            )
        self.cfg = cfg

    # ---- Adapter methods (Claude + MCP) ---------------------------------
    def submit_paper_order_via_connector(self, order: Order) -> Fill:
        """STUB. Submits the order via Claude with the broker MCP attached.

        MUST verify the destination is a paper/testnet endpoint before sending.
        Anything else should raise ``LiveTradingBlocked``.
        """
        if not self.cfg.OFFLINE_MODE:
            require_online(self.cfg, "submit_paper_order_via_connector")
            order_payload = {**asdict(order), "timestamp": str(order.timestamp)}
            prompt = (
                "Use the configured paper-broker MCP tool to submit the "
                "following PAPER order. Refuse if the connected venue is not "
                "paper/testnet. Reply with ONLY a JSON object containing the "
                "fill: {timestamp, symbol, side, qty, price, fees, reason}.\n\n"
                f"ORDER:\n{json.dumps(order_payload, default=str)}"
            )
            payload = _call_claude_with_mcp(
                cfg=self.cfg,
                prompt=prompt,
                mcp_url_key="broker_mcp_url",
                mcp_name_key="broker_mcp_name",
            )
            return _payload_to_fill(payload)
        raise NotImplementedError(
            "ConnectorBroker.submit_paper_order_via_connector is not wired up. "
            "Set OFFLINE_MODE=False and populate cfg.connector first."
        )

    def fetch_account_via_connector(self) -> dict:
        """STUB. Returns an account snapshot (cash, positions, equity)."""
        if not self.cfg.OFFLINE_MODE:
            require_online(self.cfg, "fetch_account_via_connector")
            payload = _call_claude_with_mcp(
                cfg=self.cfg,
                prompt=(
                    "Use the configured paper-broker MCP tool to fetch the "
                    "current account snapshot. Reply with ONLY a JSON object "
                    "with keys: cash, equity, positions (list of "
                    "{symbol, qty, avg_price})."
                ),
                mcp_url_key="broker_mcp_url",
                mcp_name_key="broker_mcp_name",
            )
            return payload if isinstance(payload, dict) else json.loads(payload)
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
        if self.cfg.OFFLINE_MODE:
            raise RuntimeError(
                "ConnectorBroker.execute_signals called in OFFLINE_MODE; "
                "use SimulatedBroker instead."
            )
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


# ---------------------------------------------------------------------------
# Claude + MCP stub. Mirrors data_feed._call_claude_with_mcp but kept local
# so neither module imports the other's internals.
# ---------------------------------------------------------------------------
def _call_claude_with_mcp(
    cfg: Config,
    prompt: str,
    mcp_url_key: str,
    mcp_name_key: str,
) -> Any:
    """STUB. Single point of contact with the Claude API. Refuses while offline.

    Real implementation (sketch):

        from anthropic import Anthropic
        client = Anthropic(api_key=cfg.connector["anthropic_api_key"])
        msg = client.messages.create(
            model=cfg.connector.get("claude_model", "claude-sonnet-4-6"),
            max_tokens=4096,
            mcp_servers=[{
                "type": "url",
                "url":  cfg.connector[mcp_url_key],
                "name": cfg.connector[mcp_name_key],
            }],
            messages=[{"role": "user", "content": prompt}],
        )
        return _extract_json_from_message(msg)
    """
    require_online(cfg, "_call_claude_with_mcp")
    raise NotImplementedError(
        "_call_claude_with_mcp is a stub. Implement against the Anthropic SDK "
        "with mcp_servers configured (see the docstring for the shape)."
    )


def _payload_to_fill(payload: Any) -> Fill:
    if isinstance(payload, str):
        payload = json.loads(payload)
    return Fill(
        timestamp=pd.Timestamp(payload["timestamp"]),
        symbol=payload["symbol"],
        side=payload["side"],
        qty=float(payload["qty"]),
        price=float(payload["price"]),
        fees=float(payload.get("fees", 0.0)),
        reason=payload.get("reason", ""),
    )


def build_broker(cfg: Config) -> Broker:
    if cfg.OFFLINE_MODE:
        return SimulatedBroker(cfg)
    return ConnectorBroker(cfg)
