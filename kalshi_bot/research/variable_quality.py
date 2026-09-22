"""P7C-A: offline variable diagnostics for accepted P7B artifacts only.

No outcomes, scoring, strategy evaluation, runtime imports, or source-store reads.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations

from .market_state_dataset import (
    DECISION, FIELDS, GROUPS, RULE_A, RULE_B, RULE_V1, V1, V2, finite, safe_path,
)

BASELINE = '5116cdec49e47ddca65c34f1105d62678c4a9f38'
POPULATIONS = ('preferred_ge_60s', 'boundary_60')
CATEGORICAL = {'price_to_beat_source', 'lead_source', 'p_market_semantics'}
MAPS = {'per_venue_mids', 'per_venue_staleness', 'raw_features'}
RAW_KEYS = ('obi', 'ofi_hawkes', 'microprice_dev', 'trade_sign_autocorr', 'lag_signal', 'response_gap')
STATES = ('observed', 'missing', 'structurally_unavailable')
CATEGORY = {field: category for category, fields in GROUPS.items() for field in fields}
NUMERIC_FIELDS = tuple(field for field in FIELDS if field not in MAPS | CATEGORICAL)
DEFINED = {'decision': DECISION, 'research_v1': V1, 'research_v2': V2}
RULES = {'decision': RULE_A, 'research_v1': RULE_V1, 'research_v2': RULE_B}


def numeric(value):
    """Strict JSON number; do not silently coerce strings or booleans."""
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def quantiles(values):
    """Linear interpolation at (n-1)*q; finite data only, no imputation."""
    if not values:
        return None
    data = sorted(values)
    result = {}
    for label, q in (('min', 0), ('p01', .01), ('p05', .05), ('p25', .25), ('p50', .5), ('p75', .75), ('p95', .95), ('p99', .99), ('max', 1)):
        index = (len(data) - 1) * q
        lo, hi = math.floor(index), math.ceil(index)
        weight = index - lo
        result[label] = data[lo] if lo == hi else (1 - weight) * data[lo] + weight * data[hi]
    return result


def threshold(value):
    if value is not None and (not numeric(value) or value < 0):
        raise ValueError('age threshold must be a finite nonnegative number of seconds')
    return value


def summarize(entries, kind='numeric'):
    """entries are (availability, value); every count states its denominator."""
    n = len(entries)
    states = Counter(state for state, _ in entries)
    counts = {state: states[state] for state in (*STATES, 'invalid_parent')}
    observed = [value for state, value in entries if state == 'observed']
    defined = n - counts['structurally_unavailable']
    result = dict(rows=n, nonstructural_rows=defined, availability=counts, observed_fraction_all_rows=len(observed) / n if n else None,
                  observed_fraction_nonstructural_rows=len(observed) / defined if defined else None)
    valid = [v for v in observed if numeric(v)] if kind == 'numeric' else [v for v in observed if isinstance(v, str)] if kind == 'categorical' else [v for v in observed if isinstance(v, dict)]
    result.update(valid_values=len(valid), invalid_observed_values=len(observed) - len(valid), value_kind=kind)
    if kind == 'numeric':
        masses = Counter(valid)
        top = sorted(masses.items(), key=lambda pair: (-pair[1], pair[0]))[:5]
        result.update(quantiles=quantiles(valid), unique_values=len(masses), zero_count=masses[0],
                      zero_fraction_valid=masses[0] / len(valid) if valid else None,
                      negative_count=sum(v < 0 for v in valid), constant_among_valid=(len(masses) == 1 if valid else None),
                      top_exact_value_masses=[dict(value=v, count=count, fraction_valid=count / len(valid)) for v, count in top])
    elif kind == 'categorical':
        result['value_counts'] = dict(sorted(Counter(valid).items()))
    else:
        result['empty_object_count'] = sum(not v for v in valid)
    return result


def validate_row(row, population, metadata, seen, observation_ids):
    if not isinstance(row, dict):
        raise ValueError('population row must be an object')
    for field, value in metadata['cohort'].items():
        if row.get(field) != value:
            raise ValueError('mixed or missing row cohort')
    kind = row.get('source_type')
    allowed = {'decision', 'research_v1'} if population == 'preferred_ge_60s' else {'research_v2'}
    if kind not in allowed or row.get('source_population') != population:
        raise ValueError('wrong population/source type')
    if row.get('selection_rule') != RULES[kind]:
        raise ValueError('unrecognized selection provenance')
    schema = row.get('schema_version')
    if kind != 'decision' and (type(schema) is not int or schema != (1 if kind == 'research_v1' else 2)):
        raise ValueError('source schema mismatch')
    if schema is not None and (type(schema) is not int or schema < 0):
        raise ValueError('invalid source schema version')
    asset, window = row.get('asset'), row.get('window_id_ts')
    if not isinstance(asset, str) or not asset or type(window) is not int:
        raise ValueError('invalid population key')
    key = (asset, window)
    if key in seen:
        raise ValueError('duplicate asset/window in population')
    seen.add(key)
    digest, line = row.get('source_sha256'), row.get('source_line')
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest) or type(line) is not int or line < 1:
        raise ValueError('invalid source observation identity')
    if row.get('observation_id') != f'{digest}:{line}' or row['observation_id'] in observation_ids:
        raise ValueError('invalid or duplicate observation identity')
    observation_ids.add(row['observation_id'])
    if not any(s.get('sha256') == digest and s.get('type') == kind and s.get('path') == row.get('source_file') for s in metadata['sources']):
        raise ValueError('row source missing from P7B provenance')
    if row.get('persisted_session_tag') not in (None, metadata['cohort']['session_tag']):
        raise ValueError('persisted session conflict')
    if row.get('persisted_mode') not in (None, metadata['cohort']['mode']):
        raise ValueError('persisted mode conflict')
    if row.get('persisted_dry_run') is not None and (type(row['persisted_dry_run']) is not bool or row['persisted_dry_run'] != (metadata['cohort']['mode'] == 'paper')):
        raise ValueError('persisted dry_run conflict')
    if row.get('git_sha') is not None and not isinstance(row['git_sha'], str):
        raise ValueError('invalid runtime revision provenance')
    features, availability = row.get('features'), row.get('availability')
    if not isinstance(features, dict) or not isinstance(availability, dict) or set(features) != set(FIELDS) or set(availability) != set(FIELDS):
        raise ValueError('unexpected feature/availability schema')
    for field in FIELDS:
        state, value = availability[field], features[field]
        if state not in STATES or (state == 'observed') != (value is not None):
            raise ValueError('inconsistent availability/value')
        if state != 'observed' and state != ('missing' if field in DEFINED[kind] else 'structurally_unavailable'):
            raise ValueError('availability contradicts accepted source contract')
    if isinstance(features['raw_features'], dict) and set(features['raw_features']) - set(RAW_KEYS):
        raise ValueError('unexpected nested raw feature')
    if population == 'preferred_ge_60s':
        # Match P7B eligibility coercion, but diagnose type quality independently.
        tte = finite(features['time_remaining'])
        if finite(features['p_real']) is None or tte is None or tte < 60:
            raise ValueError('ineligible Population A row')
    elif finite(features['target_tte']) != 60:
        raise ValueError('boundary population requires target_tte 60')


def nested_entries(rows, parent, child):
    entries = []
    for row in rows:
        state, value = row['availability'][parent], row['features'][parent]
        if state != 'observed':
            entries.append((state, None))
        elif not isinstance(value, dict):
            entries.append(('invalid_parent', None))
        else:
            item = value.get(child)
            entries.append(('observed' if item is not None else 'missing', item))
    return entries


def paired_quality(rows):
    """Descriptive evidence only; no inferred book, fallback, or feed state."""
    pairs = [(r['features']['dislocation'], r['features']['fresh_venue_count']) for r in rows]
    valid = [(d, n) for d, n in pairs if numeric(d) and numeric(n) and n >= 0 and float(n).is_integer()]
    zero = [(d, n) for d, n in valid if d == 0]
    quotes = ('yes_bid', 'yes_ask', 'no_bid', 'no_ask')
    # Nulls outside the source contract are structural absence, not missing quotes.
    quote_rows = [r for r in rows if all(q in DEFINED[r['source_type']] for q in quotes)]
    all_null_quote_rows = [r for r in quote_rows if all(r['features'][q] is None for q in quotes)]
    return dict(
        dislocation_with_valid_venue_count=len(valid),
        zero_dislocation_with_valid_venue_count=len(zero),
        zero_dislocation_with_fewer_than_two_venues=sum(n < 2 for _, n in zero),
        zero_dislocation_with_unknown_or_invalid_venue_count=sum(numeric(d) and d == 0 for d, _ in pairs) - len(zero),
        rows_with_quote_fields_in_source_contract=len(quote_rows),
        rows_with_all_four_quotes_null=len(all_null_quote_rows),
        rows_with_spread_but_all_four_quotes_null=sum(r['features']['kalshi_spread'] is not None for r in all_null_quote_rows),
        meaning='Quote-null counts apply only to rows whose source contract defines all four quote fields; structural absence is excluded. Null quotes are not proof that an exchange book did not exist. Low-venue zero dislocation is not agreement.',
    )


def analyze(populations, metadata, *, quote_stale_seconds=None, venue_stale_seconds=None):
    quote_stale_seconds, venue_stale_seconds = threshold(quote_stale_seconds), threshold(venue_stale_seconds)
    if not isinstance(metadata, dict) or not isinstance(populations, dict):
        raise ValueError('P7B metadata and population mapping must be objects')
    cohort = metadata.get('cohort', {})
    if not isinstance(cohort, dict) or set(cohort) != {'mode', 'session_tag', 'sample_kind'} or cohort.get('mode') not in {'paper', 'live'} or cohort.get('sample_kind') not in {'development', 'historical', 'prospective'} or not isinstance(cohort.get('session_tag'), str) or not cohort['session_tag']:
        raise ValueError('explicit P7B cohort required')
    if cohort['session_tag'] == 'p6c_d1_validation':
        raise ValueError('P6C-E excluded')
    if metadata.get('builder_schema_version') != 2 or metadata.get('taxonomy') != GROUPS or metadata.get('selection_rules') != {'A_decision': RULE_A, 'A_research_v1': RULE_V1, 'B': RULE_B} or metadata.get('defined_fields') != {k: sorted(v) for k, v in DEFINED.items()}:
        raise ValueError('requires accepted P7B schema from 5116cde')
    if not isinstance(metadata.get('sources'), list) or any(not isinstance(s, dict) for s in metadata['sources']):
        raise ValueError('source provenance required')
    if set(populations) != set(POPULATIONS):
        raise ValueError('both separate P7B populations required, even if empty')
    groups = defaultdict(list)
    for population in POPULATIONS:
        if not isinstance(populations[population], list):
            raise ValueError('population must be a list of rows')
        seen, observations = set(), set()
        for row in populations[population]:
            validate_row(row, population, metadata, seen, observations)
            group = (population, row['source_type'], row.get('schema_version'), row['asset'], row.get('git_sha'))
            groups[group].append(row)
    reports = []
    for group in sorted(groups, key=lambda g: json.dumps(g)):
        rows = groups[group]
        variables = []
        for field in FIELDS:
            entries = [(r['availability'][field], r['features'][field]) for r in rows]
            kind = 'object' if field in MAPS else 'categorical' if field in CATEGORICAL else 'numeric'
            variable = dict(field=field, category=CATEGORY[field], **summarize(entries, kind))
            if field in ('realized_vol', 'realized_vol_value'):
                variable['ambiguous_fallback_semantics'] = 'Persisted measurement versus fallback cannot be distinguished; equality to 0.30 is not a fallback flag.'
                variable['exact_0_30_count'] = sum(numeric(v) and v == .30 for state, v in entries if state == 'observed')
            if field == 'fresh_venue_count':
                variable['invalid_count_domain'] = sum(numeric(v) and (v < 0 or not float(v).is_integer()) for _, v in entries)
            if field == 'quote_age_secs':
                variable['age_check'] = age_check(entries, quote_stale_seconds)
            variables.append(variable)
            if field in MAPS:
                children = RAW_KEYS if field == 'raw_features' else sorted({k for _, value in entries if isinstance(value, dict) for k in value})
                for child in children:
                    child_entries = nested_entries(rows, field, child)
                    category = 'estimated_response' if field == 'raw_features' and child in ('lag_signal', 'response_gap') else CATEGORY[field]
                    nested = dict(field=field, member=child, category=category, **summarize(child_entries))
                    if field == 'per_venue_staleness':
                        nested['age_check'] = age_check(child_entries, venue_stale_seconds)
                    variables.append(nested)
        reports.append(dict(population=group[0], source_type=group[1], schema_version=group[2], asset=group[3], runtime_git_sha=group[4], rows=len(rows),
                            observation_timestamp_missing=sum(r.get('observation_timestamp') is None for r in rows),
                            persisted_session_missing=sum(r.get('persisted_session_tag') is None for r in rows),
                            persisted_mode_evidence_missing=sum(r.get('persisted_mode') is None and r.get('persisted_dry_run') is None for r in rows),
                            variables=variables, measurement_context=paired_quality(rows),
                            redundancy=redundancy(rows), temporal=temporal_diagnostics(rows)))
    return dict(analyzer_schema_version=1, research_tooling_baseline=BASELINE, cohort=cohort,
                population_rows={p: len(populations[p]) for p in POPULATIONS},
                settings=dict(quote_stale_seconds=quote_stale_seconds, venue_stale_seconds=venue_stale_seconds),
                methodology=NOTES, groups=reports, asset_stability=asset_diagnostics(groups))


def age_check(entries, limit):
    values = [v for state, v in entries if state == 'observed' and numeric(v) and v >= 0]
    return dict(valid_nonnegative_age_count=len(values), threshold_seconds=limit,
                above_threshold_count=sum(v > limit for v in values) if limit is not None else None,
                status='not_assessed_no_supplied_threshold' if limit is None else 'descriptive_above_supplied_threshold')


def pearson(xs, ys):
    """Scaled, centered Pearson correlation; no hypothesis test or cutoff."""
    if len(xs) < 2:
        return None, 'fewer_than_two_pairs'
    if len(set(xs)) == 1 or len(set(ys)) == 1:
        return None, 'constant_input'

    def centered(values):
        scale = max(abs(v) for v in values)
        scaled = [v / scale for v in values]
        mean = math.fsum(scaled) / len(scaled)
        residuals = [v - mean for v in scaled]
        scale = max(abs(v) for v in residuals)
        return [v / scale for v in residuals] if scale else None

    x, y = centered(xs), centered(ys)
    if x is None or y is None:
        return None, 'numerically_degenerate'
    denominator = math.sqrt(math.fsum(v * v for v in x)) * math.sqrt(math.fsum(v * v for v in y))
    return max(-1.0, min(1.0, math.fsum(a * b for a, b in zip(x, y)) / denominator)), 'defined'


def ranks(values):
    """One-based average ranks for ties, recomputed on pairwise-complete data."""
    order = sorted(range(len(values)), key=values.__getitem__)
    result = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        rank = (start + 1 + end) / 2
        for index in order[start:end]:
            result[index] = rank
        start = end
    return result


def redundancy(rows):
    result = []
    for left, right in combinations(NUMERIC_FIELDS, 2):
        pairs = [(r['features'][left], r['features'][right]) for r in rows
                 if numeric(r['features'][left]) and numeric(r['features'][right])]
        xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
        p, p_status = pearson(xs, ys)
        s, s_status = pearson(ranks(xs), ranks(ys))
        result.append(dict(left=left, right=right, group_rows=len(rows), paired_valid_rows=len(pairs),
                           paired_fraction_all_rows=len(pairs) / len(rows) if rows else None,
                           pearson=p, pearson_status=p_status, spearman=s, spearman_status=s_status))
    return result


def empirical_cdf_distance(xs, ys):
    """Two-sample KS D (maximum empirical CDF distance), with ties; no p-value."""
    if not xs or not ys:
        return None
    a, b = Counter(xs), Counter(ys)
    left = right = 0
    distance = 0.0
    for value in sorted(a.keys() | b.keys()):
        left += a[value]
        right += b[value]
        distance = max(distance, abs(left / len(xs) - right / len(ys)))
    return distance


def compare_marginals(left, right):
    comparisons = []
    for field in NUMERIC_FIELDS:
        xs = [r['features'][field] for r in left if numeric(r['features'][field])]
        ys = [r['features'][field] for r in right if numeric(r['features'][field])]
        qx, qy = quantiles(xs), quantiles(ys)
        delta = qy['p50'] - qx['p50'] if xs and ys else None
        if delta is not None and not numeric(delta):
            delta = None
            delta_status = 'numeric_overflow'
        else:
            delta_status = 'defined' if delta is not None else 'empty_valid_sample'
        comparisons.append(dict(field=field, left_rows=len(left), right_rows=len(right),
                                left_valid=len(xs), right_valid=len(ys),
                                left_quantiles=qx, right_quantiles=qy,
                                median_change_right_minus_left=delta, median_change_status=delta_status,
                                empirical_cdf_distance=empirical_cdf_distance(xs, ys)))
    return comparisons


def time_buckets(rows):
    days = defaultdict(list)
    times = []
    counts = Counter()
    for row in rows:
        value = row.get('observation_timestamp')
        if value is None:
            counts['missing'] += 1
            continue
        try:
            if not isinstance(value, str):
                raise ValueError('timestamp is not a string')
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                counts['naive_timezone_unknown'] += 1
                continue
            stamp = stamp.astimezone(timezone.utc)
        except (ValueError, OverflowError):
            counts['invalid'] += 1
            continue
        times.append(stamp)
        days[stamp.date().isoformat()].append(row)
    return days, dict(valid_aware_timestamps=len(times), missing=counts['missing'], invalid=counts['invalid'],
                      naive_timezone_unknown=counts['naive_timezone_unknown'],
                      earliest_utc=min(times).isoformat() if times else None,
                      latest_utc=max(times).isoformat() if times else None)


def temporal_diagnostics(rows):
    days, timestamps = time_buckets(rows)
    ordered = sorted(days)
    daily = [dict(utc_date=day, rows=len(days[day]), variables=[
        dict(field=f, **summarize([(r['availability'][f], r['features'][f]) for r in days[day]]))
        for f in NUMERIC_FIELDS]) for day in ordered]
    comparisons = [dict(left_utc_date=left, right_utc_date=right,
                        calendar_gap_days=(datetime.fromisoformat(right) - datetime.fromisoformat(left)).days,
                        variables=compare_marginals(days[left], days[right]))
                   for left, right in zip(ordered, ordered[1:])]
    return dict(timestamp_coverage=timestamps, daily=daily, adjacent_observed_dates=comparisons)


def asset_diagnostics(groups):
    comparable = defaultdict(list)
    for group, rows in groups.items():
        # Same cohort is already enforced; never mix source/revision/population.
        comparable[(group[0], group[1], group[2], group[4])].append((group[3], rows))
    result = []
    for key in sorted(comparable, key=lambda k: json.dumps(k)):
        for (left_asset, left), (right_asset, right) in combinations(sorted(comparable[key]), 2):
            left_days, left_times = time_buckets(left)
            right_days, right_times = time_buckets(right)
            result.append(dict(population=key[0], source_type=key[1], schema_version=key[2], runtime_git_sha=key[3],
                               left_asset=left_asset, right_asset=right_asset,
                               left_timestamp_coverage=left_times, right_timestamp_coverage=right_times,
                               shared_observed_utc_dates=len(left_days.keys() & right_days.keys()),
                               comparison_basis='Unpaired full-sample marginal distributions, including unassigned timestamps; no synchronization or common-time restriction.',
                               variables=compare_marginals(left, right)))
    return result


NOTES = [
    'P7C-A diagnostics only: no candidate/conditional ranking, performance, outcome statistics, regimes or stable/unstable verdicts.',
    'Groups keep population, source type, source schema, asset and recorded runtime revision separate. No feature joins or synchronized cross-asset observations.',
    'Coverage denominators: all group rows and nonstructural group rows. Observed does not mean numerically valid. Strict numbers exclude booleans, strings and nonfinite values.',
    'Quantiles use linear interpolation at (n-1)*q on valid numbers; zero and point masses use exact values. Top five masses sorted by count descending then value ascending. No rounding, imputation, or cutoff search.',
    'Numeric negative counts are descriptive, not universal invalidity. Finite outliers are retained. One unique valid value does not establish general constancy beyond this sample.',
    'Nested venue member universe is only keys observed in valid maps within the group; absent members are missing, not inferred structurally absent. raw_features uses six accepted named inputs. Invalid parent objects have their own count.',
    'per_venue_staleness measures recorded venue age in seconds; venue_mids contains fresh venues only. Missing venue mids do not identify the reason or reconstruct feed state.',
    'Optional age thresholds are supplied externally, never fitted. Strict greater-than comparison excludes negative ages and has a stated valid-age denominator. Without thresholds, report ages without stale labels.',
    'Historical fallback ambiguity remains variable semantics. No row-level fallback flags, book-depth reconstruction, or interpretation of low-venue dislocation zero as agreement.',
    'Manifest-authoritative cohort labels do not establish missing row provenance. Missing runtime revisions remain a separate unknown group. No claim about omitted/rotated observations.',
    'Input hashes identify supplied artifacts, not independent verification of original source files or Git execution history. Original sources are never reopened. Outcomes and abstention rows are not analyzed.',
    'Redundancy: Pearson and average-tie-rank Spearman on pairwise-complete strict numeric top-level values from the same observations within each group. Every pair has its own n. Constants or fewer than two pairs produce null with reasons. No feature selection, thresholds or significance tests.',
    'Temporal stability: UTC calendar-day summaries from timezone-aware observation timestamps only; missing, invalid and naive timestamps are separately counted and excluded from daily summaries, not reconstructed from window identity. Adjacent observed dates may have gaps; gaps are explicit.',
    'Distribution comparisons report quantiles, right-minus-left median change and maximum empirical CDF distance (two-sample KS D), without p-values, thresholds or stability verdicts. Empty samples yield null. Top-level numeric fields only; no nested/categorical redundancy or stability tests.',
    'Asset stability compares unpaired full-sample marginal distributions within the same source/schema/revision/population. Timestamp ranges and shared UTC-date counts expose timing differences, but do not establish simultaneity or control composition. Raw-unit differences, especially price/distance, are not evidence of measurement defects by themselves.',
]


def load_and_analyze(input_dir, **settings):
    root = safe_path(input_dir)
    inputs = []

    def read(name, jsonl=False):
        path = safe_path(root / name)
        data = path.read_bytes()
        inputs.append(dict(path=str(path), sha256=hashlib.sha256(data).hexdigest(), bytes=len(data)))
        text = data.decode('utf-8-sig')
        return [json.loads(line) for line in text.splitlines() if line.strip()] if jsonl else json.loads(text)

    metadata = read('schema_provenance.json')
    if not isinstance(metadata, dict) or not isinstance(metadata.get('cohort'), dict):
        raise ValueError('P7B metadata must contain a cohort object')
    # Check excluded cohort before opening population files.
    if metadata.get('cohort', {}).get('session_tag') == 'p6c_d1_validation':
        raise ValueError('P6C-E excluded')
    populations = {p: read(p + '.jsonl', True) for p in POPULATIONS}
    result = analyze(populations, metadata, **settings)
    result['input_artifacts'] = inputs
    result['p7b_provenance'] = metadata
    return result


def write_report(result, output_dir):
    out = safe_path(output_dir)
    if out.exists() or 'research_data' in {part.lower() for part in out.parts}:
        raise ValueError('output must be a new directory outside research_data')
    # Validate the entire JSON representation before creating output.
    content = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + '\n'
    out.mkdir(parents=True)
    path = out / 'variable_quality.json'
    with path.open('x', encoding='utf-8') as handle:
        handle.write(content)
    return path
