"""Unified data feed.

- Offline: load OHLCV CSV/Parquet from ``./data/{symbol}_{timeframe}.{csv|parquet}``.
- Online:  call user-provided MCP/HTTP connectors via clearly marked adapters.

Backtest and paper_offline_replay use ONLY the offline path. Online adapters
are opt-in stubs the user fills in.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

from config import Config


OHLCV_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class BarRequest:
    symbol: str
    timeframe: str
    limit: Optional[int] = None
    start: Optional[pd.Timestamp] = None
    end: Optional[pd.Timestamp] = None


class DataFeed(ABC):
    @abstractmethod
    def get_latest_bars(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 200,
        end: Optional[pd.Timestamp] = None,
    ) -> pd.DataFrame: ...

    @abstractmethod
    def get_history(
        self,
        symbol: str,
        timeframe: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.DataFrame: ...


# ---------------------------------------------------------------------------
# Offline
# ---------------------------------------------------------------------------
class OfflineCSVDataFeed(DataFeed):
    """Reads OHLCV from local files. CSV preferred, Parquet supported."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self._cache: dict[tuple[str, str], pd.DataFrame] = {}

    def _path(self, symbol: str, timeframe: str) -> Path:
        csv = self.data_dir / f"{symbol}_{timeframe}.csv"
        if csv.exists():
            return csv
        pq = self.data_dir / f"{symbol}_{timeframe}.parquet"
        if pq.exists():
            return pq
        raise FileNotFoundError(
            f"No local data for {symbol} {timeframe} in {self.data_dir} "
            f"(expected {csv.name} or {pq.name})"
        )

    def _load(self, symbol: str, timeframe: str) -> pd.DataFrame:
        key = (symbol, timeframe)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        path = self._path(symbol, timeframe)
        if path.suffix == ".csv":
            df = pd.read_csv(path)
        else:
            df = pd.read_parquet(path)
        df = _normalize_ohlcv(df)
        self._cache[key] = df
        return df

    def get_history(
        self,
        symbol: str,
        timeframe: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.DataFrame:
        df = self._load(symbol, timeframe)
        return df.loc[(df.index >= start) & (df.index <= end)].copy()

    def get_latest_bars(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 200,
        end: Optional[pd.Timestamp] = None,
    ) -> pd.DataFrame:
        df = self._load(symbol, timeframe)
        if end is not None:
            df = df.loc[df.index <= end]
        return df.tail(limit).copy()


def _normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [c.lower().strip() for c in df.columns]
    # find a timestamp column
    ts_col = next(
        (c for c in ("timestamp", "datetime", "date", "time") if c in df.columns),
        None,
    )
    if ts_col is None:
        raise ValueError(
            "OHLCV file must have one of: timestamp, datetime, date, time"
        )
    df[ts_col] = pd.to_datetime(df[ts_col], utc=False)
    df = df.set_index(ts_col).sort_index()
    df.index.name = "timestamp"
    missing = [c for c in OHLCV_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"OHLCV file missing columns: {missing}")
    return df[list(OHLCV_COLUMNS)].astype(float)


# ---------------------------------------------------------------------------
# Online (connector-backed) — adapter stubs
# ---------------------------------------------------------------------------
class ConnectorDataFeed(DataFeed):
    """Online feed backed by user-supplied MCP/HTTP connectors.

    The two ``*_via_connector`` methods are the ONLY place that should reach
    the network. Wire them up to your Alpaca / Bybit testnet / Public.com /
    MCP tools — but keep their signatures so the rest of the bot stays pure.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    # ---- Adapter methods (fill in with your MCP/HTTP connector) ----------
    def fetch_bars_via_connector(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 200,
        end: Optional[pd.Timestamp] = None,
    ) -> pd.DataFrame:
        """ADAPTER STUB. Replace with a real connector call.

        Must return a DataFrame indexed by timestamp with columns:
        open, high, low, close, volume.
        """
        raise NotImplementedError(
            "ConnectorDataFeed.fetch_bars_via_connector is not wired up. "
            "Implement it against your MCP/HTTP connector, or run in OFFLINE_MODE."
        )

    def fetch_history_via_connector(
        self,
        symbol: str,
        timeframe: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.DataFrame:
        """ADAPTER STUB. Replace with a real connector call."""
        raise NotImplementedError(
            "ConnectorDataFeed.fetch_history_via_connector is not wired up."
        )

    # ---- DataFeed interface ---------------------------------------------
    def get_latest_bars(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 200,
        end: Optional[pd.Timestamp] = None,
    ) -> pd.DataFrame:
        return self.fetch_bars_via_connector(symbol, timeframe, limit, end)

    def get_history(
        self,
        symbol: str,
        timeframe: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.DataFrame:
        return self.fetch_history_via_connector(symbol, timeframe, start, end)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def build_data_feed(cfg: Config) -> DataFeed:
    if cfg.OFFLINE_MODE:
        return OfflineCSVDataFeed(cfg.data_dir)
    return ConnectorDataFeed(cfg)
