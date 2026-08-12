# Kalshi-0000 — Make-Ready Brief for External LLM Review

**Purpose of this file:** Give Claude / Grok / another model enough context to help redesign the trading algorithm **without uploading the full repo**. Paste this whole file (or attach it). It includes: system architecture, strategy math, config, ops surface, today’s loss autopsy, quant framing, and concrete research ideas.

**As-of:** 2026-08-10 (America/New_York).  
**Repo:** local Windows project `Kalshi-0000` — paper-first Kalshi 15-minute crypto Up/Down bot.  
**Author intent:** Engineer a more efficient algorithm by studying losses; get a quant-style research agenda; brainstorm new ideas.

---

## 0. One-paragraph product summary

We trade **Kalshi 15-minute crypto binary markets** (YES = spot finishes above price-to-beat). The bot estimates a structural fair probability `p_base` (GBM digital), adds a **logit-space** microstructure tilt `alpha_micro`, routes among strategies (`lag_arb`, `close_boundary`, `dislocation_reversion`, fallback `alpha_edge`), sizes with **fractional Kelly**, and holds to settlement (early exit exists but is **off**). Default is **paper / DRY_RUN** against live market data. Live path exists with cash sync + fill booking + official settle. Control plane is **Telegram** (+ optional Streamlit dashboard). Active aggressive paper profile is `max_risk_paper` (loose gates, large size, lottery entries allowed — **not for live**).

---

## 1. How a quant would approach “study the losses”

Do **not** start by tuning Kelly or adding more signals. Sequence:

1. **Define the loss object.** Per-trade PnL is not enough. Partition losses by: side (YES/NO), entry price bucket, notional, asset, strategy tag, time-in-window, `|spot−PTB|` at settle (near-miss), and **same-window multi-asset clusters** (portfolio correlation).
2. **Separate edge failure vs risk failure.**  
   - Edge failure: WR too low in a bucket that should have edge (calibration / signal).  
   - Risk failure: WR OK but size / correlation / lottery payoff shape destroys path (R29-style).  
3. **Counterfactuals before new features.** Replay archives with skip rules (already partially implemented in `kalshi_bot/risk/drawdown.py`): skip entry &lt;0.15/0.20, skip near-miss settles, half-size, notional caps. Treat results as **hypotheses**, not proof (selection bias).
4. **Only then** change gates, sizing, or models — one change per paper round, pre-registered metric (e.g. max DD, Calmar, loss$ in multi-asset windows).
5. **Live is a different product.** Paper max_risk optimizes fire-rate × fat right tail. Live should optimize ruin probability and Kalshi-truth bankroll.

**Efficiency** here means: same or better expectancy with **lower loss dollars per unit of edge**, especially cutting **correlated simultaneous losers**.

---

## 2. Loss autopsy — Mon 2026-08-10 (paper)

### 2.1 Session timeline (do not sum ending balances across rounds)

| Round | Profile / start | Trading PnL | Equity end | Trades W/L | Note |
|-------|-----------------|-------------|------------|------------|------|
| R26 | max_risk_paper $2000 | +$3237 | $8537 (vault $3300) | 46 · 33/13 | Overnight monster |
| R27 | max_risk_micro $200 | −$27 | $173 | 23 · 10/13 | Soft red; ETH cold |
| R28 | max_risk_micro $200 | −$92 | $108 | 27 · 10/17 | Morning grind |
| R29 | max_risk_paper $500 | −$179 | $321 | 39 · 16/23 | Worst full round |
| R30 | max_risk_paper $500 | −$125 | $375 | 4 · 0/4 | One window abort |
| R31 | max_risk_paper $500 | +$140 | $2340 (vault $1700) | 22 · 16/6 | Recovery via cheap NO lotteries |
| R32 | max_risk_micro $300 | ~−$115 (in progress) | ~$185 | 9 · 1/8 | Evening micro bleed |

