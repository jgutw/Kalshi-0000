# P7C-A variable validation tooling

Accepted P7B research-tooling baseline:
`5116cdec49e47ddca65c34f1105d62678c4a9f38`.
This pass implements and tests the analyzer only. No real-data report was
generated, no variable has been declared usable, and no candidate/conditional
status is assigned. P6C-E remains independent at its frozen baseline `61d7b82`.
Changes are left uncommitted for independent review. Per the operator's latest
instruction, no Cursor review ticket is authored by the implementer.

## Architecture and inputs

- `kalshi_bot/research/variable_quality.py`: validation, descriptive diagnostics,
  input hashing and writing. Standard library plus accepted offline P7B helpers;
  no runtime, dotenv, client, scoring or network imports.
- `scripts/analyze_variable_quality.py`: explicit-input CLI.
- `kalshi_bot/test_variable_quality.py`: synthetic/adversarial tests built from
  accepted P7B fixtures, plus hand-computed statistical references.

The loader reads exactly three files from one supplied directory:
`schema_provenance.json`, `preferred_ge_60s.jsonl`, and `boundary_60.jsonl`.
Both population files are required, even if empty. It does not rebuild P7B,
reopen original source exports, consult the research store, read the overlap
index or abstention census, or use a running bot. Source provenance is retained
as declared metadata, not reverified by reading original data.

For a later independently approved offline artifact cohort:

```powershell
py -3 scripts/analyze_variable_quality.py offline_exports/p7b_cohort --out-dir reports/p7c_a_cohort
```

Optional age-policy checks:

```powershell
py -3 scripts/analyze_variable_quality.py offline_exports/p7b_cohort --out-dir reports/p7c_a_age_check --quote-stale-seconds 5 --venue-stale-seconds 10
```

These example numbers demonstrate syntax, not recommended cutoffs. Thresholds
must come from the operator's measurement policy. Without them, ages are
summarized without stale labels. No default or fitted thresholds are used.
Negative, nonfinite or boolean thresholds fail validation.

## Output and grouping

One `variable_quality.json` is produced. It contains analyzer schema version,
research-tooling baseline reference, cohort, explicit settings, P7B provenance,
SHA-256 hashes and byte lengths of the three supplied files, population row
counts, grouped diagnostics, unpaired asset comparisons, and methodology notes.

Groups separate population, source type, source schema, asset and recorded
runtime git_sha. Missing revisions form an explicit null group. Runtime and
research-tooling revisions are not interchangeable. No Population B features
are attached to A; no unequal observations are represented as synchronized.
Mode, session and sample-kind must already agree throughout the input.
Empty populations have zero counts and no fabricated variable groups; absence
of observations is not a claim about coverage in an underlying population.

## Exact coverage and distribution metrics

Coverage retains P7B `observed`, `nonfinite`, `missing`, and `structurally_unavailable` states
and checks them against the accepted source contract. Counts and two fractions
are supplied: observed/all group rows and observed/nonstructural group rows.
The nonstructural denominator is explicit; a zero denominator yields null.
Observed does not mean numerically valid. Valid-value and invalid-observed-value
counts distinguish measurement types from persisted availability.

Strict finite JSON numbers alone enter numeric calculations. Strings, booleans,
nonfinite values and integers too large for finite float representation are not
silently coerced. No finite outlier is removed. Negative counts are descriptive:
negative distance or returns are not inherently bad measurements.

Numeric summaries include valid n, unique count, negative count, min/max,
p01/p05/p25/p50/p75/p95/p99, exact zero count and fraction of valid numbers, and
the five most frequent exact values with counts and valid-value fractions.
Quantiles linearly interpolate at `(n-1)*q`. Point-mass ties sort by numeric value
ascending after frequency descending. No rounding, bins or imputation is used.
`constant_among_valid` means one unique value in this sample, including a sample
of size one; it is not a usability verdict. No valid numbers yields null
quantiles and null constancy. Categorical fields have exact value counts.

## Measurement diagnostics and nested values

Volatility equality to 0.30 is counted without identifying fallback rows.
Ambiguous fallback remains variable semantics and the value stays in the
distribution. There are no per-row fallback flags.

Dislocation context reports pair availability, zero dislocation with fewer than
two valid fresh venues, and zero dislocation with unknown/invalid venue count.
Venue count validity here requires a nonnegative integer. Invalid count-domain
values are separately counted. Neither low-venue nor unknown-count zero is
called agreement. Missing quotes/spread remain absent. Quote-null aggregates
use only rows whose source contract defines all four fields: yes_bid, yes_ask,
no_bid and no_ask. `rows_with_quote_fields_in_source_contract` reports this
eligible-row denominator explicitly. `rows_with_all_four_quotes_null` counts
the subset with all four values null; `rows_with_spread_but_all_four_quotes_null`
counts the further subset with a non-null spread. Both counts exclude structural
absence and use that same eligible-row universe; neither is a fraction over all
group rows. Decision and v2 rows qualify, while research_v1 rows do not. Thus a
v1 group has denominator zero and both counts zero, meaning not applicable,
not evidence that quote availability was good. Field-level availability is
unchanged. Even for eligible rows, four null quotes do not prove the exchange
had no book, and a supplied spread is not used to reconstruct a book. Generic
negative counts also expose negative spreads and negative ages.

