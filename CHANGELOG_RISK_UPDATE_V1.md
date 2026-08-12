# Changelog — Risk Update v1 (2026-08-10)

Every change below is **additive**. No existing trading regime was modified.
`max_risk_paper` and `max_risk_micro` behave exactly as they did for R11 and
R19–R32; this was verified by applying the new profile, switching back, and
asserting all fourteen affected parameters returned to their original values.

Evidence behind these changes: `PROPOSAL_RISK_UPDATE.md`
(174 closed paper trades, risk-normalized to % of equity at entry).

---

## Why anything changed at all

Dollar-weighted statistics said the problem was signal quality in specific
entry-price buckets. Once every trade was converted to return-on-equity — which
matters because round bankrolls ranged from $200 to $2000 — the picture
inverted. The dominant loss driver was **position size**, not signal quality:

- Windows with 10–20% gross exposure were profitable 93% of the time; windows
  above 35% gross went 0-for-3.
- The median single position risked 7.65% of equity (max 13.0%).
- The circuit breaker sat at 8 consecutive losses, which is roughly a 50%
  drawdown at that size — it never protected anything on 2026-08-10.

So the update is a set of **exposure limits**, not signal filters. It does not
encode any belief about which trades will win, which is also what makes it
relatively safe against overfitting to a single day.

---

## C1 — Portfolio gross hard stop

**What:** new `PORTFOLIO_GROSS_HARD_STOP` config knob and a matching gate in
`asset_engine.make_decision()`, checked right after the existing
`PORTFOLIO_GROSS_CAP` test.

**Description:** `PORTFOLIO_GROSS_CAP` blocks a new entry when the *projected*
exposure would exceed the cap. The hard stop is a never-exceed backstop on
*current* exposure, so no combination of sizing paths can construct a window
above the limit. Waits are logged as `portfolio_gross_hard_stop(...)`.

**Default:** `1.0` (disabled). Only `engineered_risk` (0.28) and `live_safe`
(0.20) turn it on.

**Files:** `config.py`, `asset_engine.py`

---

## C2 — Smaller positions

**What:** `KELLY_FRACTION` 0.50 → 0.30 and `MAX_POS_PCT` 0.08 → 0.05, in the new
profiles only.

**Description:** No code change — these are preset values. The intent is a
flatter equity curve with the *same trade population*, not fewer trades. This
deliberately trades headline PnL for survivability: halving size roughly halves
both the upside and the drawdown.

**Files:** `runtime_control.py` (presets only)

---

## C3 — Lottery sleeve

**What:** `LOTTERY_ENTRY_MAX`, `LOTTERY_MAX_RISK_PCT`, `LOTTERY_MAX_CONCURRENT`
config knobs; `is_lottery_entry()` and `lottery_size_cap_usd()` helpers in
`signal_engine.py`; a sizing branch and concurrency check in `asset_engine.py`.

**Description:** Contracts below $0.15 were **0-for-10 outside a single window**
— both of R31's winners came from the same 16:30 event, so that is one
observation, not two. But they were being sized at ~8.4% of equity on a 17% win
rate. This caps the dollars risked rather than banning the trade, because the
one window that worked returned +$972. R31's SOL NO would still have been taken
under the cap, just for a smaller payout. Blocked entries log
`lottery_sleeve_full(n/max)`.

**Default:** `LOTTERY_MAX_RISK_PCT = 0.0` disables the cap entirely (legacy full
Kelly behavior). `engineered_risk` uses 1.5% with 1 concurrent.

**Files:** `config.py`, `signal_engine.py`, `asset_engine.py`

---

## C4 — Tighter circuit breaker

**What:** `MAX_CONSEC_LOSSES` 8 → **4** and `MAX_DAILY_LOSS_PCT` 0.40 → 0.25 in
the new profiles.

**Description:** 2026-08-10 hit exactly 8 consecutive losses, meaning the
breaker triggered only after the damage was done. Round 33 confirmed the new
sizing lands around 2% risk per trade, so 4 straight losses is roughly an
8–12% drawdown — early enough that a human look is still useful, which is the
entire purpose of the breaker. `engineered_risk` and `live_safe` now share the
same value of 4.

Both values were also added *explicitly* to `max_risk_paper` and
`max_risk_micro` at their existing values (8 and 0.40). This changes nothing
about how those regimes trade; it makes profile switching symmetric, so coming
back from `engineered_risk` restores them instead of leaving the tighter
breaker in place.

**Revision (2026-08-10, operator):** lowered from 5 to 4 while Round 33 was in
flight. A running bot loads its config at startup, so this took effect from the
next round, not retroactively — Round 33 finished on 5.

**Files:** `runtime_control.py` (presets only)

---

## C4b — Runtime breaker controls

