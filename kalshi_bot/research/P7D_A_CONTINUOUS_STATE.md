# P7D-A Phase 1: continuous-state infrastructure

Implementation baseline: `5baa73861d5fb369573897f714dbb2c6b41bf658`.
This ticket implements an offline, Population-A-only analyzer using synthetic
fixtures. It does not authorize its first actual-data run. Leave the changes
uncommitted for independent Cursor review. P7F instrumentation is deferred.
The existing `variable_quality.py` CRLF/index anomaly is not modified.

## Input and scientific boundary

The file API accepts an explicit P7B artifact directory, a six-file SHA-256
index with its separately supplied expected hash, and an explicit P7C report
with its separately supplied expected hash. The index must name exactly the six
accepted artifact basenames, with no duplicate or traversal entries. All six
files are hashed; only the metadata and Population A observations are parsed
for P7D. Other files are not analyzed. The P7C report must identify the same
P7B metadata, population hashes and Population A count. Hash verification proves
identity with the supplied checkpoint, not independent correctness of its data.

Only paper development/historical cohorts are accepted. P6C-E, prospective/live
cohorts, and paths under logs, sessions, .env or research_data are rejected.
Excluded cohort metadata is checked before opening population artifacts. There
is no source-store discovery, raw-source reopening, B construction, feature join
or reconstruction of missing observations. Metadata source paths are provenance
strings only. No outcome, action, strategy or reason fields enter calculations.

P7B schema 2/3 row contracts and selection provenance are validated with the
accepted P7C validator. P7D rejects inconsistent input instead of changing
eligibility. Every accepted A observation appears once in a report group and in
that group's observation list, preserving observation ID, asset/window, source
path/hash/line and observation timestamp. No sampling, dropping or imputation
occurs. Finite-pair subsets are statistical denominators, not new populations.

## Architecture and API

- `kalshi_bot.research.continuous_state.analyze(rows, metadata, scale=False)`:
  pure in-memory validation, representation and diagnostics. Does not mutate its
  inputs or access files. Arbitrary outcome content has no effect on its result.
- `load_and_analyze(input_dir, p7c_report, artifact_index, *, p7c_sha256,
  aggregate_sha256, scale=False)`: strict JSON and hash-gated input adapter.
- `write_report(result, output_dir)`: validates strict serialization before
  creating a new output directory; never overwrites an existing directory.
- `scripts/analyze_continuous_state.py`: explicit CLI wrapper. No default cohort
  paths, no Git mutation and no runtime imports.

Future approved CLI shape (not an instruction to run against real data now):

```text
py -3 -B scripts/analyze_continuous_state.py APPROVED_P7B_DIR
  --p7c-report APPROVED_REPORT.json --p7c-sha256 EXPECTED_REPORT_HASH
  --artifact-index APPROVED_INDEX.sha256 --aggregate-sha256 EXPECTED_INDEX_HASH
  --out-dir NEW_DERIVED_DIR [--within-asset-scaling]
```

The operator's actual-data ticket must separately establish the reviewed code
commit and permitted Git exception, if any. The report identifies the accepted
dependency baseline and hashes the implementation/helper files; the baseline
constant is not a claim that the new analyzer existed in that commit.

## States, namespaces and registry

Every represented cell has a `state` and a `value`. States are disjoint:

- `finite`: strict finite numeric observation, excluding booleans/strings;
- `observed`: valid categorical value or object, not a numeric measurement;
- `nonfinite`: source-declared null numeric slot with exact `nonfinite_kind`;
- `missing`: defined field/member absent or null;
- `structurally_unavailable`: source contract does not define an absent field;
- `invalid`: observed value has wrong type/range, or a nested parent is invalid;
- `derivation_unavailable`: no valid finite result for a research-only derivation.

For every variable, `rows = sum(counts.values())`. Nonfinite signs/kinds and
invalid/derivation reasons are counted separately. Source nonfinite values never
receive a fabricated finite rank, value or missingness label. Invalid numeric
values do not enter finite statistics; their observations remain present.

All P7B top-level features remain separate. The six allowlisted raw_features
members use their original dotted namespace. In particular, top-level and nested
`lag_signal` and `response_gap` are never substituted for one another. Venue-map
members use bracketed JSON-string keys (e.g. `per_venue_mids["venue.name"]`),
with explicit path arrays in the registry to avoid collisions. Missing parents,
missing members and invalid parents retain their applicable states/reasons.

`spot_confidence`, `conviction`, `fresh_venue_count` and `target_tte` are marked
discrete. They retain exact masses and are not median/IQR scaled. There is no
jitter, continuous-score interpolation, or invented LOW/NORMAL/HIGH label.

## Row-local derivations

Let S be recorded spot_now and K be recorded price_to_beat:

- `d_logged = S - K`: finite inputs required; negative/zero inputs are
  arithmetically allowed, without declaring them economically valid prices.
- `r_logged = (S-K)/K`: additionally requires nonzero K.
- `l_logged = log(S/K)`: additionally requires S>0 and K>0; evaluated as
  `log(S)-log(K)` to avoid overflow/underflow of the intermediate ratio.

Each has a new research-only name, formula and named inputs. These do not fill
P7B raw_distance, relative_distance or log_distance slots. They are not assumed
equivalent to dist_from_threshold, whose historical logger/reference price and
fallback semantics differ. No volatility-standardized distance is reconstructed.

