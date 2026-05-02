"""Central configuration for the trading bot.

Safety-first defaults: OFFLINE_MODE = True, LIVE_TRADING = False.
Anything that smells like a live/mainnet endpoint must be rejected at startup.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


PROJECT_ROOT: Path = Path(__file__).resolve().parent
DATA_DIR: Path = PROJECT_ROOT / "data"
LOGS_DIR: Path = PROJECT_ROOT / "logs"
REPORTS_DIR: Path = PROJECT_ROOT / "reports"


# Patterns that suggest a live/mainnet endpoint. Anything matching these in any
# *_URL / *_ENDPOINT env var or config string aborts startup.
_LIVE_ENDPOINT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"api\.alpaca\.markets", re.I),                # Alpaca live
    re.compile(r"api-fxtrade\.oanda\.com", re.I),             # OANDA live (fxpractice is paper)
    re.compile(r"api\.bybit\.com($|/)", re.I),                # Bybit mainnet
    re.compile(r"api\.binance\.com", re.I),                   # Binance mainnet
    re.compile(r"api\.kraken\.com", re.I),                    # Kraken live
    re.compile(r"\bmainnet\b", re.I),
    re.compile(r"\blive[-_ ]?api\b", re.I),
)


@dataclass
class RiskLimits:
    max_risk_per_trade_pct: float = 0.01      # 1% of equity at risk per trade
    max_total_exposure_pct: float = 0.50      # 50% of equity across all positions
    max_position_pct: float = 0.20            # cap per single symbol


# Equity tickers, kept as an opt-in alternative universe.
TOP_EQUITIES: tuple[str, ...] = ("AAPL", "MSFT", "SPY")

# Top-20 crypto pairs quoted in USDT (rough market-cap order; adjust to taste).
# Symbols use the exchange-standard concatenated form (e.g. BTCUSDT) so they
# map directly to ``./data/{SYMBOL}_{TIMEFRAME}.csv`` and to most exchange
# REST/WebSocket APIs (Binance, Bybit testnet, etc.).
TOP_CRYPTO_USDT_PAIRS: tuple[str, ...] = (
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "TRXUSDT", "LINKUSDT",
    "DOTUSDT", "MATICUSDT", "TONUSDT", "SHIBUSDT", "LTCUSDT",
    "BCHUSDT", "NEARUSDT", "UNIUSDT", "XLMUSDT", "ATOMUSDT",
)


@dataclass
class Config:
    # --- Mode flags ---------------------------------------------------------
    OFFLINE_MODE: bool = True
    LIVE_TRADING: bool = False                # MUST stay False; tripwire only

    # --- Universe -----------------------------------------------------------
    # Default to the top-20 crypto/USDT pairs. Override via CLI --symbols
    # or by setting cfg.symbols = TOP_EQUITIES for the stock universe.
    symbols: tuple[str, ...] = TOP_CRYPTO_USDT_PAIRS
    timeframe: str = "1d"                     # used for CSV filename suffix
    base_currency: str = "USDT"

    # --- Capital + risk -----------------------------------------------------
    starting_cash: float = 100_000.0
    risk: RiskLimits = field(default_factory=RiskLimits)

    # --- Strategy parameters -----------------------------------------------
    sma_fast: int = 20
    sma_slow: int = 50

    # --- Backtest defaults --------------------------------------------------
    backtest_start: str = "2020-01-01"
    backtest_end: str = "2024-12-31"

    # --- Replay (paper_offline_replay) -------------------------------------
    replay_speed_seconds: float = 0.0         # 0 = no sleep between bars

    # --- Paths --------------------------------------------------------------
    data_dir: Path = field(default_factory=lambda: DATA_DIR)
    logs_dir: Path = field(default_factory=lambda: LOGS_DIR)
    reports_dir: Path = field(default_factory=lambda: REPORTS_DIR)

    # --- Connector adapter config (filled in by the user) ------------------
    # Adapter methods in data_feed / broker read from this dict. None of the
    # offline paths touch them. Recommended keys when wiring up Claude MCP:
    #   "anthropic_api_key"         : sk-ant-...
    #   "claude_model"              : e.g. "claude-sonnet-4-6"
    #   "market_data_mcp_url"       : URL of your market-data MCP server
    #   "market_data_mcp_name"      : friendly name for that MCP server
    #   "broker_mcp_url"            : URL of your paper-broker MCP server
    #   "broker_mcp_name"           : friendly name for that MCP server
    connector: dict[str, str] = field(default_factory=dict)

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.logs_dir, self.reports_dir):
            p.mkdir(parents=True, exist_ok=True)


class LiveTradingBlocked(RuntimeError):
    """Raised when a live/mainnet code path is reached."""


class OfflineModeViolation(RuntimeError):
    """Raised when a network path is invoked while ``OFFLINE_MODE=True``."""


def require_online(cfg: "Config", what: str) -> None:
    """Refuse to make a network call while ``OFFLINE_MODE=True``.

    Use this at the top of every connector adapter so the bot is safe to run
    with Wi-Fi disabled even if a caller forgets to branch on OFFLINE_MODE.
    """
    if cfg.OFFLINE_MODE:
        raise OfflineModeViolation(
            f"Refusing to call {what}: OFFLINE_MODE=True. "
            "Flip cfg.OFFLINE_MODE=False to enable network adapters."
        )


def _scan_env_for_live_endpoints(env: dict[str, str] | None = None) -> list[str]:
    env = env if env is not None else dict(os.environ)
    hits: list[str] = []
    for k, v in env.items():
        if not isinstance(v, str):
            continue
        if not (k.endswith("_URL") or k.endswith("_ENDPOINT") or k.endswith("_HOST")):
            continue
        for pat in _LIVE_ENDPOINT_PATTERNS:
            if pat.search(v):
                hits.append(f"{k}={v}")
                break
    return hits


def _scan_strings_for_live_endpoints(strings: Iterable[str]) -> list[str]:
    hits: list[str] = []
    for s in strings:
        if not isinstance(s, str):
            continue
        for pat in _LIVE_ENDPOINT_PATTERNS:
            if pat.search(s):
                hits.append(s)
                break
    return hits


def assert_paper_only(cfg: Config) -> None:
    """Tripwire: refuse to start if anything points at a live venue."""
    if cfg.LIVE_TRADING:
        raise LiveTradingBlocked(
            "config.LIVE_TRADING is True. This bot is paper-only. Aborting."
        )
    env_hits = _scan_env_for_live_endpoints()
    cfg_hits = _scan_strings_for_live_endpoints(cfg.connector.values())
    if env_hits or cfg_hits:
        raise LiveTradingBlocked(
            "Live/mainnet endpoint detected. Aborting paper-only bot.\n"
            f"  env: {env_hits}\n  connector: {cfg_hits}"
        )


def load_config() -> Config:
    """Build the runtime config and run the paper-only tripwire."""
    cfg = Config()
    cfg.ensure_dirs()
    assert_paper_only(cfg)
    return cfg
