# Trade Attribution — Phase 1 inventory

**Scope:** source-only inventory for a later, separately approved persistence
change. This is a candidate map for research questions, **not** a 40-column
target schema. No decision, sizing, order, or logging behavior was changed.

## Current recording surfaces

- `kalshi_decisions.jsonl` is the canonical per-price-update decision row from
  `AssetEngine._log_decision`.
- `kalshi_features.jsonl` is already an `EventRecorder` copy of that decision
  row, marked `_type: "feature"`; it is not an independent feature pipeline.
- `kalshi_events.jsonl` receives only the currently submitted raw event shape
  (here, the Kalshi price tick). The recorder adds `ts_local` as a Unix epoch.
- C7 attaches `DECISION_SNAPSHOT_FIELDS` from the entry decision as a nested
  `decision` object on each closed `kalshi_trades.jsonl` row. This is the
  strongest current trade-attribution surface.
- `kalshi_fills.jsonl` records successful open fills only; `kalshi_windows.jsonl`
  records one end-of-window summary based on the *last* decision row, not
  necessarily the entry decision.

## Candidate research fields

The Phase 2 path for every `add` row is to extend the existing decision dict,
`_log_decision` (and therefore its recorder clone), and selected C7 entry
snapshot fields. It must not add a parallel logger or make the full candidate
list mandatory.

