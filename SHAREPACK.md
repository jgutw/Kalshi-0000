# Kalshi-0000 — Sharepack

**Snapshot purpose:** handoff package describing what this program is, how it
runs, and what is intentionally *not* in git (secrets, live session state).

**Repo:** https://github.com/jgutw/Kalshi-0000  
**Markets:** Kalshi **15-minute crypto Up/Down** binaries across multiple
symbols (not BTC-only).

---

## One-paragraph description

Kalshi-0000 is an automated trading system for Kalshi’s short-dated crypto
binary markets. For each enabled asset it builds a multi-venue synthetic spot
mid, estimates whether Kalshi’s YES price is mispriced, routes entries through
lag / boundary / dislocation strategies (optional `alpha_edge` overlay), sizes
with fractional Kelly under hard risk caps, and can run in **paper** (local
simulation) or **live** (real Kalshi orders). A Streamlit dashboard and optional
Telegram bridge provide monitoring and remote pause/stop/profile control. Live
starts go through a preflight that treats the Kalshi account (cash + shard
balances) as source of truth.

---

## Traded assets (current)

Defined in `kalshi_bot/config.py` → `ASSETS`. All of the following are
**enabled=True** unless you disable them in code or via CLI:

| Symbol | Kalshi series | Spot symbols (examples) |
|--------|---------------|-------------------------|
| BTC | `KXBTC15M` | btcusdt / BTC-USDT |
| ETH | `KXETH15M` | ethusdt / ETH-USDT |
| SOL | `KXSOL15M` | solusdt / SOL-USDT |
| XRP | `KXXRP15M` | xrpusdt / XRP-USDT |
| DOGE | `KXDOGE15M` | dogeusdt / DOGE-USDT |
| BNB | `KXBNB15M` | bnbusdt / BNB-USDT |
| HYPE | `KXHYPE15M` | hypeusdt / HYPE-USDT |
| NEAR | `KXNEAR15M` | nearusdt / NEAR-USDT |
| ZEC | `KXZEC15M` | zecusdt / ZEC-USDT |

Each enabled symbol runs as its own `AssetEngine` (concurrent), sharing one
bankroll / risk book (`SimState` + LiveGuard when live).

CLI notes:

- `--no-xrp` forces XRP off for that process.
- `--enable-xrp` forces XRP on (legacy flag; XRP is already on in `ASSETS`).
- There is no per-flag toggle yet for HYPE/NEAR/etc. — edit `ASSETS` or
  `enabled=False` on a row.

Spot venues typically include Coinbase, Binance, OKX, Kraken (and Gemini where
listed). Some alts (e.g. NEAR on Gemini) may lack a venue and rely on the rest
of the synthetic mid.

---

## What you get in this repo

| Area | Contents |
|------|----------|
| Trading core | `kalshi_bot/` — orchestrator, asset engines, signals, strategies, risk, Kalshi client |
| Live safety | `safe_live.py`, `live_guard.py`, live ceilings in `runtime_control.py` |
| Paper sim | `sim_state.py`, session archive / fresh-round via `session_meta.py` |
| Dashboard | `dashboard_app.py` + `dashboard/` (Ops, Mission Control, Trade Journal, …) |
| Telegram | `run_telegram_bridge.py`, `run_telegram_watchdog.py`, `kalshi_bot/telegram/` |
| Analysis | `scripts/` — session compare, overnight analysis, Excel export helpers |
| Docs | `README.md`, this sharepack, `docs/`, risk proposal/changelog |

**Not in git (by design):** `.env` / API keys, `venv/`, `logs/`, `sessions/`,
generated Excel/reports, local sim balance files.

---

## Architecture (decision path)

```
Spot feeds (Coinbase / Binance / OKX / Kraken [+ Gemini where available])
        │
        ▼
Synthetic spot + lag tracker + signal engine   (per asset)
        │
        ▼
p_base (structural) ⊕ alpha_micro  →  p_real (logit blend)
        │
        ▼
Strategy router → lag_arb / close_boundary / dislocation (+ alpha_edge)
        │
        ▼
Risk gates → Kelly size (profile caps) → paper sim OR live order
        │
        ▼
Resolve at window end  or  early_exit on adverse MTM (if enabled)
        │
        ▼
logs/ + optional Telegram alerts + Streamlit dashboard
```

**Live-specific plumbing**

- Crypto 15m markets use Kalshi **exchange shard 2**; cash may need consolidating from shard 0.
- Client snaps prices to venue tick ranges and signs REST with RSA PEM (needs `cryptography`).
- `start_live_safe(profile)` preflights balance, funds the crypto shard when needed, archives stale sessions, and launches the bot sized from **available Kalshi cash**.
- LiveGuard syncs open exposure / cash against the exchange during the run.
- Live ceilings: max **3** consecutive losses across the **whole book**, per-asset breaker **off**, manual resume required (see `LIVE_*` in `config.py`).

