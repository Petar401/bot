# tradr

Modular paper-only trading bot. Runs fully offline by default; online
connectors are opt-in via clearly marked adapter methods.

## Project layout

```
config.py        # settings, risk limits, paper-only tripwire
data_feed.py     # OfflineCSVDataFeed + ConnectorDataFeed (adapter stubs)
strategy.py      # Signal API + SMACrossoverStrategy
portfolio.py     # cash, positions, equity curve, P&L, drawdown
broker.py        # SimulatedBroker (offline) + ConnectorBroker (paper-only)
backtest.py      # offline engine + metrics (CAGR, Sharpe, MDD, win rate, R)
runner.py        # CLI: backtest | paper_offline_replay | paper_live
data/            # ./data/{symbol}_{timeframe}.csv (or .parquet)
reports/         # equity curves + metrics JSON
logs/            # runner.log + decisions.jsonl (signal/order/fill stream)
```

## Install

```bash
pip install -r requirements.txt
```

## Data format

CSV (or Parquet) at `./data/{SYMBOL}_{TIMEFRAME}.csv` with columns:

```
timestamp, open, high, low, close, volume
```

`timestamp` may also be named `datetime`, `date`, or `time`.

## Universes

Two built-in universes are exposed as constants in `config.py`:

- `TOP_CRYPTO_USDT_PAIRS` (default): top-20 crypto pairs quoted in USDT —
  `BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, XRPUSDT, ADAUSDT, DOGEUSDT,
  AVAXUSDT, TRXUSDT, LINKUSDT, DOTUSDT, MATICUSDT, TONUSDT, SHIBUSDT,
  LTCUSDT, BCHUSDT, NEARUSDT, UNIUSDT, XLMUSDT, ATOMUSDT`.
- `TOP_EQUITIES`: `AAPL, MSFT, SPY`.

Pick one via `--universe crypto|equities`, or override entirely with
`--symbols BTCUSDT,ETHUSDT,...`.

## Run

Offline backtest on the default crypto universe (no network):
```bash
python runner.py backtest --universe crypto --timeframe 1d \
    --start 2022-01-01 --end 2024-12-31 --cash 100000
```

Same, but only a subset:
```bash
python runner.py backtest --symbols BTCUSDT,ETHUSDT,SOLUSDT --timeframe 1d
```

Replay local data 'as if live' (no network):
```bash
python runner.py paper_offline_replay --symbols BTCUSDT --timeframe 1h --speed 0
```

Live paper (requires you to fill in the connector adapters first):
```bash
python runner.py paper_live --universe crypto --timeframe 1h
```

## Wiring up your Claude connectors

All network I/O goes through Claude with `mcp_servers` configured — no raw
HTTP. Four adapter methods are the only network surface, and every one of
them refuses to run while `cfg.OFFLINE_MODE=True`:

- `data_feed.ConnectorDataFeed.fetch_bars_via_connector`
- `data_feed.ConnectorDataFeed.fetch_history_via_connector`
- `broker.ConnectorBroker.submit_paper_order_via_connector`
- `broker.ConnectorBroker.fetch_account_via_connector`

Each one builds a prompt and forwards it to a single shared helper,
`_call_claude_with_mcp(cfg, prompt, mcp_url_key, mcp_name_key)`, which is
where the Anthropic SDK call lives. Populate `cfg.connector` with:

```
anthropic_api_key       sk-ant-...
claude_model            e.g. claude-sonnet-4-6
market_data_mcp_url     https://your-market-data-mcp.example.com
market_data_mcp_name    market-data
broker_mcp_url          https://your-paper-broker-mcp.example.com
broker_mcp_name         paper-broker
```

Then implement the body of `_call_claude_with_mcp` (the docstring shows the
exact `client.messages.create(..., mcp_servers=[...])` shape) and flip
`OFFLINE_MODE=False`. Everything else stays untouched.

## Example: BTCUSDT 1h backtest

```bash
python examples/btcusdt_1h_demo.py
```

Reads `./data/BTCUSDT_1h.csv` (auto-generates synthetic data on first run
if missing) and runs:

```bash
python runner.py backtest --symbols BTCUSDT --timeframe 1h \
    --start 2023-01-01 --end 2024-01-01 --cash 10000 --tag btcusdt_1h_demo
```

Outputs land in `reports/btcusdt_1h_demo_{equity_curve.csv,fills.csv,metrics.json}`.
Crypto pairs (any symbol ending in `USDT`/`USDC`/`BUSD`/...) are sized to 4
decimal places automatically; equities still floor to whole shares.

## Safety guarantees

- `LIVE_TRADING` defaults to `False`. Setting it `True` aborts startup.
- Env vars ending in `_URL` / `_ENDPOINT` / `_HOST` are scanned for live/mainnet
  patterns at startup (`api.alpaca.markets`, `api.binance.com`, `mainnet`, ...)
  and abort if any match.
- Risk caps enforced on every order: 1% per-trade risk, 50% gross exposure,
  20% per single symbol (configurable in `config.RiskLimits`).
- Every snapshot, signal, and fill is appended to `logs/decisions.jsonl`.