**Aggregate closed trades studied (deduped across those archives):** ~170 trades, **86W / 84L**, sum wins ≈ **+$11.98k**, sum losses ≈ **−$4.14k**. Win rate ~50% but **payoff asymmetry** drives profit on good rounds.

### 2.2 Where loss dollars concentrate

**By asset (loss $ only):** DOGE (−$855) &gt; SOL (−$616) &gt; XRP (−$507) &gt; NEAR (−$453) &gt; BNB (−$435) &gt; ETH (−$422) &gt; HYPE (−$389) &gt; ZEC (−$278) &gt; BTC (−$189).  
BTC is relatively clean on losses; **alt basket + DOGE/XRP** dominate damage.

**By side:** YES losses ≈ −$2.10k (n=49, avg entry ~0.31); NO losses ≈ −$2.04k (n=35, avg entry ~0.36). Overall WR: YES ~46%, NO ~56%. In R29 specifically, YES was catastrophic (−$352) while NO was +$173 — **side × regime** matters.

**By entry bucket (WR / shape):**

| Entry | WR | Comment |
|-------|-----|---------|
| 0.00–0.10 | ~20% | Classic lottery: rare huge wins (R31 SOL NO +$679) + frequent small deaths |
| 0.10–0.20 | ~31% | Still weak WR |
| 0.20–0.40 | ~35–39% | **Largest loss $ mass** (many mid-priced losers) |
| 0.40–0.60 | ~65% | Healthier |
| 0.60–0.80 | ~91% | Favorites work when taken (small sample) |

Lottery `entry &lt; 0.15`: n=12, WR ~17%, **avg win ~$486 vs avg loss ~−$22** — positive expectancy in this sample **because of R31**, but path-dependent and easy to overfit. R29 counterfactual: skipping entry &lt;0.15 improved that round; R31 counterfactual: **same skip would have deleted the recovery**.

**Correlated portfolio risk (critical):** ~**60%** of loss dollars occurred in windows where **≥2 assets lost together**. Worst clusters:

- `2026-08-09 22:00` — DOGE+SOL+XRP ≈ −$633  
- `2026-08-10 16:00` — BTC+ETH+HYPE+NEAR ≈ −$125 (R30 wipe)  
- Multiple afternoon windows with 3-asset −$100 clusters  
- R32 `22:30` — 5-asset loss cluster  

This is a **factor / beta** problem (crypto moves together), not just single-name mispricing.

**Streaks:** max consecutive losses observed in the day series = **8** (exactly at circuit-breaker threshold `MAX_CONSEC_LOSSES=8`).

### 2.3 Counterfactual lessons (from `analyze_counterfactuals`)

- **R29 (losing):** skip entry &lt;0.15 helped a bit; **skip near-miss 5 bps** almost flattened the round (−$15 vs −$179) — many losses were knife-edge settles.  
- **R31 (winning):** skip lottery / near-miss **hurt** — fat right tail was the edge.  
- **Half-size:** roughly halves PnL and DD — pure risk dial.  
- **5% notional cap:** collapses activity to almost nothing under current sizing — too tight as a hard gate; better as soft shrink.

**Quant conclusion from today:** The bot’s PnL is a mixture of (A) rare high-payout dislocations and (B) frequent correlated mid-price scrapes. Efficiency gains likely come from **portfolio risk** and **regime filters**, not from deleting lotteries globally.

---

## 3. Quant research agenda (prioritized)

### P0 — Measurement (1–2 days)

1. **Join trades ↔ entry decisions reliably.** Log `strategy`, `ev`, `cwm`, `lag_confidence`, `conviction`, `p_real`, `p_market`, `time_remaining`, `portfolio_gross` onto every fill/sim trade row (today many BUY actions are sparse in `kalshi_decisions.jsonl`, so offline attribution is weak).  
2. **Loss ledger report** (daily): loss$ by asset / side / entry bucket / strategy / multi-asset cluster / near-miss bps.  
3. **Calibration plots:** predicted `p_real` vs realized YES frequency by decile (reliability diagram). Miscalibration → sizing is lying.

