"""Pure-offline backtest engine.

Iterates the union of bar timestamps across all symbols, asks the strategy
for signals using only data available up to ``t``, executes them via the
``SimulatedBroker``, and records the resulting equity curve + metrics.
"""
from __future__ import annotations

import csv
import json
import logging
import math
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Mapping, Optional

import numpy as np
import pandas as pd

from broker import SimulatedBroker
from config import Config
from data_feed import OfflineCSVDataFeed
from portfolio import Portfolio
from strategy import Strategy


log = logging.getLogger(__name__)

TIMEFRAME_TO_BARS_PER_YEAR: dict[str, int] = {
    "1m": 252 * 6 * 60,
    "5m": 252 * 6 * 12,
    "15m": 252 * 6 * 4,
    "1h": 252 * 6,
    "4h": 252 * 2,
    "1d": 252,
    "1w": 52,
}


@dataclass
class BacktestMetrics:
    start: str
    end: str
    starting_cash: float
    final_equity: float
    total_return_pct: float
    cagr_pct: float
    max_drawdown_pct: float
    sharpe: float
    num_trades: int
    win_rate_pct: float
    avg_r_multiple: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BacktestResult:
    metrics: BacktestMetrics
    equity_curve: pd.DataFrame
    fills_df: pd.DataFrame


def run_backtest(
    cfg: Config,
    strategy: Strategy,
    symbols: Optional[list[str]] = None,
    timeframe: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    warmup_bars: int = 200,
) -> BacktestResult:
    symbols = list(symbols or cfg.symbols)
    timeframe = timeframe or cfg.timeframe
    start_ts = pd.Timestamp(start or cfg.backtest_start)
    end_ts = pd.Timestamp(end or cfg.backtest_end)

    feed = OfflineCSVDataFeed(cfg.data_dir)
    broker = SimulatedBroker(cfg)
    portfolio = Portfolio(starting_cash=cfg.starting_cash)

    # Load full series once for each symbol (within window).
    series: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = feed.get_history(sym, timeframe, start_ts, end_ts)
        if df.empty:
            log.warning("No data for %s in window; skipping", sym)
            continue
        series[sym] = df
    if not series:
        raise RuntimeError("Backtest aborted: no data loaded for any symbol.")

    # Master timeline = sorted union of all bar timestamps.
    timeline = sorted(set().union(*(df.index for df in series.values())))

    for ts in timeline:
        # Snapshot bars up to ts for each symbol with data available.
        bars: dict[str, pd.DataFrame] = {}
        marks: dict[str, float] = {}
        for sym, df in series.items():
            window = df.loc[df.index <= ts]
            if window.empty:
                continue
            bars[sym] = window
            marks[sym] = float(window["close"].iloc[-1])

        # Skip until every active symbol has enough warmup bars.
        if any(len(b) < warmup_bars for b in bars.values()):
            portfolio.record_equity(ts, marks)
            continue

        signals = strategy.generate_signals(bars, portfolio.position_views(), cfg)
        if signals:
            broker.execute_signals(signals, marks, portfolio, ts)

        portfolio.record_equity(ts, marks)

    equity_df = portfolio.equity_curve_df()
    metrics = _compute_metrics(equity_df, portfolio, timeframe, str(start_ts.date()), str(end_ts.date()))
    fills_df = _fills_to_df(portfolio, marks_at_close=marks)
    return BacktestResult(metrics=metrics, equity_curve=equity_df, fills_df=fills_df)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def _compute_metrics(
    equity_df: pd.DataFrame,
    portfolio: Portfolio,
    timeframe: str,
    start: str,
    end: str,
) -> BacktestMetrics:
    if equity_df.empty:
        return BacktestMetrics(
            start=start, end=end,
            starting_cash=portfolio.starting_cash,
            final_equity=portfolio.starting_cash,
            total_return_pct=0.0, cagr_pct=0.0, max_drawdown_pct=0.0,
            sharpe=0.0, num_trades=0, win_rate_pct=0.0, avg_r_multiple=0.0,
        )

    equity = equity_df["equity"].astype(float)
    final_eq = float(equity.iloc[-1])
    start_eq = float(portfolio.starting_cash)
    total_return = (final_eq / start_eq) - 1.0 if start_eq > 0 else 0.0

    # Years elapsed from index span.
    span = equity.index[-1] - equity.index[0]
    years = max(span.days / 365.25, 1e-9)
    cagr = ((final_eq / start_eq) ** (1.0 / years) - 1.0) if start_eq > 0 else 0.0

    # Drawdown
    running_max = equity.cummax()
    drawdown = (equity - running_max) / running_max
    max_dd = float(drawdown.min()) if not drawdown.empty else 0.0

    # Sharpe (rf=0). Annualization based on bars/year for the timeframe.
    rets = equity.pct_change().dropna()
    bars_per_year = TIMEFRAME_TO_BARS_PER_YEAR.get(timeframe, 252)
    if len(rets) > 1 and rets.std(ddof=0) > 0:
        sharpe = float((rets.mean() / rets.std(ddof=0)) * math.sqrt(bars_per_year))
    else:
        sharpe = 0.0

    # Trade stats: pair each SELL with the prior cost basis at the time of fill.
    trades = _extract_round_trip_trades(portfolio)
    num_trades = len(trades)
    if num_trades:
        wins = sum(1 for t in trades if t["pnl"] > 0)
        win_rate = 100.0 * wins / num_trades
        # R multiple is unknowable without a stop; approximate as pnl / (entry * 1%).
        avg_r = float(np.mean([t["pnl"] / max(t["entry_price"] * 0.01, 1e-9) for t in trades]))
    else:
        win_rate = 0.0
        avg_r = 0.0

    return BacktestMetrics(
        start=start,
        end=end,
        starting_cash=start_eq,
        final_equity=final_eq,
        total_return_pct=total_return * 100.0,
        cagr_pct=cagr * 100.0,
        max_drawdown_pct=max_dd * 100.0,
        sharpe=sharpe,
        num_trades=num_trades,
        win_rate_pct=win_rate,
        avg_r_multiple=avg_r,
    )


