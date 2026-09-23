"""P7D-A: offline Population A geometry, with no outcome or runtime dependency."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

from .market_state_dataset import FIELDS, GROUPS, RULE_A, RULE_B, RULE_V1, safe_path
from .variable_quality import (
    CATEGORICAL, DEFINED, MAPS, RAW_KEYS, empirical_cdf_distance, numeric,
    pearson, quantiles, ranks, time_buckets, validate_row,
)

BASELINE = '5baa73861d5fb369573897f714dbb2c6b41bf658'
STATES = ('finite', 'observed', 'nonfinite', 'missing', 'structurally_unavailable',
          'invalid', 'derivation_unavailable')
DISCRETE = {'spot_confidence', 'conviction', 'fresh_venue_count', 'target_tte'}
DERIVATIONS = {
    'd_logged': 'spot_now - price_to_beat',
    'r_logged': '(spot_now - price_to_beat) / price_to_beat',
    'l_logged': 'log(spot_now / price_to_beat); evaluated as log(S)-log(K)',
}
PANELS = (
    ('distance_time', (('r_logged', 'time_remaining'), ('l_logged', 'time_remaining'))),
    ('probability_threshold', (('p_base', 'z_threshold'),)),
    ('spot_strike', (('spot_now', 'price_to_beat'),)),
    ('distance_reference', (('d_logged', 'dist_from_threshold'),)),
    ('alpha_inputs', tuple(('alpha_micro', 'raw_features.' + k) for k in RAW_KEYS)),
    ('lag_namespaces', (('lag_signal', 'raw_features.lag_signal'),)),
)
ARTIFACTS = (
    'preferred_ge_60s.jsonl', 'boundary_60.jsonl', 'population_overlap.jsonl',
    'abstention_census.jsonl', 'schema_provenance.json', 'coverage_missingness.json',
)


def checked_path(path):
    path = safe_path(path)
    if any(p.lower() in {'research_data', 'p6c_d1_validation'} for p in path.parts):
        raise ValueError('protected research input/output path')
    return path


def validate_metadata(metadata):
    if not isinstance(metadata, dict):
        raise ValueError('P7B metadata required')
    c = metadata.get('cohort', {})
    if not isinstance(c, dict) or set(c) != {'mode', 'session_tag', 'sample_kind'}:
        raise ValueError('explicit cohort required')
    if c['session_tag'] == 'p6c_d1_validation':
        raise ValueError('P6C-E excluded')
    if c['mode'] != 'paper' or c['sample_kind'] not in {'development', 'historical'} or not isinstance(c['session_tag'], str) or not c['session_tag']:
        raise ValueError('P7D requires offline paper development/historical inputs')
    if type(metadata.get('builder_schema_version')) is not int or metadata['builder_schema_version'] not in (2, 3):
        raise ValueError('accepted P7B schema 2/3 required')
    if metadata.get('taxonomy') != GROUPS or metadata.get('defined_fields') != {k: sorted(v) for k, v in DEFINED.items()}:
        raise ValueError('P7B feature contract mismatch')
    if metadata.get('selection_rules') != {'A_decision': RULE_A, 'A_research_v1': RULE_V1, 'B': RULE_B}:
        raise ValueError('P7B selection contract mismatch')
    if not isinstance(metadata.get('sources'), list) or any(not isinstance(s, dict) for s in metadata['sources']):
        raise ValueError('source provenance required')


def registry(rows):
    specs = []
    for f in FIELDS:
        kind = 'categorical' if f in CATEGORICAL else 'object' if f in MAPS else 'numeric'
        specs.append(dict(name=f, path=[f], kind=kind, discrete=f in DISCRETE))
    for parent in sorted(MAPS):
        children = RAW_KEYS if parent == 'raw_features' else sorted({k for r in rows
            for k in (r['features'][parent] if isinstance(r['features'][parent], dict) else {})})
        for child in children:
            name = parent + '.' + child if parent == 'raw_features' else parent + '[' + json.dumps(child) + ']'
            specs.append(dict(name=name, path=[parent, child], kind='numeric', discrete=False))
    specs.extend(dict(name=f, path=None, kind='numeric', discrete=False, formula=formula,
                      inputs=['spot_now', 'price_to_beat'], namespace='research_only')
                 for f, formula in DERIVATIONS.items())
    return specs


def cell(state, value=None, **details):
    return dict(state=state, value=value, **details)


def source_cell(row, spec):
    parent = spec['path'][0]
    state, value = row['availability'][parent], row['features'][parent]
    if state == 'nonfinite':
        return cell('nonfinite', nonfinite_kind=row['nonfinite_features'][parent])
    if state != 'observed':
        return cell(state)
    if len(spec['path']) == 2:
        if not isinstance(value, dict):
            return cell('invalid', reason='invalid_parent')
        value = value.get(spec['path'][1])
        if value is None:
            return cell('missing', reason='nested_member_absent_or_null')
    valid = numeric(value) if spec['kind'] == 'numeric' else isinstance(value, str) if spec['kind'] == 'categorical' else isinstance(value, dict)
    if not valid:
        return cell('invalid', reason='invalid_' + spec['kind'] + '_type_or_range')
    return cell('finite' if spec['kind'] == 'numeric' else 'observed', value)


def distance_cells(cells):
    s, k = cells['spot_now'], cells['price_to_beat']
    invalid = {f: c['state'] for f, c in (('spot_now', s), ('price_to_beat', k)) if c['state'] != 'finite'}
    if invalid:
        return {f: cell('derivation_unavailable', reason='input_not_finite', input_states=invalid) for f in DERIVATIONS}
    s, k = s['value'], k['value']
    result = {}
    for name in DERIVATIONS:
        reason = None
        if name == 'r_logged' and k == 0:
            reason = 'zero_strike'
        elif name == 'l_logged' and (s <= 0 or k <= 0):
            reason = 'nonpositive_log_input'
        if reason:
            result[name] = cell('derivation_unavailable', reason=reason)
            continue
        try:
            value = s-k if name == 'd_logged' else (s-k)/k if name == 'r_logged' else math.log(s)-math.log(k)
        except (OverflowError, ValueError, ZeroDivisionError):
            value = None
        result[name] = cell('finite', value) if numeric(value) else cell('derivation_unavailable', reason='numeric_overflow_or_domain')
    return result


def observations(rows, specs):
    result = []
    for row in rows:
        values = {s['name']: source_cell(row, s) for s in specs if s['path'] is not None}
        values.update(distance_cells(values))
        result.append(dict(observation_id=row['observation_id'], asset=row['asset'], window_id_ts=row['window_id_ts'],
                           observation_timestamp=row['observation_timestamp'], source_file=row['source_file'],
                           source_sha256=row['source_sha256'], source_line=row['source_line'], values=values))
    return result


def finite_values(rows, field):
    return [r['values'][field]['value'] for r in rows if r['values'][field]['state'] == 'finite']


def distribution(rows, spec):
    field = spec['name']
    cells = [r['values'][field] for r in rows]
    counts = Counter(c['state'] for c in cells)
    assert sum(counts.values()) == len(rows)
    result = dict(field=field, rows=len(rows), counts={s: counts[s] for s in STATES},
                  nonfinite_kinds=dict(Counter(c['nonfinite_kind'] for c in cells if c['state'] == 'nonfinite')),
                  reasons=dict(Counter(c['reason'] for c in cells if 'reason' in c)))
    if spec['kind'] == 'numeric':
        values = finite_values(rows, field)
        masses = sorted(Counter(values).items())
        cumulative = 0
        points = []
        for value, count in masses:
            cumulative += count
            points.append(dict(value=value, count=count, mass=count/len(values), cdf=cumulative/len(values)))
        q = quantiles(values)
        if q is not None and not all(numeric(v) for v in q.values()):
            raise ValueError('nonfinite quantile calculation')
        result.update(finite_n=len(values), unique_count=len(masses), quantiles=q, ecdf_and_exact_masses=points,
                      discrete=spec['discrete'])
    elif spec['kind'] == 'categorical':
        result['value_counts'] = dict(sorted(Counter(c['value'] for c in cells if c['state'] == 'observed').items()))
    return result


def availability(rows, fields):
    patterns = Counter(tuple(r['values'][f]['state'] for f in fields) for r in rows)
    return dict(fields=fields, rows=len(rows),
                patterns=[dict(states=list(p), count=n) for p, n in sorted(patterns.items())],
                pairwise=[dict(left=a, right=b, rows=len(rows),
                               left_available=sum(r['values'][a]['state'] in ('finite', 'observed') for r in rows),
                               right_available=sum(r['values'][b]['state'] in ('finite', 'observed') for r in rows),
                               jointly_available=sum(all(r['values'][f]['state'] in ('finite', 'observed') for f in (a,b)) for r in rows),
                               jointly_finite=sum(all(r['values'][f]['state'] == 'finite' for f in (a,b)) for r in rows))
                          for a,b in combinations(fields, 2)])


def pair_panel(rows, left, right):
    complete = [r for r in rows if all(r['values'][f]['state'] == 'finite' for f in (left, right))]
    xs, ys = [r['values'][left]['value'] for r in complete], [r['values'][right]['value'] for r in complete]
    p, ps = pearson(xs, ys)
    s, ss = pearson(ranks(xs), ranks(ys))
    exclusions = Counter((r['values'][left]['state'], r['values'][right]['state']) for r in rows
                         if any(r['values'][f]['state'] != 'finite' for f in (left, right)))
    return dict(left=left, right=right, rows=len(rows), finite_pair_n=len(complete),
                excluded_n=len(rows)-len(complete), excluded_patterns=[dict(left_state=a, right_state=b, count=n) for (a,b),n in sorted(exclusions.items())],
                pearson=p, pearson_status=ps, spearman=s, spearman_status=ss,
                points=[dict(observation_id=r['observation_id'], x=x, y=y) for r,x,y in zip(complete,xs,ys)])


def panels(rows):
    result = []
    for name, pairs in PANELS:
        fields = list(dict.fromkeys(f for pair in pairs for f in pair))
        complete = sum(all(r['values'][f]['state'] == 'finite' for f in fields) for r in rows)
        patterns = Counter(tuple(r['values'][f]['state'] for f in fields) for r in rows
                           if any(r['values'][f]['state'] != 'finite' for f in fields))
        result.append(dict(panel=name, fields=fields, rows=len(rows), complete_case_n=complete,
                           excluded_n=len(rows)-complete,
                           excluded_patterns=[dict(states=list(p), count=n) for p,n in sorted(patterns.items())],
                           pairs=[pair_panel(rows, a, b) for a,b in pairs]))
    return result


def scaling(rows, specs):
    result = []
    for spec in specs:
        if spec['kind'] != 'numeric' or spec['discrete']:
            continue
        f = spec['name']; values = finite_values(rows, f); q = quantiles(values)
        median, iqr = (q['p50'], q['p75']-q['p25']) if q else (None, None)
        status = 'empty_finite_sample' if q is None else 'numeric_overflow' if not numeric(iqr) else 'zero_iqr' if iqr == 0 else 'defined'
        items = []
        for row in rows:
            c = row['values'][f]; value = None
            reason = 'input_not_finite' if c['state'] != 'finite' else status
            if reason == 'defined':
                value = (c['value']-median)/iqr
                if not numeric(value):
                    value = None; reason = 'numeric_overflow'
            items.append(dict(observation_id=row['observation_id'], value=value, status=reason, input_state=c['state']))
        result.append(dict(field=f, fit_basis='all finite observations in this asset/source/schema/revision group; retrospective only',
                           fit_observation_ids=[r['observation_id'] for r in rows if r['values'][f]['state']=='finite'],
                           finite_n=len(values), median=median, iqr=iqr if numeric(iqr) else None, status=status, observations=items))
    return result


def compare(left, right, specs):
    result = []
    for spec in specs:
        if spec['kind'] != 'numeric':
            continue
        f = spec['name']; xs, ys = finite_values(left, f), finite_values(right, f)
        qx, qy = quantiles(xs), quantiles(ys)
        delta = qy['p50']-qx['p50'] if qx and qy else None
        state = 'empty_finite_sample' if delta is None else 'defined' if numeric(delta) else 'numeric_overflow'
        result.append(dict(field=f, left_rows=len(left), right_rows=len(right), left_finite_n=len(xs), right_finite_n=len(ys),
                           left_counts=dict(Counter(r['values'][f]['state'] for r in left)),
                           right_counts=dict(Counter(r['values'][f]['state'] for r in right)),
                           median_difference=delta if numeric(delta) else None, median_difference_status=state,
                           empirical_cdf_distance=empirical_cdf_distance(xs,ys)))
    return result


def temporal(rows, specs):
    days, coverage = time_buckets(rows)
    dates = sorted(days)
    return dict(timestamp_coverage=coverage,
                daily=[dict(utc_date=d, rows=len(days[d]), variables=[distribution(days[d],s) for s in specs]) for d in dates],
                adjacent_observed_dates=[dict(left_utc_date=a, right_utc_date=b,
                                              calendar_gap_days=(datetime.fromisoformat(b)-datetime.fromisoformat(a)).days,
                                              variables=compare(days[a], days[b], specs)) for a,b in zip(dates,dates[1:])])


def analyze(rows, metadata, *, scale=False):
    validate_metadata(metadata)
    if not isinstance(rows, list) or type(scale) is not bool:
        raise ValueError('rows list and boolean scale required')
    seen, ids = set(), set()
    for row in rows:
        validate_row(row, 'preferred_ge_60s', metadata, seen, ids)
    specs = registry(rows)
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row['source_type'], row.get('schema_version'), row['asset'], row.get('git_sha'))].append(row)
    reports, comparable = [], defaultdict(list)
    for key in sorted(grouped, key=lambda k: json.dumps(k)):
        obs = observations(grouped[key], specs)
        reports.append(dict(source_type=key[0], source_schema=key[1], asset=key[2], runtime_git_sha=key[3], rows=len(obs),
                            observations=obs, variables=[distribution(obs,s) for s in specs],
                            availability=availability(obs,[s['name'] for s in specs]), panels=panels(obs),
                            scaling=scaling(obs,specs) if scale else None, temporal=temporal(obs,specs)))
        comparable[(key[0],key[1],key[3])].append((key[2],obs))
    assets = []
    for key in sorted(comparable, key=lambda k: json.dumps(k)):
        for (a,left),(b,right) in combinations(sorted(comparable[key]),2):
            ld,lt = time_buckets(left); rd,rt = time_buckets(right)
            assets.append(dict(source_type=key[0], source_schema=key[1], runtime_git_sha=key[2], left_asset=a, right_asset=b,
                               left_timestamp_coverage=lt, right_timestamp_coverage=rt, shared_observed_dates=sorted(ld.keys() & rd.keys()),
                               basis='unpaired full-sample marginals; no time matching or composition control',
                               variables=compare(left,right,specs)))
    assert sum(g['rows'] for g in reports) == len(rows)
    return dict(p7d_schema_version=1, accepted_research_baseline=BASELINE, population='preferred_ge_60s', rows=len(rows),
                cohort=dict(metadata['cohort']), settings=dict(within_asset_scaling=scale), registry=specs,
                panel_definitions=[dict(name=n,pairs=p) for n,p in PANELS], groups=reports, asset_comparisons=assets,
                limitations=[
                    'Conditional on supplied Population A eligibility and last-supplied-row selection; no population reconstruction.',
                    'Unknown runtime revisions stay unknown; historical implementation/configuration changes cannot be separated from temporal variation.',
                    'Missing, structural, nonfinite, invalid and derivation-unavailable states are separate; no imputation, clipping or finite-value filtering beyond validity.',
                    'Logged distance derivatives use recorded spot_now and price_to_beat; they do not replace P7B fields or establish equivalence to dist_from_threshold.',
                    'ECDF points are exact empirical masses; no smoothing, bins, regimes, rankings, p-values or predictive claims.',
                    'Nested raw_features names are distinct estimates/inputs; absence of a top-level field is not repaired from a nested field.',
                    'No B observations, outcome joins, performance analysis or runtime reads.'])


def strict_load(data):
    def reject(token):
        raise ValueError('non-standard JSON token: ' + token)
    return json.loads(data, parse_constant=reject)


def load_and_analyze(input_dir, p7c_report, artifact_index, *, p7c_sha256, aggregate_sha256, scale=False):
    root, report_path, index_path = map(checked_path, (input_dir,p7c_report,artifact_index))
    index_bytes = index_path.read_bytes()
    if hashlib.sha256(index_bytes).hexdigest() != aggregate_sha256:
        raise ValueError('artifact index hash mismatch')
    expected = {}
    for line in index_bytes.decode('utf-8').splitlines():
        digest, name = line.split('  ',1)
        if name not in ARTIFACTS or name in expected or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('invalid artifact index')
        expected[name]=digest
    if set(expected)!=set(ARTIFACTS):
        raise ValueError('six artifact hashes required')
    # Reject excluded cohorts before opening population artifacts.
    meta_bytes = checked_path(root/'schema_provenance.json').read_bytes()
    if hashlib.sha256(meta_bytes).hexdigest()!=expected['schema_provenance.json']:
        raise ValueError('metadata hash mismatch')
    metadata = strict_load(meta_bytes); validate_metadata(metadata)
    contents = {}
    for name in ARTIFACTS:
        data = checked_path(root/name).read_bytes()
        if hashlib.sha256(data).hexdigest()!=expected[name]:
            raise ValueError('artifact hash mismatch: '+name)
        if name=='preferred_ge_60s.jsonl':
            contents[name]=data
    report_bytes=report_path.read_bytes()
    if hashlib.sha256(report_bytes).hexdigest()!=p7c_sha256:
        raise ValueError('P7C report hash mismatch')
    p7c=strict_load(report_bytes)
    if not isinstance(p7c, dict) or type(p7c.get('analyzer_schema_version')) is not int or p7c['analyzer_schema_version'] not in (1, 2):
        raise ValueError('accepted P7C report schema required')
    if p7c.get('p7b_provenance')!=metadata:
        raise ValueError('P7C/P7B provenance mismatch')
    linked={Path(i['path']).name:i['sha256'] for i in p7c.get('input_artifacts',[])}
    if any(linked.get(name)!=expected[name] for name in ('schema_provenance.json','preferred_ge_60s.jsonl','boundary_60.jsonl')):
        raise ValueError('P7C input hashes do not identify this P7B dataset')
    rows=[strict_load(line) for line in contents['preferred_ge_60s.jsonl'].decode('utf-8-sig').splitlines() if line.strip()]
    if p7c.get('population_rows',{}).get('preferred_ge_60s')!=len(rows):
        raise ValueError('P7C population count mismatch')
    result=analyze(rows,metadata,scale=scale)
    result['input_provenance']=dict(input_dir=str(root),artifact_sha256=expected,aggregate_sha256=aggregate_sha256,
                                    p7c_report=str(report_path),p7c_sha256=p7c_sha256,
                                    p7b_builder_schema_version=metadata['builder_schema_version'],
                                    implementation_sha256={name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                        for name in ('continuous_state.py', 'variable_quality.py', 'market_state_dataset.py')})
    return result


def write_report(result, output_dir):
    out=checked_path(output_dir)
    if out.exists():
        raise ValueError('output directory must be new')
    content=json.dumps(result,indent=2,sort_keys=True,allow_nan=False)+'\n'
    out.mkdir(parents=True)
    path=out/'continuous_state.json'
    with path.open('x',encoding='utf-8') as handle:
        handle.write(content)
    return path