### P1 — Risk engineering (highest ROI vs “new alpha”)

1. **Portfolio correlation throttle.** If N≥2 (or 3) assets already entered this window, require higher `|CWM|` / lower size / block same-direction YES basket. Target: cut the ~60% multi-loss share.  
2. **Same-side gross cap.** Cap total YES notional and total NO notional separately (crypto factor).  
3. **Per-window loss budget.** e.g. stop new entries in a window after −X% of bankroll realized/marked.  
4. **Dynamic Kelly:** scale `KELLY_FRACTION` by recent rolling Sharpe or by `belief_vol` (config already has hooks; max_risk currently sets variance scales = 1.0).  
5. **Asset allowlist by regime.** Soft-disable DOGE/XRP (or thin alts) when lag_confidence low or when BTC realized vol high.

### P2 — Edge hygiene

1. **Treat `alpha_edge` as suspect.** Historical comment: majority of losses under older higher_sharpe when this path fired. Run dedicated A/B: `ALPHA_EDGE_ENABLED=False` for 3–5 paper rounds.  
2. **Raise quality of mid-entry losers (0.20–0.40).** Today that bucket holds most loss $. Ideas: higher `CWM_MIN` when entry in [0.2,0.4]; require conviction ≥3; require spot_confidence ≥0.6.  
3. **Lottery sleeve, not lottery default.** Keep entry &lt;0.15 only as a **small dedicated sleeve** (e.g. max 1–2% bankroll, max 1 concurrent) instead of full Kelly — preserves R31-style jackpots without R29/R30 death.  
4. **Near-miss awareness (research only for entries).** Settles within 2–5 bps are coin flips; optional: reduce size when `|z_threshold|` small late in window (close_boundary already specializes here — audit overlap with lag_arb).

### P3 — Model / signal ideas (new additions)

1. **Cross-asset residual.** Trade asset *i* only on mispricing orthogonal to BTC move (residualize returns / residualize `p_base` vs BTC). Directly attacks correlated multi-loss windows.  
2. **Online Platt / isotonic calibration** on rolling paper settles for `p_real`.  
3. **Meta-labeling:** secondary classifier “will this lag_arb fire be a scratch/near-miss?” using time_left, spread, dislocation, belief_vol — size = Kelly × P(meta).  
4. **Spread / depth aware EV:** subtract effective half-spread + Kalshi fee into `ev` before Kelly (if not already fully in fill model).  
5. **Regime HMM / simple switch:** trending vs mean-revert 15m; disable dislocation_reversion in strong trends; tighten lag_arb when venues disagree (high dislocation).  
6. **Time-of-day seasonality table** from archives (UTC hour × WR) — soft prior on size.  
7. **Exit optionality:** re-enable early exit only as **risk sell** when portfolio DD intrawindow exceeds threshold (not as alpha chase).  
8. **Shadow book:** always log what `strict_gates` profile would have done alongside max_risk — free continuous A/B.

### P4 — Process / ops

1. Pre-register paper experiments (`experiment_id` in `session_meta`).  
2. Stop restarting bankroll mid-day without labeling “fresh paper unit” — confuses equity learning.  
3. Before any live size-up: require LiveGuard green + min cash + no open positions + profile `max_risk_micro` or tighter.

---

## 4. Concrete “new additions” shortlist (build candidates)

| Idea | Type | Why (from losses) | Complexity |
|------|------|-------------------|------------|
| `PORTFOLIO_SAME_WINDOW_MAX` + direction cap | Risk | 60% loss$ in multi-asset loss windows | Low |
| `LOTTERY_SLEEVE_MAX_PCT` (entry&lt;0.15) | Risk | Dual nature of lotteries (R31 vs R29/R30) | Low |
| Disable / quarantine `alpha_edge` | Edge | Prior round comments + sparse but toxic path | Low |
| Mid-band entry quality gate (0.2–0.4) | Edge | Largest loss mass | Low |
| BTC-residual feature + gate | Edge | Factor correlation | Med |
| Trade-row decision snapshot | Data | Attribution currently weak | Low |
| Daily loss ledger → Telegram `/losses` | Ops | Forces loss review habit | Low |
| Shadow strict profile | Research | Continuous counterfactual | Med |
| Calibration layer on `p_real` | Model | Kelly assumes calibrated p | Med |
| Meta-label size multiplier | Model | Cut near-miss scrapes | Med-High |

