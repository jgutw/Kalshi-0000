# Proposal — Risk Update v1 (synthesis of three reviews + arbitration on data)

**Status:** proposal only. No code changed. Awaiting operator go-ahead.
**Data basis:** 174 closed paper trades, 7 rounds, 2026-08-09/10 archives + active R32.
**Method note:** all bucket stats below are **risk-normalized** — each trade converted to return-on-equity-at-entry (`pnl / equity_before`), because per-round bankrolls differ 10x ($200 → $2000) and dollar stats are dominated by R26 alone.

---

## 0. Headline

All three reviews (mine, yours, the external one) proposed **signal/gate engineering** as the primary fix. The data says the dominant, statistically visible lever is **position sizing and gross exposure per window**. Several specific proposals are **not supported** and should be dropped before we write code.

The single strongest relationship in the dataset:

| Gross exposure in a window (sum of position sizes as % of equity) | Windows | Windows profitable | Total return |
|---|---|---|---|
| &lt; 10% | 2 | 100% | +15.6% |
| 10–20% | 15 | **93.3%** | **+337.8%** |
| 20–35% | 32 | 53.1% | +45.9% |
| **&gt; 35%** | 3 | **0.0%** | **−116.5%** |

Current config allows exactly the fatal zone: `PORTFOLIO_GROSS_CAP = 0.30` (paper) / `0.35` (micro), `MAX_POS_PCT = 0.08–0.10`, `KELLY_FRACTION = 0.50`. Observed **median risk per single trade is 7.65% of equity, max 13.0%**. With `MAX_CONSEC_LOSSES = 8`, the circuit breaker permits roughly a 50% drawdown before it trips.

**That is the bug.** R28/R29/R30 are explained by sizing, not by signal decay.

---

## 1. Arbitration — claims that did NOT survive

### 1.1 "60% of loss dollars come from multi-asset loss windows" — CONFOUNDED (my earlier claim)

Controlling for window trade count kills it: windows with 3–4 trades produce **95.2% of all losses and 99.6% of all wins**. The bot nearly always trades 3–4 assets per window, so "multi-asset window" ≈ "window". A same-window concurrency throttle as specified would remove wins and losses in equal measure.

The external review flagged exactly this risk. It was right.

**What survives:** only the extreme tail. 5+ concurrent positions: n=15, WR 20.0%, return −72.7%. That is real but rare, and it is better expressed as a **gross exposure cap** than a position count.

### 1.2 "Lottery entries have positive expectancy" — ONE WINDOW

| Sample | n | WR | Net return |
|---|---|---|---|
| All entry &lt; 0.15 | 12 | 16.7% | +37.6% |
| Excluding R31 | 10 | **0.0%** | −22.4% |
| Excluding top 2 trades | 10 | **0.0%** | −22.4% |

Both R31 lottery wins (SOL NO 0.055 → +$679, XRP NO 0.12 → +$293) occurred in **the same window** (16:30). That is one event, not two samples. Outside it, lotteries are **0 for 10**.

**But** these are currently sized at **8.41% average risk** — full Kelly on a 17%-win-rate bet. The problem is the size, not the existence of the trade. Do not ban; cap hard.

### 1.3 "Skip near-miss settles" — DOES NOT REPLICATE

| Settle margin | n | WR | Net return |
|---|---|---|---|
| &lt; 2 bps | 9 | **66.7%** | +387% |
| 2–5 bps | 16 | 43.8% | +1346% |
| 5–15 bps | 46 | 34.8% | +1446% |
| &gt; 15 bps | 103 | 58.3% | +4828% |

The R29 counterfactual (−$179 → −$15) was round-specific noise; knife-edge settles are not systematically losers. **Drop this idea.**

### 1.4 "Mid-band 0.20–0.40 is the problem bucket" — WRONG SIGN

| Entry bucket | n | WR | Avg return / trade |
|---|---|---|---|
| &lt; 0.15 | 12 | 16.7% | +3.13% |
| 0.15–0.20 | 6 | 50.0% | +2.90% |
| **0.20–0.40** | **81** | 39.5% | **+0.73%** |
| 0.40–0.60 | 64 | 65.6% | +2.15% |
| &gt; 0.60 | 11 | 90.9% | +2.77% |

The mid-band is **net positive**, and it holds up out of sample (early rounds WR 42.9%, later rounds 38.2%, both net positive). It only looked bad in dollar terms because it carries the most volume. It is the *weakest* bucket, not a losing one.

**Correct action:** relative size tilt, not a hard gate. Gating it at conviction ≥ 4 / CWM 0.03 would cut ~47% of all trades to remove a positive-expectancy bucket.

### 1.5 "Same-side (all YES/all NO) clustering is the risk" — NOT SUPPORTED