Unavailable results have explicit `input_not_finite` plus input states,
`zero_strike`, `nonpositive_log_input` or `numeric_overflow_or_domain` reasons.
IEEE floating-point precision still applies, including cancellation/underflow;
these are descriptive transforms of recorded inputs, not recovered measurements.

## Exact metrics and six panels

Each group is one asset/source type/source schema/runtime revision. Unknown
revision remains unknown; it is never merged with a known revision.

Distributions contain the existing linear-interpolation quantile grid
min/p01/p05/p25/p50/p75/p95/p99/max, finite N, unique count and **all** exact point
masses. Sorted points include value, count, mass=count/N and cumulative ECDF.
There are no bins, smoothing or tail exclusions. Categorical frequencies are
reported separately; objects receive coverage accounting, not numeric summaries.

Availability output contains exact state-pattern frequencies and, for every
variable pair, both marginal available counts, joint available N and joint
finite N. Available means finite numeric or valid categorical/object; nonfinite
presence is separately reported and does not count as numerically available.
Equal marginal counts never imply equal observation sets.

The panel registry is fixed in code:

1. distance_time: r_logged/time_remaining and l_logged/time_remaining;
2. probability_threshold: p_base/z_threshold;
3. spot_strike: spot_now/price_to_beat;
4. distance_reference: d_logged/dist_from_threshold;
5. alpha_inputs: alpha_micro with each of the six nested raw_features inputs;
6. lag_namespaces: top-level lag_signal/nested raw_features.lag_signal.

Each pair reports finite-pair N, excluded N/state patterns, Pearson, average-tie-
rank Spearman and explicit correlation status. Scatter points carry observation
IDs and unmodified paired values. Ranks are recomputed on the joint finite set.
Fewer than two pairs, constant inputs and numerical degeneracy produce null
correlations with reasons. The entire panel also reports complete-case N across
all its participating fields; this can differ from each pair's N. For both
pairs and panels, complete N + excluded N = group rows.

## Optional scaling, time and assets

Optional scaling fits median and IQR on finite values within each full
asset/source/schema/revision group. Output records the fitting observation IDs,
N, median, IQR, and one scaled-value/status record per original observation.
Empty samples, zero IQR and numerical overflow are explicit unavailable states.
No dates or other assets enter that group's fit. The fit is retrospective across
the group's full time span, not suitable for causal/predictive deployment. Native
values and the predefined native-unit panels remain unchanged.

Time uses only valid timezone-aware observation timestamps converted to UTC.
Missing, invalid and naive timestamps remain in global/group summaries and are
counted separately, but do not enter daily summaries. Daily distributions retain
states/kinds/reasons and exact masses. Only observed dates exist in the report.
Adjacent-date comparisons expose calendar gaps, left/right finite N, state
counts, right-minus-left median change and maximum empirical CDF distance.
No empty dates are interpolated; no p-values or stable/unstable verdicts exist.

Cross-asset comparisons use unpaired full-sample marginals only within a shared
source/schema/revision. Both timestamp ranges and shared observed dates are
reported. Price levels retain their units; separately named relative/log distance
provides dimensionless comparison where defined. These do not establish
simultaneity, control sampling composition or prove measurement defects.

## Output schema and limitations

`continuous_state.json`, `p7d_schema_version=1`, includes:

- accepted dependency baseline, cohort, Population A N and settings;
- registry and fixed panel definitions;
- groups with observation-level states/derivations, variable summaries,
  availability patterns/pairs, panels/scatter points, optional scaling and time;
- cross-asset comparisons and scientific limitations;
- file-API provenance: all six artifact hashes, aggregate index hash, P7C report
  path/hash, P7B schema and implementation/helper SHA-256 values.

All output uses `allow_nan=False`. Loaded JSON constants Infinity/NaN are rejected.
Source nonfinite states must already be represented by accepted P7B schema 3.
No outcome values or outcome-derived computations appear in the output.

This is an in-memory analyzer. Exact ECDF/scatter/observation output can be large;
pair availability grows quadratically with the variable registry, including
dynamic venue keys. No automatic variable filtering or output sampling reduces
that cost. Input files must be static during a run. Output creation is not a
multi-file transaction, though this tool writes only one report after complete
serialization. Hashes do not establish representativeness or historical runtime
identity. The actual 1,491-row cohort was not opened during this implementation.

## Validation

Synthetic tests cover signed nonfinite states, finite/missing/structural/invalid
accounting, derivation domain and overflow failures, namespace separation,
unequal marginal/joint availability, panel complete cases, ties, constants,
zero-IQR and within-asset fitting, UTC conversion/gaps/unknown timestamps,
unchanged membership, outcome/strategy metadata independence, source-path
non-reopening, strict JSON, hash/linkage tampering, repeatability and the CLI.

Validation uses the P7D, P7B and P7C unittest suites with temporary synthetic
sources, no runtime imports and network connection/DNS calls blocked. On this
Windows sandbox, fixture-directory writes required an approved unsandboxed
execution. The initial sandbox attempt failed on filesystem permissions before
exercising the analyzer. Record the final test totals in the implementation
handoff; do not treat that environment failure as a scientific result.