---

## 5. System architecture (all aspects)

### 5.1 Entry points

| File | Role |
|------|------|
| `run_kalshi_bot.py` | Main bot CLI → `kalshi_bot.kalshi_bot.main` (`scan` / `run` / `status`) |
| `run_safe_live.py` | Live start sized from Kalshi available cash |
| `run_telegram_bridge.py` | Telegram long-poll + alerts |
| `run_telegram_watchdog.py` | Keep bridge alive |
| `run_overnight_watchdog.py` | Keep paper bot alive |
| `dashboard_app.py` | Streamlit ops UI |

### 5.2 Runtime loop (mental model)

```
Spot venues (Coinbase primary, OKX/Binance/Gemini/Kraken) 
  → SyntheticSpotEstimator (mid + confidence + dislocation)
Kalshi REST (YES mid, markets, PTB)
  → per-asset AssetEngine.make_decision()
      structural_prob → p_base
      MicroAlphaModel → alpha_micro
      p_real = sigmoid(logit(p_base) + alpha_micro)
      StrategyRouter → lag_arb / close_boundary / dislocation (+ veto)
      optional alpha_edge if router WAIT
      gates (edge, lag, CWM, conviction, entry price, vol, portfolio cap…)
      kelly_binary → size
      execute paper sim OR live IOC
  → hold to settle (paper: spot vs PTB; live: Kalshi result)
  → SimState.record + vault skim rules + circuit breakers
Telegram / runtime_control.jsonl for pause-stop-profile
Archives → sessions/session_YYYY-MM-DD_HHMM/
```

### 5.3 Assets (all enabled in code today)

BTC, ETH, SOL, XRP, DOGE, BNB, HYPE, NEAR, ZEC — series tickers `KX{SYM}15M`.

### 5.4 Strategy details

**Structural probability (`prob_model.structural_prob`):** GBM-style digital \(P(S_T &gt; S_0)\) → `p_base`. Features also expose `z_threshold`, `mispricing_base = p_base − p_market`, **CWM** = mispricing × spot_confidence × lag_confidence.

**Micro alpha (`models/micro_alpha_model.py`):** weighted z-scores of order-book / flow features (OBI, OFI/Hawkes, microprice deviation, trade-sign autocorr, lag_signal, response_gap). **Important invariant in code comments:** never blend probabilities in probability space — only logit(`p_base`) + alpha.

**Router (`strategy/strategy_router.py`):**

- `lag_arb`: primary; needs lag, fresh quote, tight spread, low dislocation, CWM, time left &gt;60s; side = sign(CWM).  
- `close_boundary`: last ~15–120s; large `|p_base−p_market|`, `|z|`, tight quote, high spot conf.  
- `dislocation_reversion`: fade single-venue outlier; also **veto** opposing candidates.  
- Special: if ≤120s and lag+close agree → prefer close_boundary.  
- `alpha_edge` (in `asset_engine.py`): if router WAIT and `|p_real−yes| ≥ ALPHA_EDGE_MIN` and lag ≥ `ALPHA_EDGE_LAG_MIN`.

**Side for sizing:** from EV sign (`BUY_YES` / `BUY_NO`), not blindly from router label.

**Exits:** default hold-to-binary. `EARLY_EXIT_ENABLED=False` currently.

### 5.5 Sizing & risk knobs (`TradingConfig` defaults = max_risk_paper)