| Candidate field / research question | Exists in memory? (file + symbol) | Persisted today? | Timestamp / join key | Capture without changing decisions? | Joinable to a closed trade today? |
| --- | --- | --- | --- | --- | --- |
| `asset` | `asset_engine.py: AssetEngine.spec.symbol` | decisions, features clone, fills, windows, trades | `asset` + `window_id` / ticker | already | Yes; non-unique across decision ticks |
| `ticker` / `market_ticker` | `AssetEngine._ticker`; `OpenPosition.market_ticker` | all primary rows (trade key is `ticker`) | ticker | already | Yes; strongest common identifier |
| `window_id_ts` | `AssetEngine._window_id` (Unix window boundary) | decisions, features clone, windows; fills numeric | `asset + window_id_ts` | already | Not directly in trades (trades use formatted string) |
| formatted `window_id` | `_fmt_window_id`; `OpenPosition.window_id` | decisions, windows, trades; different types in fills | formatted UTC minute string | already | Yes for decisions/trades, but normalize first |
| decision event time | `_log_decision` call time | decisions/features `ts` UTC ISO; recorder also `ts_local` epoch | `ticker + asset + window + ts` | already | No deterministic entry link; many decisions/window |
| entry / fill time | `OpenPosition.entered_at` | fills `ts` UTC ISO; not in closed trade | `ticker + asset + window` | add C7 `entry_ts` from position/decision if research needs it | Indirect only; no closed-trade entry timestamp today |
| close / settlement time | `SimState.record` | trades `ts` from `datetime.now().isoformat()` | closed-trade `ts` | already | Yes within the trade row; timezone is ambiguous |
| decision `action` | decision dict / `_wait` | decisions + features clone | decision row | already | Only indirectly; C7 does not copy action |
| WAIT / rejection `reason` | `_wait` and strategy results | decisions + features clone; windows keep last reason only | decision row | already | No entry-level closed-trade link today |
| selected `strategy` | router signal / `signal_strategy` | decisions/features, fills, windows, C7 trade snapshot | entry decision copied by C7 | already | Yes, directly on C7 trade snapshot |
| strategy score / `ev` | `make_decision` | decisions/features, C7 snapshot | entry decision | already | Yes, C7 |
| side actually filled | `_execute` `side`; `OpenPosition.side` | fills and closed trades | `ticker + asset + window` | already | Yes |
| requested size | decision `size_usd` | C7 snapshot only | entry decision | already | Yes, C7 |
| actual contracts / cost / fees | `_execute` fill result; `OpenPosition` | fills and trades | `ticker + asset + window` | already | Yes; fill and close rows can be related by those keys |
| order id | `OpenPosition.order_id` | fills only | order id (not copied to trade) | add to C7 only if an external-order reconciliation question requires it | No; absent from closed trade |
| `p_market` / raw YES price | `yes_price_raw`, `LogitPriceTracker` | decisions/features; fills `p_market`; C7 snapshot has `p_market` | entry decision / tick time | already | Yes, C7 has smoothed `p_market`; raw price is not C7 |
| `p_base` | `ThresholdFeatures.p_base` | decisions/features, windows, C7 | entry decision | already | Yes, C7 |
| `p_real` | logit blend in `make_decision` | decisions/features, windows, C7, calibration | entry decision | already | Yes, C7 |
| `alpha_micro` | `MicroAlphaModel.compute` | decisions/features, C7 | entry decision | already | Yes, C7 |
| six `raw_features` | `make_decision.raw_features` | decisions/features as nested object; not C7 | decision row | add selected whole object or fields to C7 only after review | Not directly; requires ambiguous decision join |
| `obi`, `ofi_hawkes`, `microprice_dev`, `trade_sign_autocorr` | `AssetSignalEngine` private signal methods | inside decision `raw_features`; `signals` has related but differently named/rounded values | decision row | already for decision rows | No direct closed-trade copy |
| `lag_signal`, `response_gap` | `KalshiLagTracker` properties | `lag_signal` separately on decision rows; `response_gap` only inside `raw_features`; neither C7 | decision row | add candidates to C7 if selected | No direct closed-trade copy |
| **`lag_zscore`** | **No named object.** `MicroAlphaModel.compute` standardizes each configured feature transiently. | no | n/a | report absent; define computation first if ever desired | No |
| micro-alpha feature means / stds | `MicroAlphaModel.config.feature_means/feature_stds` | no | current process state only | add only if explaining alpha normalization is a chosen question | No |
| `bias` / `uncertainty` | `AssetSignalEngine.get_bias_score` | decisions/features (`bias`); `uncertainty` is calculated but omitted | decision row | add `uncertainty` to existing decision/C7 path if selected | `bias`: no C7; `uncertainty`: no |
| `conviction` | `AssetSignalEngine.conviction` | decisions/features and C7 | entry decision | already | Yes, C7 |
| `signals` components | `AssetSignalEngine.get_components` | decisions/features only | decision row | add selected fields to C7 only if useful | No direct closed-trade copy |
| `realized_vol` | `AssetSignalEngine.get_realized_vol` | C7 snapshot; decision output only on successful entry | entry decision | already for trade; add decision-row persistence if analysis needs WAITs | Yes for entered trades |
| `belief_vol` / adjusted edge | `LogitPriceTracker.belief_vol`, `.adjusted_min_edge` | `belief_vol` decisions/features/C7; `min_edge` C7 but omitted from decision log | entry decision | add `min_edge` to decision row if needed | Both yes in C7 |
| `entry_for_size` / `is_lottery` | sizing block in `make_decision` | C7 only | entry decision | already | Yes, C7 |
| portfolio gross / concurrent opens | `make_decision` / `SimState.gross_open_exposure` | C7 only | entry decision | already | Yes, C7 |
| spot now / start / price-to-beat | `SyntheticSpotEstimator`; `AssetEngine._price_to_beat` | decisions/features; trades retain price-to-beat and exit spot | ticker/window | already | Partly: closed trade has PTB, not entry spot |
| price-to-beat provenance | `AssetEngine._price_to_beat_source` | decisions/features, C7 | entry decision | already | Yes, C7 |
| `z_threshold` / base mispricing / CWM | `ThresholdFeatures` | all three on decisions/features; `z_threshold` and CWM in windows/C7; base mispricing not C7 | entry decision | `mispricing_base` needs C7 only if selected | z/CWM yes; base mispricing no |
| synthetic spot quality | `SyntheticSpotEstimator.confidence`, `source_count`, `staleness`, `lead_source` | confidence decisions/features/C7; others no | decision time | add selected values to existing rows if selected | confidence yes; others no |
| venue mids / staleness | `SyntheticSpotEstimator.venue_mids`, `.venue_staleness` | passed to strategy snapshot only; not logged | decision time | add the dictionaries (or chosen normalized fields) to existing decision/C7 path | No |
| **`venue_1`–`venue_4`** | **No named objects.** Venue data is keyed dictionaries, with variable exchange names/count. | no | n/a | report absent; do not invent ordinal columns | No |
| `dislocation` / one-venue-outlier state | estimator `.dislocation`; `DislocationReversionStrategy.compute_signal` locals | not persisted as a named decision field; strategy reason may imply veto | decision time | add existing scalar/diagnostic only if selected | No |
| Kalshi quote quality | `_last_price_age`, `_last_kalshi_spread` | spread in windows only if supplied (current decision dict does not return it); neither persist reliably on decision rows | decision time | add to existing decision/C7 path if selected | No |
| 1-second spot / Kalshi response | `_spot_return_1s`, `_kalshi_prob_change_1s` | strategy snapshot only | decision time | add existing scalars to decision/C7 path if selected | No |
| time remaining / threshold distance | `_time_remaining_secs`, `_wait.dist_from_threshold` | time remaining decisions/features/C7; distance decisions/features only | entry decision | add distance to C7 if selected | time remaining yes; distance no |
| router diagnostics / veto | `StrategySignal.diagnostics`, `StrategyRouter.veto_check` | decisions/features as nested `diagnostics` | decision row | add only chosen stable diagnostics to C7 | No direct closed-trade copy |
| outcome / P&L / exit spot | `_resolve_position`, `SimState.record` | trades; windows; calibration subset | `ticker + asset + window` | already | Yes, closed-trade record is authoritative for paper close; live settle source is not persisted there |
| settlement provenance | `_resolve_position.settle_source` | window fill memory only; not trade or window summary row | ticker/window | add to C7/trade only if paper-vs-live settlement research requires it | No |
| **MAE / MFE** | **No running extrema or named fields.** Only current early-exit MTM/loss fraction is computed. | no | n/a | report absent; requires a defined holding-period sampling rule, not a passive column | No |
| **acceleration** | **No named object.** There are one-second returns/changes, but no second derivative. | no | n/a | report absent; define horizon/units first | No |

