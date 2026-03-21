# Dashboard Monitoring Guide

## First 30 Seconds — Check Feed Health

Look at each asset card header. Right now ETH, SOL, and XRP all show **FEED UNRELIABLE**. This is your first check every session.

- **Green state:** Spot shows two different numbers with an arrow (e.g. `71,383 vs 71,294 ↑`) and Synth conf ≥ 0.60
- **Bad state:** "FEED UNRELIABLE" — means fewer than 2 venues are fresh. The structural model cannot compute a valid p_base and will WAIT on everything
- **What to do:** If FEED UNRELIABLE persists past 2 minutes after startup, restart the bot. The feeds aren't reconnecting cleanly

---

## The Probability Stack — Your Primary Signal

For each asset card, read the three numbers in order:

```
p_market | p_base | p_real
```

**What healthy looks like:**
- p_base between 0.20 and 0.80 (not stuck at 0.0% like ETH/SOL/XRP right now)
- p_real close to p_base with a small adjustment
- p_market meaningfully different from p_real (that gap is your edge)

**What broken looks like:**
- p_base = 0.000 → structural model has no valid spot price (feed issue)
- p_real = 0.500 → model has no conviction, defaulting to 50/50
- BTC right now is the only healthy asset: p_market=0.986, p_base=0.998, p_real=0.500

---

## Z-Score — How Far From the Threshold

The z-score under STRUCTURAL MODEL tells you how decisive the current window is:

| Z-score | Meaning | What to expect |
|---------|---------|----------------|
| > 1.5 | STRONG YES | p_base near 0.90+ |
| 0.5 to 1.5 | LEAN YES | p_base 0.65-0.90 |
| -0.5 to 0.5 | COINFLIP | p_base near 0.50 |
| < -1.5 | STRONG NO | p_base near 0.10 |

BTC shows z=2.84 STRONG YES right now which is legitimate — the window is nearly decided. ETH/SOL/XRP show z=0.00 COINFLIP because their p_base is broken at 0.0%.

---

## WAIT Reasons — Why No Trades Are Firing

Look at the reason shown under **Strategy — None — WAIT** on each card:

- `structural_model_invalid` — p_base outside bounds, usually a feed problem
- `position_open` — already in a trade this window, correct behavior
- `edge(+0.058<0.167)` — edge exists but below the minimum threshold
- `lag_absent` — lag tracker hasn't confirmed Kalshi is lagging
- `uncertain_near_50` — price too close to 50/50 with weak confidence

Right now SOL shows `edge(+0.058<0.167)` which is actually healthy — the model sees some edge but it's not large enough to trade. That's the filter working correctly.

---

## Strategy Router Panel (Bottom Table)

Check this table every 5-10 minutes:

```
asset | score | action | lag_confidence | spot_confidence
```

**What you want to see before a trade fires:**
- score > 0.04 (absolute value)
- lag_confidence > 0.30
- spot_confidence ≥ 0.60

Right now all four assets have score near 0.00 and lag_confidence is 0.00 for BTC and XRP. No trades will fire until lag_confidence builds up — this takes 5-10 minutes of the bot running and observing Kalshi vs spot divergence.

---

## SOL Probability Chart

This is the most useful diagnostic view. What you're seeing is:

- **p_market (blue):** Jumping around 0.80-1.0, very erratic
- **p_base (orange):** Volatile, ranging from 0.60 to 1.0
- **p_real (green):** Flat around 0.50, not tracking either

The flat green line means the micro-alpha model is outputting near-zero alpha — it has no conviction. The erratic orange line suggests p_base is still unstable, likely because the vol estimate is still noisy early in the session.

---

## Your 5-Minute Monitoring Routine

1. **Feed health** — Are all 4 cards showing real spot numbers or FEED UNRELIABLE?
2. **p_base** — Is it between 0.05 and 0.95 for all assets?
3. **lag_confidence** — Is any asset above 0.30 in the router table?
4. **WAIT reasons** — Is `structural_model_invalid` still dominating or has it shifted to `uncertain_near_50` and `lag_absent`?
5. **Log staleness** — Top of Mission Control should show green "Last log update: Ns ago" — if it goes red the bot has crashed

---

## When to Restart vs Wait

| Situation | Action |
|-----------|--------|
| FEED UNRELIABLE on 3+ assets after 3 minutes | Restart bot |
| p_base = 0.000 on all assets | Restart bot |
| All WAIT reasons = `structural_model_invalid` | Restart bot |
| WAIT reasons = `lag_absent`, `uncertain_near_50` | Wait — normal early-session behavior |
| WAIT reasons = `position_open` | Good — bot is in a trade |
| Log staleness indicator turns red | Restart bot immediately |

---

## Restart Procedure

1. **Stop both processes:** Ctrl+C in bot terminal, Ctrl+C in dashboard terminal

2. **Start bot first:**
   ```powershell
   cd C:\Projects\Kalshi-0000
   .\venv\Scripts\Activate.ps1
   python run_kalshi_bot.py --mode run
   ```

3. **Wait 60 seconds** and watch for:
   ```
   [BTC] Coinbase connected
   [BTC] First Coinbase trade: price=XXXXX
   [BTC] Synthetic spot initialized: XXXXX
   ```
   Once you see those three lines for each asset, start the dashboard.

4. **Start dashboard:**
   ```powershell
   cd C:\Projects\Kalshi-0000
   .\venv\Scripts\Activate.ps1
   streamlit run dashboard_app.py
   ```

If p_base is still 0.000 after restart, the Coinbase feed is failing to deliver trade prices to the signal engine. Screenshot the first 60 seconds of terminal output and share for debugging.