**What:** three new runtime commands — `set_consec_losses`, `set_daily_loss`,
`set_max_drawdown` — with matching Telegram commands.

**Description:** Added after the C4 revision exposed a gap: the circuit-breaker
thresholds were the only risk parameters with no mid-round adjustment path,
unlike Kelly, max position, min trade, and portfolio cap. Changing them meant
editing a preset and starting a new round.

`set_consec_losses` accepts 2–8 and **refuses anything above the legacy 8**, so
it cannot be used to disable the breaker while a round is bleeding — the
failure mode the breaker exists to prevent. `set_daily_loss` accepts 5–50% and
`set_max_drawdown` 5–60%.

All three follow the existing `bot_control.jsonl` queue pattern and show up in
`/sizing` via `write_live_config()`.

**Files:** `runtime_control.py`, `telegram/handlers.py`, `telegram/formatters.py`

---

## C5 — YES-side size haircut

**What:** `YES_SIZE_MULT` / `NO_SIZE_MULT` config knobs and a
`side_size_scalar()` multiplier in the sizing chain.

**Description:** Risk-normalized, YES is **−15% cumulative across 91 trades**
while NO is **+298% across 83**. NO wins on both frequency (56.6% vs 46.2%) and
magnitude. The plausible mechanism is `p_base` (the GBM digital) over-weighting
upside drift on 15-minute crypto windows, making YES look cheap when it isn't.

This is the change most likely to be noise, so it is deliberately a soft
multiplier with a one-line off switch rather than a gate. It was independently
reproduced by the new `/losses` ledger on both R29 and the current live session.

**Default:** `1.0` (no tilt). `engineered_risk` uses 0.7 on YES.

**Files:** `config.py`, `signal_engine.py`, `asset_engine.py`

---

## C6 — Mid-band soft tilt

**What:** `MID_BAND_LOW`, `MID_BAND_HIGH`, `MID_BAND_SIZE_MULT` config knobs and
a `mid_band_size_scalar()` multiplier.

**Description:** Three separate reviews proposed hard-gating the 0.20–0.40 entry
band. The data does not support that: risk-normalized, the band is **net
positive** (+0.73% per trade) and it holds up out of sample. It only looked bad
in dollar terms because it carries the most volume — 81 of 174 trades. Gating it
at conviction ≥ 4 would have deleted roughly 47% of trade flow to remove a
profitable bucket. A soft size tilt shifts capital toward the stronger
0.40–0.60 band (+2.15% per trade) without losing the flow.

**Default:** `1.0` (no tilt). `engineered_risk` uses 0.8.

**Files:** `config.py`, `signal_engine.py`, `asset_engine.py`

---

## C7 — Instrumentation (active on all profiles)

**What:** decision snapshot on every closed trade, a calibration log, and a
`/losses` ledger.

**Description:** This is the only change that applies to existing regimes,
because it is purely additive logging and alters no decision. It was the
blocking problem: `alpha_edge` could not be judged because only 6 of 39 R29
trades could be joined back to a decision row.

1. **Decision snapshot** — every closed trade in `kalshi_trades.jsonl` now
   carries a `decision` object with the entry-time context: strategy, EV,
   `p_real`, `p_base`, `p_market`, CWM, lag/spot confidence, conviction,
   belief vol, time remaining, realized vol, whether it was a lottery, and
   crucially the **portfolio gross and concurrent position count at entry**.
   Loss attribution now needs one file instead of a fragile join.
   Old rows are unaffected and the field is omitted when absent, so every
   existing reader keeps working.

2. **Calibration log** — new `logs/kalshi_calibration.jsonl` pairing predicted
   probability with realized outcome. Kelly sizing assumes `p_real` is
   calibrated and that has never been measured on this system. A reliability
   curve becomes possible after a few hundred settles.

3. **`/losses` ledger** — new `kalshi_bot/risk/loss_ledger.py`, exposed as
   Telegram `/losses`. Breaks losses down by side, entry bucket, asset, and
   window crowding, all risk-normalized. Read-only.

**Files:** `config.py`, `sim_state.py`, `asset_engine.py`,
`risk/loss_ledger.py`, `telegram/handlers.py`, `telegram/formatters.py`

---

## Regime preservation

**What:** `PROFILE_META` registry, `/regimes` command, extended round
fingerprint.

**Description:** Three pieces, all documentation-grade:

1. **`PROFILE_META`** in `runtime_control.py` describes every regime — its
   style, when to use it, its trading history, and whether it has been modified.
   Kept deliberately *separate* from `PROFILE_PRESETS` because `apply_profile()`
   setattr's every key of a preset onto the config object, so prose in there
   would pollute `cfg`. Nothing in `PROFILE_META` affects behavior.

2. **`/regimes`** (alias `/styles`) prints those descriptions in Telegram.
   `/profile <name>` now echoes the description of what you just switched to,
   and the round-start message names the regime and its gross cap.

