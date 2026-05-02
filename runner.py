"""CLI entry point.

Modes:
  backtest             — full offline backtest on local data (no network).
  paper_offline_replay — replay local data 'as if live' (no network).
  paper_live           — paper trading via your connectors (NEEDS adapters wired in).

Examples:
  python runner.py backtest --symbols AAPL,MSFT --timeframe 1d \
      --start 2022-01-01 --end 2024-12-31 --cash 100000

  python runner.py paper_offline_replay --symbols AAPL --timeframe 1d --speed 0.0
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import pandas as pd

from backtest import run_backtest, save_results
from broker import SimulatedBroker, build_broker
from config import Config, LiveTradingBlocked, load_config
from data_feed import OfflineCSVDataFeed, build_data_feed
from portfolio import Portfolio
from strategy import build_strategy


def _setup_logging(logs_dir: Path) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "runner.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ---------------------------------------------------------------------------
# Decision logger — every signal/order/fill is appended as a JSON line.
# ---------------------------------------------------------------------------
class DecisionLogger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fp = path.open("a", buffering=1)

    def log(self, event: dict) -> None:
        self._fp.write(json.dumps(event, default=str) + "\n")

    def close(self) -> None:
        try:
            self._fp.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------
def cmd_backtest(args: argparse.Namespace, cfg: Config) -> int:
    strategy = build_strategy(cfg)
    result = run_backtest(
        cfg=cfg,
        strategy=strategy,
        symbols=args.symbols,
        timeframe=args.timeframe,
        start=args.start,
        end=args.end,
        warmup_bars=max(cfg.sma_slow + 2, args.warmup),
    )
    paths = save_results(result, cfg.reports_dir, tag=args.tag or "backtest")
    print(json.dumps({
        "metrics": result.metrics.to_dict(),
        "artifacts": {k: str(v) for k, v in paths.items()},
    }, indent=2, default=str))
    return 0


def cmd_paper_offline_replay(args: argparse.Namespace, cfg: Config) -> int:
    """Replay local data as if it were arriving live, in accelerated time."""
    if not cfg.OFFLINE_MODE:
        raise RuntimeError("paper_offline_replay requires OFFLINE_MODE=True")

    log = logging.getLogger("replay")
    feed = OfflineCSVDataFeed(cfg.data_dir)
    broker = SimulatedBroker(cfg)
    portfolio = Portfolio(starting_cash=cfg.starting_cash)
    strategy = build_strategy(cfg)

    symbols = list(args.symbols or cfg.symbols)
    timeframe = args.timeframe or cfg.timeframe
    start_ts = pd.Timestamp(args.start or cfg.backtest_start)
    end_ts = pd.Timestamp(args.end or cfg.backtest_end)
    speed = args.speed if args.speed is not None else cfg.replay_speed_seconds

    series = {s: feed.get_history(s, timeframe, start_ts, end_ts) for s in symbols}
    series = {s: df for s, df in series.items() if not df.empty}
    if not series:
        log.error("No data loaded; aborting replay")
        return 2

    timeline = sorted(set().union(*(df.index for df in series.values())))
    decisions = DecisionLogger(cfg.logs_dir / "decisions.jsonl")

    try:
        for ts in timeline:
            bars: dict[str, pd.DataFrame] = {}
            marks: dict[str, float] = {}
            for sym, df in series.items():
                window = df.loc[df.index <= ts]
                if window.empty:
                    continue
                bars[sym] = window
                marks[sym] = float(window["close"].iloc[-1])

            decisions.log({
                "ts": ts, "event": "snapshot", "marks": marks,
                "cash": portfolio.cash, "equity": portfolio.equity(marks),
            })

            if any(len(b) < cfg.sma_slow + 2 for b in bars.values()):
                portfolio.record_equity(ts, marks)
                if speed > 0:
                    time.sleep(speed)
                continue

            signals = strategy.generate_signals(bars, portfolio.position_views(), cfg)
            for sig in signals:
                decisions.log({"ts": ts, "event": "signal", **asdict(sig)})
            fills = broker.execute_signals(signals, marks, portfolio, ts)
            for fl in fills:
                decisions.log({"ts": ts, "event": "fill", **{
                    "symbol": fl.symbol, "side": fl.side, "qty": fl.qty,
                    "price": fl.price, "fees": fl.fees, "reason": fl.reason,
                }})

            portfolio.record_equity(ts, marks)
            if speed > 0:
                time.sleep(speed)
    finally:
        decisions.close()

    eq_path = cfg.reports_dir / "replay_equity_curve.csv"
    state_path = cfg.logs_dir / "replay_portfolio.json"
    portfolio.equity_curve_df().to_csv(eq_path)
    portfolio.save_json(state_path)
    print(json.dumps({
        "final_equity": portfolio.equity({s: float(df['close'].iloc[-1]) for s, df in series.items()}),
        "fills": len(portfolio.fills),
        "equity_curve": str(eq_path),
        "portfolio_state": str(state_path),
    }, indent=2, default=str))
    return 0


def cmd_paper_live(args: argparse.Namespace, cfg: Config) -> int:
    """Live paper trading via connectors. Adapters MUST be wired up first."""
    if cfg.OFFLINE_MODE:
        raise RuntimeError(
            "paper_live requires OFFLINE_MODE=False. Set it explicitly when you "
            "have connectors wired up; this bot is paper-only regardless."
        )
    feed = build_data_feed(cfg)
    broker = build_broker(cfg)
    portfolio = Portfolio(starting_cash=cfg.starting_cash)
    strategy = build_strategy(cfg)

    log = logging.getLogger("paper_live")
    log.info(
        "Starting paper_live loop. symbols=%s timeframe=%s",
        args.symbols or cfg.symbols, args.timeframe or cfg.timeframe,
    )
    log.warning(
        "paper_live is a scaffolding loop. Implement "
        "ConnectorDataFeed.fetch_bars_via_connector and "
        "ConnectorBroker.submit_paper_order_via_connector before running."
    )

    # Single tick to fail fast if adapters aren't wired up — keeps this mode
    # honest without pretending to do live work.
    symbols = list(args.symbols or cfg.symbols)
    timeframe = args.timeframe or cfg.timeframe
    bars: dict[str, pd.DataFrame] = {}
    marks: dict[str, float] = {}
    for sym in symbols:
        df = feed.get_latest_bars(sym, timeframe, limit=cfg.sma_slow + 5)
        bars[sym] = df
        marks[sym] = float(df["close"].iloc[-1])

    signals = strategy.generate_signals(bars, portfolio.position_views(), cfg)
    fills = broker.execute_signals(signals, marks, portfolio, pd.Timestamp.utcnow())
    print(json.dumps({"signals": len(signals), "fills": len(fills)}, default=str))
    return 0


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------
def _parse_symbols(s: Optional[str]) -> Optional[list[str]]:
    if not s:
        return None
    return [tok.strip().upper() for tok in s.split(",") if tok.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bot", description="Paper-only modular trading bot.")
    sub = p.add_subparsers(dest="mode", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--symbols", type=_parse_symbols, default=None,
                        help="Comma-separated, e.g. AAPL,MSFT")
    common.add_argument("--timeframe", type=str, default=None)
    common.add_argument("--start", type=str, default=None)
    common.add_argument("--end", type=str, default=None)
    common.add_argument("--cash", type=float, default=None,
                        help="Override starting cash.")

    bt = sub.add_parser("backtest", parents=[common], help="Offline backtest.")
    bt.add_argument("--warmup", type=int, default=0)
    bt.add_argument("--tag", type=str, default=None)

    rp = sub.add_parser("paper_offline_replay", parents=[common],
                        help="Replay local data 'as if live'.")
    rp.add_argument("--speed", type=float, default=None,
                    help="Seconds to sleep between bars (0 = max speed).")

    sub.add_parser("paper_live", parents=[common],
                   help="Paper trading via connectors (requires adapters).")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except LiveTradingBlocked as e:
        print(f"ABORT: {e}", file=sys.stderr)
        return 3

    if args.cash is not None:
        cfg.starting_cash = args.cash

    _setup_logging(cfg.logs_dir)
    log = logging.getLogger("runner")
    log.info("mode=%s offline=%s symbols=%s timeframe=%s",
             args.mode, cfg.OFFLINE_MODE, args.symbols or cfg.symbols, args.timeframe or cfg.timeframe)

    handlers = {
        "backtest": cmd_backtest,
        "paper_offline_replay": cmd_paper_offline_replay,
        "paper_live": cmd_paper_live,
    }
    return handlers[args.mode](args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
