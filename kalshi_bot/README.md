# Kalshi Multi-Asset 15-Min Bot

Trades BTC, ETH, SOL, and XRP 15-minute Up/Down markets on Kalshi concurrently.
Rearchitected from the Polymarket 5-min bot with all post-mortem fixes applied.

## File Structure

```
kalshi_bot/
├── config.py          — All config dataclasses and asset manifest
├── kalshi_client.py   — REST + RSA-PSS auth client
├── signal_engine.py   — Hawkes, OBI, Bayesian fusion (per-asset)
├── sim_state.py       — SimState with cooldown circuit breaker
├── asset_engine.py    — Decision + position lifecycle per asset
├── kalshi_bot.py      — Orchestrator + async event loop
├── dashboard.py       — Streamlit dashboard
├── test_engines.py    — Smoke tests
├── requirements.txt
└── .env               — Credentials (gitignored)
```

## Post-Mortem Fixes Applied

| Issue | Old behavior | Fix |
|-------|--------------|-----|
| Circuit breaker never reset | consec_losses >= 3 halted forever | Cooldown timer: resumes after COOLDOWN_MINUTES (default 60m). Win resets streak. Daily loss resets at UTC midnight. |
| Stale 0.505 price | Gamma outcomePrices polled every 2s, never refreshed mid-window | Kalshi REST orderbook polled every 1s; staleness guard rejects price unchanged > 30s when near 50/50 |
| Hawkes decay too fast | HAWKES_DECAY=3.0 → half-life 0.23s; 99% decayed before next poll | HAWKES_DECAY=0.046 → half-life 15s, matching 15-min market cadence |
| No edge at 50/50 | Bot entered regardless of market price stickiness | PRICE_MAX_AGE_SECS guard; SKIP_OPEN_SECS=45 (was 30) for 15-min windows |
| Single asset | BTC only | BTC, ETH, SOL, XRP run concurrently; per-asset AssetStats in SimState |
| Poll lag | 2s REST poll (Polymarket) | 1s REST poll (Kalshi); exchange data via WebSocket |

## Quick Start

**Run all commands from the project root** (`Polymarket-1/`).

### 1. Install

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r kalshi_bot/requirements.txt
```

### 2. Configure .env

```env
# Kalshi credentials (from https://kalshi.com/account/developer)
KALSHI_API_KEY=your_api_key_here
KALSHI_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----
...
-----END RSA PRIVATE KEY-----"

# Use demo environment for paper trading (recommended)
KALSHI_DEMO=true
```

**Paper trading:** `KALSHI_DEMO=true` routes all requests to Kalshi's demo API.
No real money. Run for at least 1–2 weeks before enabling live mode.

**API migration:** If you see 401 "API has been moved", check [Kalshi's API docs](https://docs.kalshi.com) for the current base URL. You can override with `KALSHI_BASE_URL=https://api.elections.kalshi.com/trade-api/v2` (or the current URL from their docs) in `.env`.

### 3. Run tests

```powershell
python -m kalshi_bot.test_engines
```

All checks should pass. If any fail, fix before running the bot.

### 4. Scan (verify markets exist)

```powershell
python run_kalshi_bot.py --mode scan
```

Confirms each series ticker exists (KXBTC15M, KXETH15M, etc.) and shows
current YES mid-prices. If XRP returns "NOT FOUND", add `--no-xrp` to the run command.

### 5. Paper trade

**Terminal 1 — Bot:**
```powershell
python run_kalshi_bot.py --mode run
```

**Terminal 2 — Dashboard:**
```powershell
streamlit run kalshi_bot/dashboard.py
```

Open http://localhost:8501

### 6. Live trading

Only after ≥2 weeks of paper results with consistent win rate > 55%:

```powershell
python run_kalshi_bot.py --mode run --live
```

## Series Tickers

These must be verified against the Kalshi API. Run `--mode scan` to confirm.

| Asset | Series ticker | Notes |
|-------|---------------|-------|
| BTC | KXBTC15M | Confirmed available |
| ETH | KXETH15M | Confirm in scan |
| SOL | KXSOL15M | Confirm in scan |
| XRP | KXXRP15M | May not exist; use `--no-xrp` if scan fails |

To find the right ticker yourself:
`GET https://trading-api.kalshi.com/trade-api/v2/series`
Look for series with `frequency=15min` and `category=crypto`.

## Architecture

```
Binance WS (aggTrade + depth20) ─┐
                                  ├─→ AssetSignalEngine (Hawkes/OBI/EMA/HA/MRO)
OKX WS (books5 + trades) ────────┘          │
                                             ↓
                                     BayesFusion (6 signals)
                                             │
Kalshi REST (orderbook, 1s) ──────→ AssetEngine.make_decision()
                                             │
                                     Kelly sizing + risk gates
                                             │
                                     kalshi_client.place_market_order()
                                             │
                                         SimState.record()
```

Each asset runs independently via `asyncio.gather()`. State is not shared between
asset engines except through the common SimState (balance, circuit breaker).

## Risk Rules (always enforced)

- ¼ Kelly position sizing
- Max 3% of bankroll per trade
- Halt at 3 consecutive losses → 60-min cooldown (auto-resumes)
- Halt at 10% daily loss → resumes next UTC day
- Min 5% edge required (widened dynamically by belief volatility)
- Min 3/5 signals must agree (conviction gate)
- Skip first 45s / last 30s of each 15-min window
- Macro blackouts: FOMC, CPI, NFP ±5 minutes
