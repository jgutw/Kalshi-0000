# When You Return — Paper Trading Quick Reference

## Start the Paper Trading Session

**Terminal 1 — Dashboard**
```powershell
.\venv\Scripts\Activate.ps1
streamlit run dashboard.py
```
→ Open **http://localhost:8501** in your browser

**Terminal 2 — Bot**
```powershell
.\venv\Scripts\Activate.ps1
python polymarket_bot.py --mode run
```

**BTC-only mode** (5/15-min markets only, no arb/weather/events):
```powershell
python polymarket_bot.py --mode run --btc-only
```

Leave both running. Paper trading is **LIVE** when the bot terminal shows no errors and keeps scrolling (WebSocket data, scan logs).

---

## BTC-Only Mode (5/15-min markets only)

To run **only** the BTC engine (no arbitrage, weather, events, or maker):

```powershell
python polymarket_bot.py --mode run --btc-only
```

Scan mode to check for a BTC market:
```powershell
python polymarket_bot.py --mode scan --btc-only
```

---

## Paper vs Real Money

| Mode | How to enable | What happens |
|------|---------------|--------------|
| **Paper (current)** | `DRY_RUN=True` (default) | Simulated trades only, no real orders |
| **Live** | `--live` flag or `DRY_RUN=False` | Real money — **do not enable** until gates pass |

---

## Questions You Can Ask When You Return

### About the data
- *"What's my current balance and win rate?"* → Check `simulation.json` or the dashboard
- *"Show me my last 10 trades"* → `paper_trades.jsonl` (each line is one trade)
- *"Which engine found the most signals?"* → `simulation.json` → `last_scan`

### About markets we're in
- *"What markets did the bot trade?"* → `paper_trades.jsonl` has `market_id`, `strategy`
- *"Why did we enter that weather trade?"* → NWS forecast vs Polymarket bucket price, Gaussian EV
- *"Why did we take that arb?"* → YES+NO < $1 (BUY_BOTH) or resolution sweep (99¢ strategy)

### About code changes
- *"What changed since I left?"* → `git log --oneline -10` or `git diff`
- *"Show me recent commits"* → `git log -5`

---

## Key Data Locations

| File | What it contains |
|------|------------------|
| `simulation.json` | Balance, wins, losses, win_rate, sharpe, last_scan counts, active_quotes |
| `paper_trades.jsonl` | Every closed trade: ts, market_id, pnl, strategy, entry/exit prices |
| `config_override.json` | Strategy filter (from dashboard sidebar) — which engines are active |

---

## Quick Health Check

```powershell
python test_engines.py
```
All 6 checks should pass. If any fail, fix before running the full bot.

---

## Monitor Code Changes

```powershell
git status          # Uncommitted changes
git log -3          # Last 3 commits
git diff            # Diff of working tree vs last commit
```