---

## Risk profiles (`runtime_control.PROFILE_PRESETS`)

Compile-time defaults in `TradingConfig` match **`max_risk_paper`**. Live /
Telegram rounds usually **apply a preset** at start (e.g. `engineered_risk`).

| Profile | Intent | Typical knobs |
|---------|--------|---------------|
| `max_risk_paper` | Max sample / aggressive paper | Kelly 0.50, 8% max, 30% gross |
| `max_risk_micro` | Small book + activity probe | Kelly 0.50, 10% max; 30m idle probe + fill quota |
| `engineered_risk` | Same signals, smaller size (usual live workhorse) | Kelly 0.30, 5% max, 20% gross, lottery cap 1.5% |
| `live_safe` | Strictest standard live candidate | Kelly 0.25, 4% max, 15% gross, no cheap lotteries |
| `higher_sharpe` | Quality-first; no `alpha_edge` | Kelly 0.25, 4% max; Sharpe floor after 20 trades |

Profile prose lives in `PROFILE_META` (Telegram / round logs). Switching
profiles via Telegram `/profile` applies preset knobs onto `cfg`.

---

## How to run (operator cheat sheet)

All commands from the project root, with the project venv activated.

### Install

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r kalshi_bot/requirements.txt
```

### Secrets (never commit)

Create `.env`:

```env
KALSHI_API_KEY=...
KALSHI_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----
...
-----END RSA PRIVATE KEY-----"
```

Optional Telegram vars are documented under `kalshi_bot/telegram/settings.py`.

### Paper

```powershell
py -3 run_kalshi_bot.py --mode run --session-tag round_name
streamlit run dashboard_app.py
```

Default paper bankroll reset target: `$SIM_BALANCE` = **$2000** (see `config.py`).

### Live (preferred path)

```powershell
# Account check only
.\venv\Scripts\python.exe run_safe_live.py --check-only

# Start live with a named profile (sizes from Kalshi available cash)
.\venv\Scripts\python.exe run_safe_live.py --profile engineered_risk
```

Or from Python:

```python
from kalshi_bot.safe_live import start_live_safe, format_live_start_result
print(format_live_start_result(start_live_safe("engineered_risk")))
```

### Smoke tests

```powershell
py -3 -m kalshi_bot.test_engines
```

### Analysis helpers

```powershell
py -3 scripts/report_last_day.py
py -3 scripts/compare_sessions.py
```

### Telegram remote control (when bridge is running)

`/pause` `/resume` `/stop` `/set_kelly` `/profile` (and live resume helpers as wired).

---

## Key entry points

| File | Role |
|------|------|
| `run_kalshi_bot.py` | Main CLI: `scan` / `run` / `status`; `--live`, Kelly / size overrides |
| `run_safe_live.py` | Safe live start / preflight from Kalshi balance |
| `dashboard_app.py` | Streamlit control room |
| `run_telegram_bridge.py` | Telegram command bridge |
| `run_telegram_watchdog.py` | Keep bridge / bot health supervised |
| `run_overnight_watchdog.py` | Overnight supervision helper |

---

## Runtime artifacts (local only)

| Path | Purpose |
|------|---------|
| `logs/kalshi_sim.json` | Sim / mirrored cash state |
| `logs/kalshi_trades.jsonl` | Closed trades |
| `logs/kalshi_decisions.jsonl` | WAIT / BUY ticks + reasons |
| `logs/session_meta.json` | Tag, profile, active/idle |
| `logs/bot_heartbeat.json` | Liveness for dashboard / guards |
| `logs/runtime_control.json` | Pause flag + applied knobs |
| `sessions/session_YYYY-MM-DD_HHMM/` | Archived rounds after stop / fresh-round |

---

## Operator notes from live use

- Prefer **`engineered_risk`** as the balanced live default after paper validation.
- `higher_sharpe` can be too idle (misses fills); `max_risk_*` increases fire rate and drawdown.
- With **9 symbols**, whole-book live breaker matters: many small losses can trip before any single asset does.
- Common idle / skip reasons in decisions: `lag_absent`, `structural_model_invalid`, `venue_dislocation`.
- After reboot, check for **duplicate bot PIDs** before starting another live session.
- Dashboard: http://localhost:8501

---

## Safety & disclaimer

This software can place **real-money** orders when started live. It is research /
operator tooling, not a managed product. Always verify `.env` is local-only,
preflight with `--check-only`, and confirm a single bot process before leaving
unattended. Past paper or live PnL does not predict future results.

---

## Restore / share later

This project’s saved copy lives on GitHub at the URL above. Ask the assistant
to save again after meaningful code changes (commit + push). Keep secrets and
`logs/` / `sessions/` on the machine that runs the bot.