Same-side multi-trade windows: 76 trades, WR 50.0%, +$4403. Mixed: 98 trades, WR 52.0%, +$3604. No meaningful edge difference. **Drop the directional gross cap as specified.**

---

## 2. Claims that DID survive (build these)

### 2.1 Gross exposure per window is the dominant risk lever — STRONG

See headline table. Effect is monotone and large. This subsumes the concurrency idea.

### 2.2 Per-trade risk is far too high — STRONG

Median 7.65% / max 13.0% of equity per position. Kelly 0.5 on a binary with an uncalibrated `p_real` is aggressive to the point of self-harm; 8 consecutive losses (which occurred today) at that size is a ~48% drawdown.

### 2.3 YES side is structurally weaker — MODERATE (n=174)

| Side | n | WR | Sum return | Avg / trade |
|---|---|---|---|---|
| YES | 91 | 46.2% | **−15.0%** | −0.17% |
| NO | 83 | 56.6% | **+297.7%** | +3.59% |

NO wins on both frequency and magnitude. Plausible mechanism: `p_base` (GBM digital) systematically over-weights upside drift on 15m crypto windows, so YES looks cheap when it is not. Worth a calibration check before acting hard — but a YES size haircut is a cheap, reversible hedge against it.

### 2.4 Per-asset dispersion — WEAK / WATCH ONLY

Risk-normalized average return per trade: SOL +6.43%, ZEC +3.53%, BNB +3.06%, BTC +2.93%, NEAR +0.86%, HYPE +0.87%, XRP −0.07%, DOGE −1.82%, ETH −2.04%.

Samples are 14–25 trades per asset. **Do not disable assets on this.** Log it and revisit at n ≥ 100 per asset.

### 2.5 `alpha_edge` — UNRESOLVED, needs instrumentation

Offline attribution failed: `kalshi_decisions.jsonl` contains BUY rows too sparsely to join reliably to fills (only 6 of 39 trades matched in R29). We cannot currently prove or disprove the historical "alpha_edge causes most losses" comment. **Instrument before judging.**

---

## 3. Proposed changes

Ordered by evidence strength × reversibility. Each is config-driven and individually revertible.

### C1 — Cut gross exposure cap (highest confidence)

```python
# kalshi_bot/config.py
PORTFOLIO_GROSS_CAP = 0.20        # from 0.30 (paper) / 0.35 (micro preset)
PORTFOLIO_GROSS_HARD_STOP = 0.28  # never exceed, even with override
```
Also update `max_risk_micro` preset in `runtime_control.py` from 0.35 → 0.20.

**Rationale:** 10–20% gross → 93% of windows profitable; &gt;35% → 0%.
**Risk:** fewer concurrent entries per window; may cut some winners. Mitigated because the cap binds only in the zone that has never worked.

### C2 — Cut per-trade risk

```python
KELLY_FRACTION = 0.30   # from 0.50
MAX_POS_PCT    = 0.05   # from 0.08 (paper) / 0.10 (micro)
```
**Rationale:** median trade currently risks 7.65% of equity. At 8 consecutive losses (observed today) that is ~48% drawdown; at 5% it is ~34%; combined with C1 it is materially safer.
**Note:** halving size roughly halves both PnL and drawdown (already confirmed by `half_size` counterfactual across rounds). This trades headline P&L for survivability — that is the intent.

### C3 — Lottery risk cap (not a ban)

```python
LOTTERY_ENTRY_MAX        = 0.15
LOTTERY_MAX_RISK_PCT     = 0.015   # vs current ~8.4% average
LOTTERY_MAX_CONCURRENT   = 1
```
In the sizing path: if `entry_price < LOTTERY_ENTRY_MAX`, `size = min(kelly_size, equity * LOTTERY_MAX_RISK_PCT)` and enforce concurrency.

**Rationale:** 0-for-10 outside a single window, but the one window returned +$972. Keep the ticket, pay 1.5% not 8.4% for it. R31's SOL NO would still have been taken (and would have returned ~$120 instead of $679 — acceptable price for removing the 10 losers' sizing).

### C4 — Tighten the circuit breaker

```python
MAX_CONSEC_LOSSES = 5      # from 8
MAX_DAILY_LOSS_PCT = 0.25  # from 0.40
```
**Rationale:** today hit exactly 8 consecutive losses — i.e. the breaker never protected anything. At the new sizes, 5 losses ≈ 15–20% drawdown, which is a sane place to pause and let the operator look.

### C5 — YES-side size haircut (experimental, flagged)

```python
YES_SIZE_MULT = 0.7   # 1.0 disables the experiment
```
Applied as a multiplier in sizing when side is YES.
**Rationale:** YES is −15% cumulative vs NO +298% risk-normalized. This is a hedge against `p_base` upward bias, not a claim that YES is unprofitable. Reversible in one line; revisit after calibration data exists.

