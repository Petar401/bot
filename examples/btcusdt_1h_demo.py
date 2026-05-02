"""End-to-end example: read ./data/BTCUSDT_1h.csv, run a backtest.

If the CSV is missing this script first generates a SYNTHETIC one so the
example is runnable out of the box. Replace it with real data whenever
you have it — the rest of the script is unchanged.

Run:
    python examples/btcusdt_1h_demo.py

This is equivalent to:
    python runner.py backtest --symbols BTCUSDT --timeframe 1h \
        --start 2023-01-01 --end 2024-01-01 --cash 10000 --tag btcusdt_1h_demo

Network: zero. Backtest mode never touches a connector.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Make the project root importable when running from anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data_feed import OfflineCSVDataFeed  # noqa: E402
import runner  # noqa: E402


CSV_PATH = ROOT / "data" / "BTCUSDT_1h.csv"


def _generate_synthetic_btcusdt_1h(path: Path, n_hours: int = 24 * 365) -> None:
    """Geometric-Brownian-style synthetic BTC/USDT 1h OHLCV. SYNTHETIC ONLY."""
    print(f"[demo] {path.name} not found — generating SYNTHETIC data ({n_hours} bars).")
    rng = np.random.default_rng(20240101)
    dates = pd.date_range("2023-01-01", periods=n_hours, freq="1h")
    # ~30% annual drift, ~60% annualized vol, scaled to hourly.
    mu_h = 0.30 / (24 * 365)
    sig_h = 0.60 / np.sqrt(24 * 365)
    rets = rng.normal(mu_h, sig_h, n_hours)
    close = 30_000 * np.exp(np.cumsum(rets))
    high = close * (1 + rng.uniform(0, 0.004, n_hours))
    low = close * (1 - rng.uniform(0, 0.004, n_hours))
    open_ = np.concatenate([[close[0]], close[:-1]])
    vol = rng.uniform(50, 500, n_hours)
    df = pd.DataFrame({
        "timestamp": dates, "open": open_, "high": high,
        "low": low, "close": close, "volume": vol,
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def main() -> int:
    if not CSV_PATH.exists():
        _generate_synthetic_btcusdt_1h(CSV_PATH)

    # Demonstrate the offline feed reading the CSV directly.
    feed = OfflineCSVDataFeed(CSV_PATH.parent)
    bars = feed.get_latest_bars("BTCUSDT", "1h", limit=5)
    print(f"[demo] last 5 bars from {CSV_PATH.name}:")
    print(bars)

    # Run the backtest through the same CLI entrypoint a user would call.
    argv = [
        "backtest",
        "--symbols", "BTCUSDT",
        "--timeframe", "1h",
        "--start", "2023-01-01",
        "--end",   "2024-01-01",
        "--cash",  "10000",
        "--tag",   "btcusdt_1h_demo",
    ]
    print(f"[demo] runner.py {' '.join(argv)}")
    return runner.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