```
KELLY_FRACTION = 0.50
MIN_EDGE_PCT = 0.01
MAX_POS_PCT = 0.08
PORTFOLIO_GROSS_CAP = 0.30
MIN_TRADE_USD = 5
MAX_CONSEC_LOSSES = 8, COOLDOWN_MINUTES = 10, PER_ASSET_CIRCUIT_BREAKER = True
MAX_DAILY_LOSS_PCT = 0.40
MAX_DRAWDOWN_PCT = 0.50 (equity includes vault when DRAWDOWN_USE_EQUITY)
MIN_ENTRY_PRICE = 0.02, MAX_ENTRY_PRICE = 0.98   # lotteries allowed
LAG_CONFIDENCE_MIN = 0.12, CWM_MIN = 0.015
ALPHA_EDGE_ENABLED = True, ALPHA_EDGE_MIN = 0.04, ALPHA_EDGE_LAG_MIN = 0.15
MIN_CONVICTION = 2
SPOT_CONFIDENCE_MIN = 0.30
ENTRY_VAR_*_SCALE = 1.0, BELIEF_VOL_*_SCALE = 1.0  # no lottery shrink
DRY_RUN = True, SIM_BALANCE = 2000
```

**Runtime presets** (`runtime_control.PROFILE_PRESETS`):  
- `max_risk_paper`: Kelly 0.50, MAX_POS 8%, portfolio 30%, min trade $5  
- `max_risk_micro`: Kelly 0.50, MAX_POS 10%, portfolio 35%, min trade $2  

Telegram recipes: micro/$200, micro100, standard/$500, mid/$1000, big/$2000.

**Vault:** skim profits from trading balance into `vault_balance` so sizing bankroll ratchets down after wins (path dependency on “equity vs trading balance”).

### 5.6 Live safety (must not regress)

- `LiveGuard`: sync Kalshi available cash; halt on divergence / sync fail / low cash; refuse live start if balance unreadable.  
- Fills: book actual IOC fill price/fees/count — not intended quote alone.  
- Settle: prefer Kalshi official `result`; spot fallback marked unofficial.  
- `safe_live.start_live_safe`: preflight API, cash, open positions, archive stale session.  
- **Do not use max_risk_paper live** (config comment). Prefer micro + LiveGuard.

### 5.7 Telegram surface (phone trading)

Read: `/status` `/live` `/risk` `/positions` `/trades` `/windows` `/summary` `/account` `/why` `/router` `/sizing`  
Round: `/go` `/start_round` `/presets` `/history` `/stop` `/pause` `/resume`  
Live: `/resume_live` `/start_live`  
Knobs: `/set_kelly` `/set_max_pos` `/set_min_trade` `/set_portfolio_cap` `/profile`  
Vault: `/vault` `/take_cash` `/vault_auto` …

Bridge: `run_telegram_bridge.py` + watchdog. Heartbeat stale alerts when session active.

### 5.8 Data artifacts

**Live `logs/`** (archived to `sessions/session_YYYY-MM-DD_HHMM/`):

- `kalshi_sim.json` — balance, streaks, vault, history  
- `kalshi_trades.jsonl` — closed trades (pnl, entry, exit, side, asset, window_id, equity…)  
- `kalshi_decisions.jsonl` — high-volume WAIT/BUY diagnostics (p_base, p_real, CWM, lag, strategy, ev…)  
- `kalshi_windows.jsonl`, `kalshi_events.jsonl`, `kalshi_features.jsonl`  
- `session_meta.json` — tag, config_levels, prior round summary  
- `vault_*.json*`, `runtime_control.json`, `open_positions.json`, `bot_heartbeat.json`  
- Excel: `reports/kalshi_rounds.xlsx`

### 5.9 Important file map (~25)

