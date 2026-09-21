# P7B market-state dataset builder

Offline data engineering only. The builder imports no runtime engines or scoring
modules and makes no network calls. No production files were modified.

Run from the repository root:

```powershell
py -3 scripts/build_market_state_dataset.py offline_exports/manifest.json --out-dir reports/p7b_review_001
```

The output directory must be new. Inputs remain read-only. Paths containing
`logs`, `sessions`, or `.env` are rejected; output beneath `research_data` is
rejected. No default scan of historical or active data occurs. The explicit
P6C-E session `p6c_d1_validation` is rejected. Obtain independently prepared,
static offline exports outside protected directories for a later authorized run.
No real-data build was performed during implementation.

Example manifest (paths relative to the manifest):

```json
{
  "mode": "paper",
  "session_tag": "historical_export_session",
  "sample_kind": "historical",
  "sources": [
    {"type": "research_v1", "path": "preferred.jsonl"},
    {"type": "research_v2", "path": "boundary.jsonl"}
  ]
}
```

Use `decision` instead of `research_v1` for Population A reconstructed from
ordered decision exports. A build cannot mix these two source types. Use a
separate build for every mode/session/sample-kind cohort. Accepted sample kinds
are development, historical, and prospective (the active P6C-E session remains
excluded). Manifest declarations are explicit provenance, not inferred evidence;
persisted mode, dry_run, session_tag and sample_kind are checked when present.
The operator is responsible for truthful labels on unversioned exports.
Top-level mode/session_tag/sample_kind are manifest-authoritative cohort labels;
persisted_mode, persisted_dry_run and persisted_session_tag record actual row
values and stay null when absent. git_sha and ticker also stay null if absent;
manifest labels never fill them. Stripped or rotated decision exports cannot
prove their original session, runtime revision, mode, or completeness. Selection
and census claims apply only to the supplied observations, not all live ticks.
Outcome joins still require the existing persisted evidence and fail closed.

## Architecture and six artifacts

`kalshi_bot/research/market_state_dataset.py` reads explicit JSONL sources,
validates identity/cohort, selects independent populations, attaches outcome
sidecars, and produces deterministic objects. The CLI resolves manifest-relative
paths and writes four JSONL files and two JSON summaries:

- `preferred_ge_60s.jsonl`: at most one row per asset/window.
- `boundary_60.jsonl`: at most one row per asset/window.
- `population_overlap.jsonl`: asset/window/cohort and A_only/B_only/both only.
- `abstention_census.jsonl`: only windows never qualifying for Population A in
  supplied decisions, with their last recorded recognized early reason,
  provenance, category, and window_count=1; no reconstructed feature state.
- `schema_provenance.json`: ordered source paths and SHA-256 hashes, cohort,
  taxonomy, exact rules and historical semantic limitations.
- `coverage_missingness.json`: population sizes, missing outcome counts and
  per-field observed/missing/structurally-unavailable counts; no scoring.

Population A has distinct source-specific row-level `selection_rule` values,
also exported in schema_provenance.selection_rules:

- A_decision: `last eligible supplied decision observation in manifest-file order then line order, with finite p_real and finite raw time_remaining >= 60`
- A_research_v1: `already-persisted v1 preferred row retained only after verifying finite p_real and finite raw time_remaining >= 60; does not prove last eligible live tick ever generated`

Decision reconstruction replaces the asset/window candidate on each qualifying
supplied observation. It does not sort timestamps, choose closest TTE, freeze
after a sub-60 row, or use a sub-60 fallback. Missing outcomes do not remove
eligible rows. This retains the Project 3 rule within the supplied records.
For research_v1, eligibility is validated on the already-persisted preferred row;
an ineligible row raises ValueError with source path and line, aborting the build
before CLI artifact writing. Duplicate v1 keys also fail. It cannot prove the
last eligible live tick ever generated. The v1 writer's first-crossing freeze
cannot be audited or undone without the original decisions. Documentation and
row-level provenance deliberately make the same limited claim.