## Join and time normalization findings

1. **C7 is the preferred closed-trade attribution join.** It avoids an
   entry-decision join by copying selected decision values when the position is
   opened. It cannot recover fields that were never copied.
2. **Decision-to-fill is not deterministic today.** Multiple decision rows are
   emitted for one `asset × ticker × window`; fills contain no decision ID or
   decision timestamp. `asset + ticker + window` is useful but cannot prove the
   exact triggering tick.
3. **Window summaries are not entry attribution.** `_append_window_summary`
   reads `_last_decision`, which can be a later WAIT tick after the entry.
4. **Time representations differ:** decision/fill/window rows use UTC ISO;
   recorder adds Unix-epoch `ts_local`; closed-trade and calibration rows use
   naive `datetime.now().isoformat()`. `window_id_ts` is a UTC-aligned epoch in
   decisions/windows, numeric in fills, and absent from trades; trades instead
   use the UTC-formatted `window_id` string. Phase 2 should normalize a chosen
   key/time representation before claiming exact event ordering.
5. **Rotation limits research retention.** decisions/features retain 10,000 of
   20,000 rows; events retain 5,000. Any later study requiring full rounds must
   account for that independently of field selection.

## Phase 2 boundary (not implemented)

Choose a small gold list from the rows above, then persist it on the existing
decision row (therefore its existing `kalshi_features.jsonl` clone) and where
needed on C7's nested `decision` snapshot. Keep the old schema for the current
round; begin any new fields only after the next deliberate process start.