3. **Extended round fingerprint** — `session_meta._config_levels()` now records
   the Risk Update v1 knobs alongside the existing ones, so any future round's
   regime can be reproduced exactly. Older archives only captured the
   pre-update fields; an audit found just one archived round (R11) carrying a
   full fingerprint at all. `_profile_note()` keeps its original wording
   verbatim for the two legacy regimes so archived rounds stay comparable.

**New Telegram recipes** (`/go engineered`, `/go engineered_micro`, `/go ab`)
were added alongside the originals. `standard`, `micro`, `micro100`, `mid`, and
`big` are untouched.

**Files:** `runtime_control.py`, `session_meta.py`, `telegram/rounds.py`,
`telegram/handlers.py`, `telegram/formatters.py`

---

## What was deliberately NOT done

Each of these was proposed by at least one review and rejected on the data:

| Proposal | Why not |
|---|---|
| Same-window concurrency throttle | Confounded. Windows with 3–4 trades hold 95% of losses **and 99.6% of wins** — the bot almost always trades 3–4 assets, so the throttle cuts winners equally. C1 captures the real effect. |
| Directional YES/NO gross cap | Same-side windows (50.0% WR) and mixed windows (52.0%) show no meaningful edge difference. |
| Near-miss skip or shrink | Does not replicate. Sub-2bps settles won 66.7%. The R29 counterfactual was single-round noise. |
| Hard mid-band conviction/CWM gate | The bucket is profitable. See C6. |
| Disabling DOGE / ETH | 14–20 trades each. Far too small to act on. |
| `alpha_edge` quarantine | Cannot be judged until C7 has collected attributable data. Left enabled on purpose. |
| BTC-residual, Platt scaling, meta-labeling, HMM regime switch | All premature. Revisit after the risk layer has run and the ledger confirms movement. |

---

## Rollout

`max_risk_paper` remains the untouched baseline. Suggested sequence and
promotion rule are in §5 of `PROPOSAL_RISK_UPDATE.md`: keep a change only if max
drawdown improves and sum-of-wins falls no more than ~15% versus baseline over
the same number of rounds.

No live promotion until `engineered_risk` has several paper rounds behind it and
LiveGuard is green.

---

## Addendum — C8: the vault is now real in live (2026-08-10)

**Description.** The vault worked in paper and silently did nothing in live.
Kalshi has one cash balance and no sub-accounts, so the vault was only ever a
bot-side number — and in live, `LiveGuard._apply_sync` overwrote the sizing
bankroll with full Kalshi available cash every 10 seconds, handing skimmed
profit straight back to the trader. This makes the reservation real.

**Why it mattered.** Three failures compounded, all live-only:

1. **No protection.** Any skim was undone by the next sync, so vaulted dollars
   stayed in the pot the bot sized against.
2. **Inflated equity.** `total_equity` is `balance + vault_balance`. With
   `balance` already equal to full Kalshi cash, the vault was counted twice, so
   `/status`, `/vault`, and `/summary` overstated the account.
3. **A weakened drawdown breaker.** `peak_drawdown` is equity-based by default.
   Peak and current were inflated by the same phantom amount, which shrinks the
   ratio — a real 20% drawdown on a $1,000 account carrying a $1,000 phantom
   vault reads as 10%, so `MAX_DRAWDOWN_PCT` tripped at roughly half the
   intended severity. Auto-skim fires inside `record()` on every close and is
   enabled ($200 trigger / $100 skim), so the phantom grew trade by trade.

**Change.** `_apply_sync` now reserves the vault out of the synced bankroll:

```python
vault = max(0.0, sim.vault_balance)
if vault > available + portfolio:      # unbacked claim — withdrawal or losses
    vault = max(0.0, available + portfolio)
    sim.vault_balance = vault          # clamp, logged at ERROR
self.sim.balance = max(0.0, available - vault)   # sizing bankroll
self.sim.kalshi_available = available            # raw Kalshi truth, for reporting
```

Consequences, all verified: skims survive resync; `total_equity` equals real
Kalshi cash instead of double-counting; drawdown reads true; and auto-skim can
no longer run away, because `skimmable_profit` (`balance - starting_balance`)
now falls after each skim and stays down.

**Two guards added.**

- `live_vault_locked` halt when cash exists but the vault owns all of it
  (`tradeable < LIVE_MIN_AVAILABLE_USD`, flat book). Beats spinning on min-size
  rejections. Auto-clears once tradeable recovers, like the other sync halts.
- `bootstrap()` discards a carried-over vault on a fresh live book (zero
  trades). A $1,000 paper vault is not backed by real cash and must not
  reserve against a real account. A live restart mid-book has trades, so a
  genuine live vault survives.