Population B accepts only schema_version=2, record_kind=boundary_snapshot and
numeric target_tte=60. It preserves actual_tte, raw_tte, time_remaining (if
recorded), tau_used and target_tte independently. It never reselects a tick using
raw TTE or substitutes model time. Duplicate boundary keys fail rather than
silently choosing a source. Other horizons are ignored.

## Provenance, features and outcomes

Each population row carries source_population, source_type, selection_rule,
source schema_version, asset/window identity, window_id, ticker, timestamp,
capture time, decision_id, source path/hash/line, content-hash-plus-line
observation_id, git_sha, config_profile, action/strategy/reason, declared cohort,
and persisted mode/dry_run/session values. Missing timestamps stay null; the
source hash and line still identify the actual record. A single input-file byte
change changes its hash, intentionally distinguishing source revisions.

Only an explicit contemporaneous field allowlist enters `features`. The known
six raw_features keys remain nested; arbitrary nested fields are excluded to
prevent outcome or portfolio leakage. Estimated response variables are separate
from model and observed market fields in the taxonomy. Nested lag_signal and
response_gap are also estimates. spot_return_1s is never substituted for the lag
tracker input. dist_from_threshold retains the logger's price-minus-strike
semantics (last signal price minus price_to_beat; historical absent-input zero
fallback is preserved), and is classified with market quantities, not model
transforms. kalshi_prob_change_1s is the current YES poll value minus the latest
historical poll at least 1.5 seconds earlier, with the existing zero fallback
when history is insufficient; the name does not guarantee a one-second interval.
It is an observed poll-change series, not a lag-tracker estimate. Nested
raw_features.lag_signal and raw_features.response_gap remain estimated tracker
outputs in their original namespace. No cross-asset factors, state interpolation, book depth, feature
joins or regime bins are produced.

`outcome` is a separate object with its own source provenance. The only accepted
semantic is `spot_vs_price_to_beat`, not official Kalshi settlement. V1 uses its
own recorded outcome. V2 joins sidecars by source type, asset/window and the
validated cohort, with matching git_sha and ticker (including matching absent
historical values). Duplicate outcome identities and inconsistent YES/NO versus
yes_settled fail. Decision exports may use v2 outcomes only when BOTH records
have matching persisted session, non-null revision and ticker, and persisted
mode/dry_run evidence. Otherwise outcome remains null. No decision outcome is
inferred solely from manifest labels. Outcome availability never selects features.

## Missingness and limitations

Null or absent fields defined by the source contract are `missing`. Null or
absent fields outside that contract are `structurally_unavailable`, even when
an export includes a null placeholder. Non-null explicitly persisted allowlisted
values are preserved as observed, including extensions outside the base contract.
The schema summary exports `defined_fields` for decision, research_v1 and
research_v2; builder_schema_version is now 2 to identify the corrected semantics.
Decision availability uses the known logger contract, not the union of all
research columns: target_tte, actual_tte, raw_distance, relative_distance,
log_distance and legacy realized_vol are not defined there. raw_tte, tau_used,
realized_vol_value, quote fields and the other logged fields are defined, so
absence is ordinary missingness. Historical unversioned/stripped exports cannot
prove exactly when a defined field was introduced; the known contract is the
coverage reference, not an invented historical schema version.

Present values, including historical volatility 0.30, remain unchanged.
`schema_provenance.variable_semantics` labels volatility provenance as
`ambiguous_fallback` at the variable level. This is separate from observed/null
availability counts; it is never a fabricated per-row fallback flag or a reason
to replace a value with null. Missing spread remains null. Dislocation zero with
fewer than two fresh venues never becomes an agreement label.

The census contains only windows that never obtain an eligible Population A
observation anywhere in the supplied decision files. After processing all files,
every Population A key is excluded from the reason candidates. Early warmup
followed by eligibility and eligible/traded followed by position_open are both
excluded. This is a supplied-forecast-eligibility census, not proof that a window
never traded outside the supplied records or at sub-60 horizons.