| Path | One-liner |
|------|-----------|
| `kalshi_bot/config.py` | Assets + TradingConfig single source of truth |
| `kalshi_bot/kalshi_bot.py` | Async orchestrator + CLI |
| `kalshi_bot/asset_engine.py` | Decide / execute / settle / early-exit |
| `kalshi_bot/signal_engine.py` | Micros, Kelly, vol/variance scalars |
| `kalshi_bot/prob_model.py` | Structural p_base |
| `kalshi_bot/lag_tracker.py` | Spot↔Kalshi lag |
| `kalshi_bot/features/threshold_features.py` | z, mispricing, CWM |
| `kalshi_bot/models/micro_alpha_model.py` | Logit alpha_micro |
| `kalshi_bot/strategy/strategy_router.py` | Rank + veto |
| `kalshi_bot/strategy/lag_arb.py` | Primary strategy |
| `kalshi_bot/strategy/close_boundary.py` | Late window |
| `kalshi_bot/strategy/dislocation_reversion.py` | Fade + veto |
| `kalshi_bot/sim_state.py` | Bankroll + halts |
| `kalshi_bot/live_guard.py` | Live cash truth |
| `kalshi_bot/safe_live.py` | Live preflight spawn |
| `kalshi_bot/kalshi_client.py` | REST / IOC / settle |
| `kalshi_bot/vault.py` | Profit skim |
| `kalshi_bot/runtime_control.py` | Pause/stop/profile queue |
| `kalshi_bot/session_meta.py` | Tags + archive |
| `kalshi_bot/recorder.py` | Events/features JSONL |
| `kalshi_bot/risk/drawdown.py` | DD + counterfactuals |
| `kalshi_bot/data/synthetic_spot.py` | Multi-venue mid |
| `kalshi_bot/telegram/handlers.py` | Commands |
| `kalshi_bot/telegram/bridge.py` | Poll loop |
| `kalshi_bot/telegram/rounds.py` | Paper round recipes |

### 5.10 Env / secrets (do not paste into chats)

`.env` holds `KALSHI_API_KEY`, `KALSHI_PRIVATE_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, etc. **Never include secrets in shared briefs.**

---

## 6. Known historical lessons already in code comments

1. Lottery tickets (entry &lt;0.15) caused concentrated losses in some rounds (R12) — hence Excel metric `pct_entry_lt_0.15` and counterfactual helpers.  
2. `alpha_edge` was majority of losing entries under older “higher_sharpe” profile — still enabled under max_risk.  
3. max_risk_paper intentionally allows lotteries and disables variance shrink — **paper only**.  
4. Hawkes decay was historically too fast (fixed).  
5. Live settle fallback to spot is marked unofficial.  
6. README may be stale vs `config.py` (profile name / XRP enabled).

---

## 7. Prompt you can give Claude / Grok with this file

> You are advising on a Kalshi 15m crypto binary trading system described in the attached MAKE_READY brief.  
> Goals: (1) reduce loss dollars and drawdowns without killing the fat right tail that made R26/R31 work; (2) propose a ranked experiment plan; (3) sketch concrete config/code changes for P0–P1 items (portfolio window cap, lottery sleeve, alpha_edge quarantine, mid-band gates); (4) call out overfitting risks.  
> Do not invent APIs or files that contradict the brief. Prefer risk engineering before new signals.  
> Output: executive diagnosis, experiment matrix (hypothesis / metric / duration), and a minimal patch plan.

---

## 8. What we want from the external model (checklist)

- [ ] Agree / challenge the “60% multi-asset loss$” diagnosis  
- [ ] Design portfolio throttle math that won’t delete R31  
- [ ] Lottery sleeve design (caps, concurrency, when to allow)  
- [ ] Whether to kill or quarantine `alpha_edge`  
- [ ] Calibration + meta-label roadmap  
- [ ] Live-safe profile distinct from paper max_risk  
- [ ] Pseudo-code or patch-level guidance mapped to files in §5.9  

---

## 9. Operator context (non-code)

- User often runs **entirely from Telegram** with PC plugged in.  
- Live bankroll was previously desynced from sim (fixed via LiveGuard) — any redesign must keep Kalshi cash as live source of truth.  
- Kalshi available cash may be very low (~$2) when not funded — paper is the research lab.  
- Today’s R32 was active and losing on micro while this brief was written — treat as live experiment noise unless archived.

---

*End of make-ready brief. No secrets included. For code-level edits, open the repo files listed in §5.9.*
