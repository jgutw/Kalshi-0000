# P7B market-state dataset builder

Offline data engineering only. The builder imports no runtime engines or scoring
modules and makes no network calls. No production files were modified.

Run from the repository root:

```powershell
py -3 scripts/build_market_state_dataset.py offline_exports/manifest.json --out-dir reports/p7b_review_001
```

An optional observation cohort fence is off unless both bounds are supplied.
Omitting both flags preserves the previous unfenced build. Supplying only one
bound fails. The same keys may be set in the manifest as `start_utc` and
`end_utc`; a CLI value that disagrees with the manifest fails. Example, not a
default:

```powershell
py -3 scripts/build_market_state_dataset.py offline_exports/manifest.json --out-dir reports/p7b_review_001 --start-utc 2026-08-09T00:00:00Z --end-utc 2026-09-01T00:00:00Z
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
  taxonomy, exact rules, historical semantic limitations, and malformed-record
  policy/accounting/quarantine ledger.
- `coverage_missingness.json`: population sizes, missing outcome counts and
  per-field observed/missing/structurally-unavailable counts; no scoring.

## Optional cohort fence

The fence runs after JSON parsing and before Population A/B selection and the
abstention census. It does not change `RULE_A`, `RULE_V1`, or `RULE_B`. With
no bounds, every non-empty supplied record remains eligible for those rules.
With both bounds, membership is `start <= cohort_time < end`. Excluded records
are not rewritten, reordered, or replaced. Original path, SHA-256, and line
numbers stay on the records that remain. Blank lines are not records.

The cohort clock follows the source contract:

- Decision rows, research v1 rows, and v2 `boundary_snapshot` rows use the
  existing observation timestamp: `snapshot_ts` when that key is present,
  otherwise `ts`. The value must be timezone-aware ISO-8601 (`Z` is UTC).
  Naive, missing, or unparseable values fail the build. `window_id_ts` is the
  window identity and is not a substitute clock. A non-null `ts` or
  `capture_time_utc` that parses to a different instant than the primary clock
  fails closed.
- V2 `window_outcome` rows have no observation timestamp. Their clock is
  integer `window_id_ts` as UTC unix seconds. `outcome_ts` and
  `close_time_utc` are not cohort clocks. A non-null observation timestamp on
  an outcome row makes membership ambiguous and fails closed.

`schema_provenance.cohort_fence` records the requested bound strings, whether
the fence was applied, the half-open interval, and record counts before,
after, and excluded, including the same counts per source file. Counts are
cohort-filter counts, not population sizes. `builder_schema_version` stays 2
because selection and availability semantics are unchanged.
`schema_provenance.sources` remains path, type, and hash only.

## Optional historical malformed-record quarantine (P7B-HQ)

Default behavior remains strict: malformed JSON fails the entire build, with or
without a cohort fence. Neither a development label nor a date fence enables
tolerance. Explicitly set manifest `malformed_record_policy` to `quarantine`,
or supply `--malformed-record-policy quarantine`, to opt in. `fail` is the only
other accepted value and the default when omitted. CLI/manifest disagreement
fails before observations are read; an omitted CLI option preserves the
manifest's policy. No file names, line numbers or dates are hardcoded.

This option is restricted to `mode=paper` and `sample_kind=development` or
`historical`. Prospective/live declarations and input paths under `research_data`
are rejected in quarantine mode. The existing protected-path and P6-session
checks remain. The policy does NOT authorize prospective/live research stores
to silently tolerate malformed records. The operator must authorize a static,
immutable-style historical reconstruction; a policy declaration is not proof
that inputs are immutable or that unknown contents have a particular cohort.

Example syntax for a separately approved reconstruction (not run in this pass):

```powershell
py -3 scripts/build_market_state_dataset.py offline_exports/manifest.json --out-dir reports/p7b_reconstruction --malformed-record-policy quarantine
```

Quarantine permits analysis of surviving parseable records from an immutable historical reconstruction. It does not establish that a quarantined record was outside the research cohort.

Only `json.JSONDecodeError` is caught. A failed line becomes a
`malformed_unclassifiable` ledger entry and contributes nothing to A, B,
overlap, census or outcomes. It is never assigned a timestamp, asset, window,
session or cohort membership. Processing resumes at the next physical line;
valid records preserve their original order and source identity. No source is
rewritten, repaired, normalized or supplemented. A partially readable timestamp
inside a broken line is not evidence for cohort inclusion or exclusion.

Encoding failures, successfully parsed non-object JSON, invalid keys, ambiguous
or missing cohort clocks, schema/cohort conflicts and all other validation
failures still abort. Python's existing JSON decoder behavior for parseable
numeric constants is unchanged; this is not a new data-value cleaning policy.
A later fatal error returns no successful build or partial quarantine artifact.

`schema_provenance.malformed_record_handling` contains `policy`, `ledger`,
aggregate `accounting`, `by_source` accounting, and explicit reconciliation
results. The existing six output filenames and population schema version 2 are
retained; metadata is additive. Ledger fields are:

- source_file, source_type and source_sha256 (hash of the complete source bytes);
- source_line (one-based original physical line) and byte_offset (zero-based);
- line_length_chars (decoded parsing text, excluding line terminator and a
  first-line UTF-8 BOM), raw_line_length_bytes and raw_line_sha256;
- parse_error_class, parse_error_message, parse_error_column and
  parse_error_position (decoder-relative character positions);
- quarantine_reason = malformed_unclassifiable.

Raw-line length/hash cover exact bytes including CR/LF/CRLF terminators and any
first-line BOM. No raw text or parser document is copied into the ledger. The
whole-file hash still includes all quarantined bytes and blank lines. A source
path may name a session directory; that is file provenance, not inferred
record-level session identity.

Physical lines use CR, LF or CRLF; a final unterminated line still counts.
Whitespace-only physical lines count as blank. UTF-8 decoding remains strict,
with an optional first-line BOM. Embedded nonphysical Unicode/control line
separators that would require the old text `splitlines()` segmentation fail
closed rather than silently changing record boundaries or assigning misleading
physical line numbers. Normal UTF-8 JSONL with CR/LF/CRLF retains its accepted
selection, original line numbers and hashing behavior.

For every source and the whole successful build, the checked identities are:

```text
physical_lines = blank_lines + candidate_records
candidate_records = parsed_records + quarantined_records
parsed_records = cohort_included_records + cohort_excluded_records
```

Candidates are physical nonblank lines. Counts of parsed/included/excluded
records are not population sizes. Existing `cohort_fence.records_before` counts
successfully parsed records; `records_after` and `records_excluded` retain
included/excluded valid-record semantics. Quarantined records never inflate
any of those three fence counts. Without a fence, all parsed records are
cohort-included and cohort-excluded is zero. No acceptable-malformation rate,
automatic quality verdict, or statistical analysis is introduced.

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
observation anywhere in the supplied decision files. A cohort fence, when
applied, defines that supplied set: rows outside the fence do not create or
remove census entries. After processing all files,
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

Malformed JSON fails by default; only the explicit historical policy above
permits its quarantine. Invalid keys, schema conflicts and duplicate sidecars
still fail the build. Features must be JSON-serializable finite values for artifact writing;
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

## P7B-H cohort fence

The fence is optional infrastructure. It does not run a population build on
historical archives and it does not hardcode an August cohort. Safe offline
validation for this pass:

- P7B (`kalshi_bot.test_market_state_dataset`): 28 tests passed.
- Variable quality (`kalshi_bot.test_variable_quality`): 31 tests passed.
- Research store (`kalshi_bot.test_research_store`): 20/20 checks passed.
- Boundary store (`kalshi_bot.test_boundary_store`): 17/17 checks passed.
- Forecast history (`kalshi_bot.test_forecast_history`): 15/15 checks passed.
- Engine suite: not run. Its paths can attempt live API traffic.

No frozen historical reconstruction was built. Changes are uncommitted.

## P7B-HQ validation

Synthetic/adversarial validation against accepted baseline
`d3bf8bb030863b87e22d36230a0623f2fe4e3b0e`:

- P7B (`kalshi_bot.test_market_state_dataset`): 39 tests passed, including
  11 new quarantine tests.
- Variable quality (`kalshi_bot.test_variable_quality`): 31 tests passed.
- Research store (`kalshi_bot.test_research_store`): 20/20 fixture checks passed.
- Boundary store (`kalshi_bot.test_boundary_store`): 17/17 fixture checks passed.
- Forecast history (`kalshi_bot.test_forecast_history`): 15/15 fixture checks passed.
- Engine suite: not run.

The guarded harness above was used with `loadTestsFromNames` for both P7B and
variable-quality modules (70 tests total), followed by the three fixture suites.
Dotenv loading was disabled and network connection/DNS calls were blocked in the
test process. CLI integration tests used synthetic temporary inputs only.
The existing forecast-history checks are regression fixtures, not a historical
performance analysis. No actual-data P7B build or P7C analysis was run.

The source snapshot, protected paths, prospective cohort and running bot were
untouched. Implementation is left uncommitted and unpushed for independent review.
