"""Unified data feed.

- Offline: load OHLCV CSV/Parquet from ``./data/{symbol}_{timeframe}.{csv|parquet}``.
- Online:  call user-provided Claude connectors / MCP servers via clearly
           marked adapter methods. No raw HTTP — everything goes through Claude
           with ``mcp_servers`` configured, so the user controls the toolset.

Backtest and paper_offline_replay use ONLY the offline path. Online adapters
short-circuit with ``OfflineModeViolation`` whenever ``cfg.OFFLINE_MODE`` is
True, so the code is safe to run with Wi-Fi disabled.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from config import Config, require_online


log = logging.getLogger(__name__)


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
    """Online feed backed by Claude + your MCP market-data server.

    All adapter methods refuse to run while ``cfg.OFFLINE_MODE=True``. The
    actual network call goes through the Anthropic SDK with ``mcp_servers``
    configured, so the user's Claude connectors do the I/O — not raw HTTP.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    # ---- Adapter methods (Claude + MCP) ---------------------------------
    def fetch_bars_via_connector(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 200,
        end: Optional[pd.Timestamp] = None,
    ) -> pd.DataFrame:
        """STUB. Calls Claude with the user's market-data MCP server attached.

        Must return a DataFrame indexed by timestamp with columns:
        ``open, high, low, close, volume``.
        """
        if not self.cfg.OFFLINE_MODE:
            require_online(self.cfg, "fetch_bars_via_connector")
            prompt = (
                f"Use the configured market-data MCP tool to fetch the last "
                f"{limit} {timeframe} OHLCV bars for {symbol}"
                + (f" ending at {end.isoformat()}" if end is not None else "")
                + ". Reply with ONLY a JSON array of objects with keys "
                "timestamp, open, high, low, close, volume."
            )
            payload = _call_claude_with_mcp(
                cfg=self.cfg,
                prompt=prompt,
                mcp_url_key="market_data_mcp_url",
                mcp_name_key="market_data_mcp_name",
            )
            return _rows_to_ohlcv_df(payload)
        raise NotImplementedError(
            "ConnectorDataFeed.fetch_bars_via_connector is not wired up. "
            "Set OFFLINE_MODE=False, populate cfg.connector with your Claude+MCP "
            "config, and finish the stub above."
        )

    def fetch_history_via_connector(
        self,
        symbol: str,
        timeframe: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.DataFrame:
        """STUB. Same shape as fetch_bars_via_connector but for a date range."""
        if not self.cfg.OFFLINE_MODE:
            require_online(self.cfg, "fetch_history_via_connector")
            prompt = (
                f"Use the configured market-data MCP tool to fetch {timeframe} "
                f"OHLCV bars for {symbol} from {start.isoformat()} to "
                f"{end.isoformat()}. Reply with ONLY a JSON array of objects "
                "with keys timestamp, open, high, low, close, volume."
            )
            payload = _call_claude_with_mcp(
                cfg=self.cfg,
                prompt=prompt,
                mcp_url_key="market_data_mcp_url",
                mcp_name_key="market_data_mcp_name",
            )
            return _rows_to_ohlcv_df(payload)
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
        if self.cfg.OFFLINE_MODE:
            raise RuntimeError(
                "ConnectorDataFeed.get_latest_bars called in OFFLINE_MODE; "
                "use OfflineCSVDataFeed instead."
            )
        return self.fetch_bars_via_connector(symbol, timeframe, limit, end)

    def get_history(
        self,
        symbol: str,
        timeframe: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.DataFrame:
        if self.cfg.OFFLINE_MODE:
            raise RuntimeError(
                "ConnectorDataFeed.get_history called in OFFLINE_MODE; "
                "use OfflineCSVDataFeed instead."
            )
        return self.fetch_history_via_connector(symbol, timeframe, start, end)


# ---------------------------------------------------------------------------
# Claude + MCP stub (shared with broker.py)
# ---------------------------------------------------------------------------
def _call_claude_with_mcp(
    cfg: Config,
    prompt: str,
    mcp_url_key: str,
    mcp_name_key: str,
) -> Any:
    """STUB. Single point of contact with the Claude API.

    The real call should look roughly like:

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

    Kept as a NotImplementedError so a misconfigured run fails loudly instead
    of silently sending traffic. Wire it up once your MCP servers are ready.
    """
    require_online(cfg, "_call_claude_with_mcp")
    raise NotImplementedError(
        "_call_claude_with_mcp is a stub. Implement against the Anthropic SDK "
        "with mcp_servers configured (see the docstring for the shape)."
    )


def _rows_to_ohlcv_df(payload: Any) -> pd.DataFrame:
    """Parse a JSON-array OHLCV payload returned by the Claude+MCP call."""
    if isinstance(payload, str):
        payload = json.loads(payload)
    df = pd.DataFrame(payload)
    return _normalize_ohlcv(df)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def build_data_feed(cfg: Config) -> DataFeed:
    if cfg.OFFLINE_MODE:
        return OfflineCSVDataFeed(cfg.data_dir)
    return ConnectorDataFeed(cfg)