### C6 — Mid-band soft tilt (replaces the proposed hard gate)

```python
MID_BAND_LOW, MID_BAND_HIGH = 0.20, 0.40
MID_BAND_SIZE_MULT = 0.8
```
**Rationale:** bucket is positive but lowest-return per unit risk. Tilt capital toward the 0.40–0.60 band (+2.15%/trade) without deleting ~47% of trade flow.

### C7 — P0 instrumentation (prerequisite for everything else)

1. Snapshot the full decision context onto every trade row written to `kalshi_trades.jsonl`: `strategy`, `ev`, `cwm`, `lag_confidence`, `conviction`, `p_real`, `p_base`, `p_market`, `time_remaining`, `portfolio_gross_at_entry`, `concurrent_count`, `is_lottery`, `size_mult_applied`. Touchpoint: `asset_engine.py` at fill/record time; writer in `recorder.py` / sim record path.
2. Log `p_real` and realized outcome to a calibration file so a reliability curve can be built after ~300 trades.
3. Telegram `/losses` command: loss ledger by asset / side / entry bucket / gross bucket / concurrency, **risk-normalized**. Touchpoint: `telegram/handlers.py` + small report helper.
4. Keep `ALPHA_EDGE_ENABLED = True` for now, but tag every trade with its originating strategy so the A/B can actually be measured next round. Quarantine only after we can attribute it.

### Not doing (explicitly)

- Same-window concurrency throttle as originally specified (confounded — C1 covers the real effect)
- Directional YES/NO gross cap (no edge difference found)
- Near-miss skip / shrink (does not replicate)
- Hard mid-band conviction/CWM gate (bucket is profitable)
- Disabling DOGE/ETH (n too small)
- BTC-residual feature, Platt calibration, meta-labeling, HMM regime switch (all P2+; revisit after C1–C7 have run and instrumentation exists)

---

## 4. Combined expected effect

Applying C1–C4 to today's trade sequence, approximately (accounting identity, not a backtest):

- Per-trade risk drops from ~7.6% → ~3.5–4% of equity
- Window gross capped at 20% — the 3 catastrophic windows (0% win rate) could not have been constructed
- Lottery bleed drops ~80% while keeping the jackpot path
- R26/R31 upside is roughly halved in percentage terms but preserved in shape
- Circuit breaker trips at ~5 losses instead of after the damage is done

The goal is a **flatter equity curve with the same trade population** — not fewer trades, smaller ones.

---

## 5. Rollout plan

Run all changes in **paper** under a new profile so `max_risk_paper` stays as the untouched baseline.

```
runtime_control.PROFILE_PRESETS["engineered_risk"] = {
    KELLY_FRACTION: 0.30, MAX_POS_PCT: 0.05,
    PORTFOLIO_GROSS_CAP: 0.20, MIN_TRADE_USD: 2.0,
}
```

| Step | Change | Rounds | Pre-registered metric |
|---|---|---|---|
| S0 | C7 instrumentation only | 1 | % trades with full decision snapshot = 100% |
| S1 | C1 + C2 + C4 (sizing + caps) | 4 | Max drawdown per round; return per unit risk; win$ retained vs baseline |
| S2 | + C3 lottery cap | 3 | Lottery bucket net return; jackpot still capturable |
| S3 | + C6 mid-band tilt | 3 | Return per unit risk in 0.20–0.40 vs 0.40–0.60 |
| S4 | + C5 YES haircut | 3 | YES vs NO risk-normalized return; calibration curve |

Promotion rule: keep a change only if **max drawdown improves** and **sum of wins does not fall more than ~15%** relative to the `max_risk_paper` baseline over the same number of rounds.

**Live:** no live promotion until S1–S2 complete and a `live_safe` preset exists (Kelly ≤ 0.25, MAX_POS ≤ 0.04, gross ≤ 0.15, lottery off, LiveGuard green, Kalshi cash as source of truth).

---

## 6. Overfitting caveats

- **174 trades, one day.** Every number above is a hypothesis, not a parameter estimate. The gross-exposure result rests on only 3 windows above 35%.
- The 10–20% gross bucket's 93% win rate is partly an artifact of R26/R31 being low-gross, high-quality sessions — direction is trustworthy, magnitude is not.
- Asset-level and side-level effects need 3–5x more data before any hard gating.
- C5 (YES haircut) is the most likely to be noise; it is deliberately a soft multiplier with a one-line off switch.
- We are tuning risk parameters on the same data that revealed the problem. The mitigation is that these are **exposure limits, not signal filters** — they do not encode any belief about which trades will win.

---

## 7. Operator decision required

Approve any subset. Recommended first commit: **C7 + C1 + C2 + C4** (instrumentation + sizing/caps), leaving lottery, mid-band, and YES experiments for subsequent rounds so each remains attributable.