For remaining windows, only recognized recorded reasons with nonfinite/absent
p_real qualify. The last recognized reason in manifest-file order then line
order wins. A later unrecognized null-p_real reason does not replace it. No
numeric value or other state is reconstructed from reason strings. Reasons such
as no_market and stale_price returned before _log_decision leave no decision
record and cannot be represented by this decision-log-derived census. Their
recognition entries apply only if an input actually contains recorded rows;
the builder never infers those unlogged events. A v1-only build has no
census coverage, and an empty census is not evidence of no abstention. Rotated
or stripped exports may omit eligible rows or reasons, limiting conclusions to
the supplied observations.

Malformed JSON, invalid keys, schema conflicts and duplicate sidecars fail the
build. Features must be JSON-serializable finite values for artifact writing;
nonfinite retained feature values cause an explicit serialization error. Inputs
must be static and fit in memory. Writes are not a multi-file transaction; a
serialization/I/O failure can leave a partial new output directory, which must
not be treated as a completed build. Unknown future schema fields are not
implicitly accepted into features. Raw feature coverage describes the object,
not each nested key. Source order is part of reproducibility.

## Validation and review handoff

Synthetic tests in `kalshi_bot/test_market_state_dataset.py` cover exact last-row
selection, uniqueness, target filtering, independent time fields, membership-only
overlap, outcome identity and feature isolation, null spread, low-venue zero
dislocation, ambiguous volatility preservation, reason-only census, mode/cohort
validation, v1 structural missingness, persisted decision-outcome provenance,
nested feature filtering, six-artifact reproducibility and read-only sources.

Correction-pass results (safe synthetic suites only):

- P7B (`kalshi_bot.test_market_state_dataset`): 20 tests passed.
- Research store (`kalshi_bot.test_research_store`): 20/20 checks passed.
- Boundary store (`kalshi_bot.test_boundary_store`): 17/17 checks passed.
- Forecast history (`kalshi_bot.test_forecast_history`): 15/15 checks passed.
- Engine suite: NOT RERUN in this correction pass because its existing paths
  can attempt live API traffic. The prior 106/106 result is not independent
  correction-pass validation.

These suites were run through the guarded Python harness below rather than
unguarded module commands. The approved unsandboxed run allowed Python temporary
fixture directories to work. Dotenv loading was mocked off to avoid reading
local credentials. A Python audit hook prohibited network connection/DNS calls
in the test process. The P7B CLI subprocess imports only the offline builder.
Expected store-fixture warnings exercise malformed input, duplicates and an
intentional temporary write failure; the suites completed successfully.

```powershell
@'
import sys
import runpy
import unittest
from unittest.mock import patch

def offline_audit(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname'}:
        raise RuntimeError('Network prohibited in P7B correction validation')

sys.addaudithook(offline_audit)
with patch('dotenv.load_dotenv', return_value=False):
    suite = unittest.defaultTestLoader.loadTestsFromName('kalshi_bot.test_market_state_dataset')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    for module in ('kalshi_bot.test_research_store', 'kalshi_bot.test_boundary_store', 'kalshi_bot.test_forecast_history'):
        print('\nRUN ' + module, flush=True)
        runpy.run_module(module, run_name='__main__')
'@ | py -3 -
```

Added adversarial tests cover warmup then eligibility across files, eligible
BUY then position_open, never-eligible windows with deterministic last recognized
reason selection, distinct v1 provenance and matching documentation, fail-closed
v1 eligibility, decision schema structural absence versus missing fields,
variable-level ambiguous fallback semantics, and localized taxonomy corrections.
Existing fail-closed decision outcome tests remain in place.

P6C-E, its existing gap, production behavior, source data, runtime settings and
running bot were untouched. No real-data build or performance research was run.
No commit, push, pull, merge, branch switch, or bot restart was performed.
This correction changes only the builder, tests and this documentation within
the original four untracked P7B additions; the CLI is unchanged. Independent
Cursor re-review is the next step.