Quote ages and per-venue ages use recorded seconds. Optional above-threshold
counts use strict `>` and only valid nonnegative ages, with that denominator
reported. Negative ages remain in numeric distributions but are excluded from
the valid-age denominator. No stale/nonstale conclusion is made without a
supplied threshold. Fresh venue counts are never recomputed.

Maps have parent availability, invalid-object and empty-object counts. Venue
members are only the union of names observed in valid maps within a group;
there is no assumed full exchange universe. Nested coverage uses all group
rows: parent structural absence remains structural; missing parents or members
count missing; invalid parents have a separate `invalid_parent` count. This
does not establish why a venue is missing. No venue-member summaries are
fabricated if no member names exist. `per_venue_mids` records fresh mids, whereas
`per_venue_staleness` records ages of available venue observations, including
ones no longer fresh. These series are not equated.

`raw_features` uses the six accepted names. Nested `lag_signal` and
`response_gap` stay classified as estimated tracker outputs. Parent structural
absence propagates to these named children. The P7B taxonomy is otherwise
preserved: price-minus-strike `dist_from_threshold` is a market quantity and
`kalshi_prob_change_1s` is observed YES poll change, not a tracker estimate or
guaranteed one-second interval.

## Redundancy diagnostics

Every unordered pair of top-level numeric fields is reported within its
population/source/schema/asset/revision group. Only strict numeric values on
the same supplied row enter a pair. Every pair reports group n, paired valid n,
paired fraction, Pearson correlation and Spearman rank correlation. Pairwise
missingness can change the sample between pairs; no listwise filtering occurs.

Pearson uses scaled and centered values for numerical stability. Spearman uses
one-based average ranks for ties, recalculated on that pair's complete sample,
then Pearson on those ranks. Fewer than two pairs or a constant input yields
null with an explicit reason; numerical degeneracy also yields null. Correlation
is bounded to [-1,1] only to handle floating arithmetic error. No p-values,
significance thresholds, feature dropping, redundant/not-redundant labels or
causal claims are produced. A near-unit coefficient in a tiny sample is not a
general redundancy conclusion. Nested and categorical fields are excluded from
this first redundancy implementation.

## Temporal and asset stability diagnostics

Temporal groups use UTC calendar dates from timezone-aware observation
timestamps only. Missing, invalid and timezone-naive timestamps are separately
counted and excluded from daily groups, but remain in aggregate diagnostics.
Window identity, capture time and outcomes never fill missing timestamps. Each
observed day reports row n and coverage/distributions of top-level numeric
fields. No empty calendar days are manufactured.

Adjacent *observed* dates are compared, with the calendar gap explicitly shown.
Each variable comparison gives row counts, valid counts, quantiles on both
sides, right-minus-left median change and maximum empirical CDF distance
(two-sample KS D). KS D is the supremum of absolute empirical-CDF differences,
handling tied values together. It is descriptive only: no p-value, independent
sample assumption, acceptance threshold or stable/unstable verdict. An empty
valid sample yields null; singleton samples remain explicit. Median-difference
overflow yields null with a numeric-overflow reason.

Asset comparisons use unpaired, full-sample marginal distributions within the
same population/source/schema/runtime revision. They report the same numeric
comparison metrics, timestamp coverage/ranges and shared observed UTC-date
count. They do not match asset windows, restrict to common time or pretend
simultaneity. Unassigned timestamps remain in these full-sample marginals.
Differences may reflect time coverage, price units, asset scales, composition or
measurement. Especially for raw prices/distances, a large median difference
does not establish a defect. Nested and categorical fields are excluded from
this first temporal/asset comparison implementation. These diagnostics supply
evidence for later interpretation, not conclusions about stability.

## Safeguards and limitations

The analyzer rejects incompatible P7B contract versions, incorrect selection
rules, wrong population/source labels, duplicate asset/window or observation
identities, invalid A eligibility, non-60 B targets, unexpected feature keys,
contradictory availability, missing source references and cohort conflicts.
The `p6c_d1_validation` cohort is rejected before population files open.
Protected paths resolving through `logs`, `sessions`, or `.env` are rejected.
Output must be a new directory outside `research_data`.