**Backward compatibility.** `vault == 0` makes `tradeable == available`, so
every path reduces to the previous behavior exactly. Paper is untouched:
`LiveGuard` is never constructed under `DRY_RUN` and `maybe_sync()` returns
early. Round 33 was running paper during this change and was unaffected.

**Honest limits.** This is a soft reservation, not custody. The dollars remain
in your Kalshi account and are still exposed to anything that bypasses the bot —
manual trades, another client, or a bug in the sizing path. The only hard vault
is a withdrawal to your bank.

**Files:** `live_guard.py`, `vault.py` (docstring), `sim_state.py` (docstring),
`scripts/verify_live_vault.py` (new — 6 scenarios, no network or live account)

---

## Addendum — C9: consecutive-loss breaker capped at 3 in live (2026-08-10)

**Description.** Requested change: the consecutive-loss circuit breaker should
be 3 for real money. Implemented as a preset value *and* a hard ceiling, because
the preset alone would not have delivered it.

**Why a ceiling and not just a number.** `start_live_safe()` defaults to
`profile="max_risk_micro"`, which inherits `_LEGACY_RISK_KNOBS` with
`MAX_CONSEC_LOSSES = 8`. Editing only the `live_safe` preset would leave the
realistic path to real money running an 8-loss breaker. The ceiling closes that
gap regardless of which profile is selected:

```python
# runtime_control.apply_profile() -> _apply_live_ceilings()
if cfg.DRY_RUN:
    return ""
ceiling = int(getattr(cfg, "LIVE_MAX_CONSEC_LOSSES", 3))
if int(cfg.MAX_CONSEC_LOSSES) > ceiling:
    cfg.MAX_CONSEC_LOSSES = ceiling      # logged at WARNING
```

`run_kalshi_bot` sets `cfg.DRY_RUN = False` from `--live` *before* calling
`apply_profile()`, so `DRY_RUN` is already correct when the clamp runs.

**Changes.**

- `config.py`: new `LIVE_MAX_CONSEC_LOSSES = 3`, in the live-safety block.
- `runtime_control.py`: `live_safe` preset `MAX_CONSEC_LOSSES` 4 → 3;
  `_apply_live_ceilings()` called at the end of `apply_profile()`;
  `/set_consec_losses` upper bound is now 8 in paper but the live ceiling in
  live, so the breaker cannot be loosened mid-round with real money;
  `PROFILE_META["live_safe"]` text updated from "4 losses" to "3 losses".

**Scope: whole book, not per asset.** `PER_ASSET_CIRCUIT_BREAKER` defaults to
`True`, and `is_halted()` returns early on the per-asset branch, so "3" would
have meant three losses on a *single symbol*. Across 8+ symbols the book could
bleed a dozen trades without any streak tripping. Live now forces
`PER_ASSET_CIRCUIT_BREAKER = False` (`LIVE_PER_ASSET_BREAKER`), so three
consecutive losing settles anywhere in the book stop live entries. A win on any
asset still resets the streak — that logic was already global and needed no
change.

**No timed resume.** `COOLDOWN_MINUTES` (10) would put real money back into the
same regime that produced the streak. Under `LIVE_BREAKER_MANUAL_RESUME` the
live breaker raises a sticky `live_halt_reason` instead, which `is_halted()`
checks first and no timer clears. `/resume` clears it through the new
`SimState.clear_breaker_halt()`, which resets the streak, the global cooldown,
and any benched assets — but deliberately leaves LiveGuard halts (balance
divergence, cash too low) in place, since those are not an operator's to wave
off. `process_bot_commands()` takes an optional `sim` so the resume handler can
reach that state; the parameter is optional, so existing callers still work.
Paper keeps the timed cooldown exactly as before.

**One bug found while testing.** The live clamp mutates the shared `cfg`, and
`PER_ASSET_CIRCUIT_BREAKER` was in no preset, so a live clamp leaked into any
profile applied afterward in the same process. Fixed by stating the value
explicitly in `_LEGACY_RISK_KNOBS` (True), `engineered_risk` (True), and
`live_safe` (False, so a paper shadow run of the live candidate behaves the way
real money will).

**Backward compatibility.** Paper is untouched — the clamp returns immediately
under `DRY_RUN`, every paper preset keeps its own breaker (`max_risk_paper` 8,
`max_risk_micro` 8, `engineered_risk` 4), per-asset scope stays on, and the
timed cooldown still applies. Verified in both modes: profile clamping, book-wide
counting, streak reset on a win, halt stickiness past any cooldown, selective
clearing by `/resume`, and unchanged paper semantics.

**Files:** `config.py`, `runtime_control.py`, `sim_state.py`, `kalshi_bot.py`,
`session_meta.py`, `telegram/handlers.py`,
`scripts/verify_live_breaker.py` (new)