def _extract_round_trip_trades(portfolio: Portfolio) -> list[dict]:
    """FIFO-pair BUY and SELL fills to estimate per-trade P&L."""
    trades: list[dict] = []
    open_lots: dict[str, list[tuple[float, float, pd.Timestamp]]] = {}
    for f in portfolio.fills:
        if f.side == "BUY":
            open_lots.setdefault(f.symbol, []).append((f.qty, f.price, f.timestamp))
        else:  # SELL
            remaining = f.qty
            lots = open_lots.get(f.symbol, [])
            while remaining > 1e-9 and lots:
                lot_qty, lot_price, lot_ts = lots[0]
                used = min(lot_qty, remaining)
                pnl = (f.price - lot_price) * used
                trades.append({
                    "symbol": f.symbol,
                    "entry_ts": lot_ts,
                    "exit_ts": f.timestamp,
                    "qty": used,
                    "entry_price": lot_price,
                    "exit_price": f.price,
                    "pnl": pnl,
                })
                if used >= lot_qty - 1e-9:
                    lots.pop(0)
                else:
                    lots[0] = (lot_qty - used, lot_price, lot_ts)
                remaining -= used
    return trades


def _fills_to_df(portfolio: Portfolio, marks_at_close: Mapping[str, float]) -> pd.DataFrame:
    if not portfolio.fills:
        return pd.DataFrame(columns=["timestamp", "symbol", "side", "qty", "price", "fees", "reason"])
    rows = [{
        "timestamp": f.timestamp, "symbol": f.symbol, "side": f.side,
        "qty": f.qty, "price": f.price, "fees": f.fees, "reason": f.reason,
    } for f in portfolio.fills]
    return pd.DataFrame(rows).set_index("timestamp").sort_index()


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
def save_results(result: BacktestResult, out_dir: Path, tag: str = "backtest") -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    eq_path = out_dir / f"{tag}_equity_curve.csv"
    metrics_path = out_dir / f"{tag}_metrics.json"
    fills_path = out_dir / f"{tag}_fills.csv"

    result.equity_curve.to_csv(eq_path)
    metrics_path.write_text(json.dumps(result.metrics.to_dict(), indent=2, default=str))
    result.fills_df.to_csv(fills_path)

    return {"equity_curve": eq_path, "metrics": metrics_path, "fills": fills_path}