Hashes identify supplied artifacts, not independent proof of original-source
authenticity or execution revision. Original sources are never reopened.
P7B constants are imported from the accepted module, which this pass leaves
unchanged. Future P7B contract changes require explicit compatibility review.
Manifest-authoritative labels cannot restore absent row-level provenance.
Reports count missing timestamps, persisted sessions and persisted mode evidence;
null runtime revision is explicit. Stripped/rotated exports cannot establish
completeness. No empirical verdict is possible without an approved data run.

Population JSON includes outcomes physically, so parsing loads their objects,
but diagnostics never inspect, summarize or copy outcome payloads. Tests show
arbitrary outcome/strategy/action changes do not affect analysis. No forecast
scoring, Brier/log-loss/calibration, P&L, strategy ranking, regime labels,
threshold optimization, clustering/classification, or p_raw/p_clamped analysis
is included. No original source data is rewritten.

Input files must be static; three reads are not an atomic snapshot. Data and
exact frequency maps are held in memory. Pairwise diagnostics cost O(n*f^2),
and rank computations add sorting; output size grows with groups/days/fields.
Deterministic output has no generation timestamp. Entire JSON serialization is
validated before making the output directory; unchanged input paths/bytes and
settings produce identical output bytes. I/O failure during the single write
can leave an incomplete file. No overwrite/recovery is attempted.

## Synthetic validation

Final results:

- P7C-A analyzer: 31/31 unittest tests passed after the quote-availability
  correction, including v1 structural exclusion and decision/v2 positive cases.
- Accepted P7B builder: 20/20 unittest tests passed.
- Research store: 20/20 synthetic checks passed.
- Boundary store: 17/17 synthetic checks passed.
- Forecast history: 15/15 synthetic checks passed.
- Engine suite: not run; existing paths can attempt API traffic.

The correction-pass guarded run passed 51 tests (31 analyzer + 20 P7B), followed
by all three store/history suites. Store fixture warnings were expected
duplicate/malformed/write-failure cases. No production module was changed.
The bounded correction changed only quote-null aggregation, its eligible-row
denominator, tests and this documentation. Other diagnostics and the CLI are
unchanged. The v1 fixture also verifies that a non-null spread extension cannot
turn structurally absent quote fields into a recorded all-null quote observation.
Regression suites use synthetic data only. The guarded test harness is:

```python
import sys
import runpy
import unittest
from unittest.mock import patch

def offline(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname'}:
        raise RuntimeError('Network prohibited in P7C-A validation')

sys.addaudithook(offline)
with patch('dotenv.load_dotenv', return_value=False):
    suite = unittest.defaultTestLoader.loadTestsFromNames([
        'kalshi_bot.test_variable_quality',
        'kalshi_bot.test_market_state_dataset',
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    for module in ('kalshi_bot.test_research_store', 'kalshi_bot.test_boundary_store', 'kalshi_bot.test_forecast_history'):
        print('\nRUN ' + module, flush=True)
        runpy.run_module(module, run_name='__main__')
```

Run using `py -3 -` with the harness on stdin. Dotenv loading is disabled to avoid
local credentials. The network guard covers the test process; CLI subprocesses
import offline modules only. Approved unsandboxed execution is used where
Windows sandbox temporary-directory ACLs prevent fixtures from working.
No real historical or prospective artifact cohort is supplied in this pass.
## P7B-NFR compatibility: explicit nonfinite states

The analyzer now accepts P7B schema 2 and schema 3, and emits analyzer schema 2.
Schema 3 uses a null scalar numeric slot, `availability[field] = nonfinite`, and
`nonfinite_features[field]` equal to `positive_infinity`, `negative_infinity` or
`nan`. Missing/extra/unknown kind entries, non-null represented slots, fields
outside the declared scalar numeric contract and nonfinite states in schema 2
are rejected. Literal non-standard JSON Infinity/NaN tokens are rejected on load.

Population N includes these observations. Each variable reports availability
counts for observed, nonfinite, missing and structurally unavailable, plus
nonfinite_observed and counts for each signed kind. For numeric variables,
finite_observed equals valid_values. Observed coverage fractions include both
ordinary observed and explicit nonfinite observations; the separate
invalid_observed_values metric retains its existing type-quality meaning and
does not include the separately counted nonfinite states. Consequently:

`rows = finite_observed + invalid_observed_values + nonfinite_observed + missing + structurally_unavailable`

For nested summaries add invalid_parent to that identity. Quantiles, masses,
Pearson/Spearman pair counts and distribution comparisons use finite numeric
values only. Temporal summaries preserve signed nonfinite counts. A nulled
nonfinite quote is not counted as an ordinary missing quote/book. No statistical
thresholds, performance calculations or population-selection rules were added.

Clean schema-2 and schema-3 data have the same diagnostic values; new zero-count
fields and the analyzer version are output-contract additions. Nested object
nonfinite values remain unsupported/fail-closed in P7B; no nested measurement
policy is inferred by this extension. No actual-data analyzer run is authorized
by this implementation.
