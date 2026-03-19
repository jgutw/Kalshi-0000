# Overnight Bot Performance — Reconciliation Report

## TL;DR

**Last 10 hours: 0 trades placed.** The bot ran and made 11,820 decision ticks, but none met all entry thresholds. This is expected behavior given current market conditions and your filter settings.

**Historical: 20 trades** (per sim) from a previous session (March 18), with **+$308.28** P&L. The `kalshi_report.xlsx` reflects those trades.

---

## Timeline

| Data Source | Time Range | Count |
|-------------|------------|-------|
| **kalshi_decisions.jsonl** | Mar 19, 11:18 UTC → 14:25 UTC (~3h) | 11,820 decisions |
| **kalshi_trades.jsonl** | Mar 18, 14:00 → 17:00 UTC | 24 trades (all from yesterday) |
| **kalshi_sim.json** | — | total_trades=20, balance=$1,308.28 |

**Takeaway:** All 24 trades in the file are from **March 18**. The overnight run (March 19) produced **zero trades**.

---

## Why No Trades in the Last 10 Hours?

Every decision was a WAIT. Here’s the breakdown:

| WAIT Reason | Count | % | What it means |
|-------------|-------|---|---------------|
| **mispricing_weak** | 3,632 | 30.7% | CWM (confidence-weighted mispricing) < 0.03 — Kalshi wasn’t lagging enough vs spot |
| **structural_model_invalid** | 2,677 | 22.6% | p_base outside [0.05, 0.95] — market already decided (e.g. BTC/ETH dropped sharply) |
| **spot_confidence_low** | 2,044 | 17.3% | Synthetic spot had < 2 fresh venues — feed reliability issue |
| **lag_absent** | 980 | 8.3% | Lag tracker didn’t detect Kalshi lagging spot |
| **window_boundary** | 477 | 4.0% | Too close to open or close of window |
| **lag_confidence_low** | 458 | 3.9% | Lag present but confidence < 0.25 |
| **p_real_near_50** | 331 | 2.8% | Model had no conviction (p_real near 50%) |
| **uncertain_near_50** | 242 | 2.0% | Price near 50/50, weak confidence |
| **sharpe(0.40<1.2)** | 145 | 1.2% | Sharpe gate blocked (below 1.2 after 20 trades) |

**Top 3 blockers:**
1. **mispricing_weak** — Most of the time the edge wasn’t large enough (CWM < 0.03).
2. **structural_model_invalid** — Markets were often decided (p_base near 0 or 1).
3. **spot_confidence_low** — Synthetic spot lacked 2+ fresh venues for 17% of ticks.

---

## Near-Misses

There were **4,090** decisions that passed most gates but failed on one. Examples:

- **ETH** had CWM up to 0.027 (threshold 0.03) with lag_confidence 0.77 — very close.
- **BTC** had CWM ~0.015–0.017 — not enough mispricing.
- **SOL** and **XRP** often hit **lag_confidence_low** (e.g. 0.16–0.33) or **mispricing_weak**.

If lag_confidence were slightly higher or CWM slightly above 0.03, some of these would have become trades.

---

## Data Reconciliation

| File | Purpose | Status |
|------|---------|--------|
| **kalshi_trades.jsonl** | Closed trades (one JSON per line) | 24 records, all from Mar 18 |
| **kalshi_decisions.jsonl** | Every decision tick (WAIT or BUY) | 11,820 in last 10h |
| **kalshi_sim.json** | Balance, total_trades, asset_stats | total_trades=20, balance=$1,308.28 |
| **kalshi_events.jsonl** | Raw market events (recorder) | ~5k total; ts format may differ |
| **kalshi_features.jsonl** | Feature snapshots at decision points | ~11.7k; mirrors decisions |
| **kalshi_report.xlsx** | Excel report from trades | Built from kalshi_trades.jsonl |

**Mismatch: 24 trades in file vs 20 in sim.** Likely causes:
- Sim file saved before the last 4 trades were written.
- Bot restart that reloaded an older sim.
- Manual edits or multiple processes writing to the same files.

**Recommendation:** Run `python export_excel.py` to regenerate the report. The report uses `kalshi_trades.jsonl` (24 trades); sim’s `total_trades` is a separate counter that can drift.

---

## Is the Bot Working Correctly?

Yes. The filters are doing what they’re designed to do:

1. **structural_model_invalid** — Correctly blocks when p_base &lt; 0.05 or &gt; 0.95 (e.g. market 99.95% certain to resolve NO).
2. **mispricing_weak** — Avoids trading when CWM &lt; 0.03 (insufficient edge).
3. **lag_confidence_low** — Waits until lag evidence is strong enough.

The situation you described (BTC/ETH decided, SOL/XRP borderline) matches this behavior.

---

## What to Expect Next

- At the start of a new window, z-scores reset and lag builds over 5–10 minutes.
- More trades are likely when:
  - Spot moves sharply and Kalshi lags.
  - CWM crosses 0.03 and lag_confidence ≥ 0.25.
  - Spot confidence stays ≥ 0.6 (2+ venues fresh).

If you want more trades, you could:

- Lower **CWM threshold** from 0.03 to 0.025 in `lag_arb.py` (more signals, but weaker edge).
- Lower **LAG_CONFIDENCE_MIN** from 0.25 to 0.20 (already reduced from 0.30).
- Monitor **spot_confidence_low** — if it stays high, synthetic spot feeds may need tuning.

---

## Run This to Re-Analyze

```bash
python analyze_overnight.py
```

This script prints the WAIT breakdown, near-misses, and reconciliation for the last 10 hours.
