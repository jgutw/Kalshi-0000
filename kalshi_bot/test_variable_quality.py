"""Synthetic-only P7C-A correctness, provenance and scope tests."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kalshi_bot.research.market_state_dataset import build, write_artifacts
from kalshi_bot.research.variable_quality import (
    BASELINE, analyze, compare_marginals, empirical_cdf_distance, load_and_analyze, pearson,
    quantiles, ranks, summarize, write_report,
)


class VariableQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def decision(self, window=100, **fields):
        return dict(dict(asset='BTC', window_id_ts=window, ts='2026-01-01T00:00:00Z', time_remaining=65, p_real=.6), **fields)

    def boundary(self, window=100, **fields):
        return self.decision(window, **dict(dict(schema_version=2, record_kind='boundary_snapshot', target_tte=60, raw_tte=61, tau_used=60), **fields))

    def dataset(self, decisions=(), boundaries=()):
        manifest = dict(mode='paper', session_tag='synthetic_p7c', sample_kind='development', sources=[])
        for name, kind, rows in (('decisions', 'decision', decisions), ('boundary', 'research_v2', boundaries)):
            path = self.root / (name + '.jsonl')
            path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
            manifest['sources'].append(dict(type=kind, path=str(path)))
        return build(manifest)

    def run_analysis(self, data, **settings):
        return analyze({p: data[p] for p in ('preferred_ge_60s', 'boundary_60')}, data['schema_provenance'], **settings)

    def variable(self, report, field, member=None, group=0):
        return next(v for v in report['groups'][group]['variables'] if v['field'] == field and v.get('member') == member)

    def test_coverage_keeps_structural_missing_and_observed_separate(self):
        data = self.dataset([self.decision(kalshi_spread=None), self.decision(101, kalshi_spread=.02)])
        result = self.run_analysis(data)
        spread = self.variable(result, 'kalshi_spread')
        self.assertEqual(spread['availability'], dict(observed=1, nonfinite=0, missing=1, structurally_unavailable=0, invalid_parent=0))
        self.assertEqual(spread['observed_fraction_all_rows'], .5)
        target = self.variable(result, 'target_tte')
        self.assertEqual(target['availability']['structurally_unavailable'], 2)
        self.assertIsNone(target['observed_fraction_nonstructural_rows'])
        self.assertIsNone(target['quantiles'])

    def test_nonfinite_counts_quantiles_pairs_and_loading(self):
        values = [(1, 2), (3, 6), (float('inf'), 8), (float('-inf'), 10), (float('nan'), 12), (9, float('inf')), (None, 14)]
        data = self.dataset([self.decision(100+i, z_threshold=x, response_beta=y) for i, (x, y) in enumerate(values)])
        root = self.root / 'represented'
        write_artifacts(data, root)
        report = load_and_analyze(root)
        v = self.variable(report, 'z_threshold')
        self.assertEqual(v['rows'], 7)
        self.assertEqual(v['availability'], dict(observed=3, nonfinite=3, missing=1, structurally_unavailable=0, invalid_parent=0))
        self.assertEqual(v['nonfinite_kinds'], dict(positive_infinity=1, negative_infinity=1, nan=1))
        self.assertEqual(v['finite_observed'], 3)
        self.assertEqual(v['observed_fraction_all_rows'], 6/7)
        self.assertEqual(v['quantiles']['p50'], 3)
        pair = next(p for p in report['groups'][0]['redundancy'] if {p['left'], p['right']} == {'z_threshold', 'response_beta'})
        self.assertEqual(pair['paired_valid_rows'], 2)
        self.assertAlmostEqual(pair['pearson'], 1)
        self.assertAlmostEqual(pair['spearman'], 1)
        daily = report['groups'][0]['temporal']['daily'][0]['variables']
        self.assertEqual(next(v for v in daily if v['field'] == 'z_threshold')['nonfinite_observed'], 3)
        loaded = [json.loads(line) for line in (root / 'preferred_ge_60s.jsonl').read_text().splitlines()]
        self.assertEqual(loaded[2]['nonfinite_features'], data['preferred_ge_60s'][2]['nonfinite_features'])
        self.assertEqual(loaded[2]['source_sha256'], data['preferred_ge_60s'][2]['source_sha256'])
        json.dumps(report, allow_nan=False)

    def test_nonfinite_metadata_inconsistencies_fail_closed(self):
        original = self.dataset([self.decision(z_threshold=float('inf'))])
        mutations = [
            lambda d: d['preferred_ge_60s'][0].pop('nonfinite_features'),
            lambda d: d['preferred_ge_60s'][0]['nonfinite_features'].update(z_threshold='unknown'),
            lambda d: d['preferred_ge_60s'][0]['features'].update(z_threshold=4),
            lambda d: d['preferred_ge_60s'][0]['availability'].update(z_threshold='missing'),
            lambda d: d['schema_provenance'].update(builder_schema_version=2),
            lambda d: d['preferred_ge_60s'][0]['nonfinite_features'].update(raw_distance='nan'),
        ]
        for i, mutate in enumerate(mutations):
            data = copy.deepcopy(original); mutate(data)
            with self.subTest(i=i), self.assertRaises(ValueError):
                self.run_analysis(data)

    def test_clean_schema_two_and_three_analysis_agree(self):
        data = self.dataset([self.decision(z_threshold=1), self.decision(101, z_threshold=2)])
        current = self.run_analysis(data)
        self.assertNotIn('nonfinite_features', data['preferred_ge_60s'][0])
        data['schema_provenance']['builder_schema_version'] = 2
        self.assertEqual(current, self.run_analysis(data))

    def test_nonfinite_quote_is_not_missing_book(self):
        data = self.dataset([self.decision(yes_bid=float('inf'))])
        context = self.run_analysis(data)['groups'][0]['measurement_context']
        self.assertEqual(context['rows_with_all_four_quotes_null'], 0)

    def test_literal_nonfinite_artifact_and_unrepresented_values_rejected(self):
        data = self.dataset([self.decision(z_threshold=2)])
        root = self.root / 'invalid_literals'
        write_artifacts(data, root)
        path = root / 'preferred_ge_60s.jsonl'
        row = json.loads(path.read_text())
        row['features']['z_threshold'] = float('inf')
        path.write_text(json.dumps(row) + '\n')
        with self.assertRaisesRegex(ValueError, 'non-standard JSON'):
            load_and_analyze(root)
        data['preferred_ge_60s'][0]['features']['z_threshold'] = float('inf')
        with self.assertRaises(ValueError):
            self.run_analysis(data)

    def test_known_quantiles_and_exact_masses(self):
        summary = summarize([('observed', v) for v in (0, 0, 2, 6)])
        self.assertEqual(summary['quantiles']['p50'], 1)
        self.assertEqual(summary['quantiles']['p25'], 0)
        self.assertEqual(summary['quantiles']['p75'], 3)
        self.assertEqual(summary['zero_fraction_valid'], .5)
        self.assertEqual(summary['top_exact_value_masses'][0], dict(value=0, count=2, fraction_valid=.5))
        self.assertFalse(summary['constant_among_valid'])
        self.assertEqual(quantiles([7])['p99'], 7)
        self.assertIsNone(quantiles([]))

    def test_pearson_reference_values_and_constant_cases(self):
        self.assertAlmostEqual(pearson([1, 2, 3], [2, 4, 6])[0], 1)
        self.assertAlmostEqual(pearson([1, 2, 3], [6, 4, 2])[0], -1)
        self.assertAlmostEqual(pearson([-1, 0, 1], [1, -2, 1])[0], 0)
        self.assertEqual(pearson([1, 1], [2, 3]), (None, 'constant_input'))
        self.assertEqual(pearson([1], [2]), (None, 'fewer_than_two_pairs'))
        self.assertAlmostEqual(pearson([-1e300, 0, 1e300], [1e300, 0, -1e300])[0], -1)

    def test_redundancy_pairwise_denominators_and_tied_ranks(self):
        self.assertEqual(ranks([5, 1, 1, 8]), [3, 1.5, 1.5, 4])
        data = self.dataset([self.decision(100 + i, spot_now=x, price_to_beat=y) for i, (x, y) in enumerate([(1, 2), (1, 3), (3, 4), (None, 100), ('bad', 5)])])
        correlations = self.run_analysis(data)['groups'][0]['redundancy']
        pair = next(p for p in correlations if p['left'] == 'spot_now' and p['right'] == 'price_to_beat')
        self.assertEqual(pair['paired_valid_rows'], 3)
        self.assertEqual(pair['group_rows'], 5)
        self.assertEqual(pair['paired_fraction_all_rows'], .6)
        self.assertAlmostEqual(pair['spearman'], 3 ** .5 / 2)

    def test_empirical_cdf_distance_reference_cases(self):
        self.assertEqual(empirical_cdf_distance([0, 0, 1], [0, 0, 1]), 0)
        self.assertEqual(empirical_cdf_distance([0, 0], [1, 1]), 1)
        self.assertEqual(empirical_cdf_distance([0, 1], [1, 2]), .5)
        self.assertIsNone(empirical_cdf_distance([], [1]))

    def test_temporal_utc_days_gaps_and_unassigned_timestamps(self):
        rows = [
            self.decision(100, ts='2026-01-01T23:30:00-02:00', spot_now=10),
            self.decision(101, ts='2026-01-02T02:00:00+00:00', spot_now=20),
            self.decision(102, ts='2026-01-04T00:00:00Z', spot_now=30),
            self.decision(103, ts=None, spot_now=999),
            self.decision(104, ts='invalid', spot_now=999),
            self.decision(105, ts='2026-01-02T00:00:00', spot_now=999),
        ]
        temporal = self.run_analysis(self.dataset(rows))['groups'][0]['temporal']
        coverage = temporal['timestamp_coverage']
        self.assertEqual(coverage['valid_aware_timestamps'], 3)
        self.assertEqual((coverage['missing'], coverage['invalid'], coverage['naive_timezone_unknown']), (1, 1, 1))
        self.assertEqual([(d['utc_date'], d['rows']) for d in temporal['daily']], [('2026-01-02', 2), ('2026-01-04', 1)])
        comparison = temporal['adjacent_observed_dates'][0]
        self.assertEqual(comparison['calendar_gap_days'], 2)
        spot = next(v for v in comparison['variables'] if v['field'] == 'spot_now')
        self.assertEqual(spot['median_change_right_minus_left'], 15)
        self.assertEqual(spot['empirical_cdf_distance'], 1)
        self.assertEqual((spot['left_valid'], spot['right_valid']), (2, 1))

    def test_asset_marginals_are_unpaired_and_revision_isolated(self):
        rows = [self.decision(100, asset='BTC', git_sha='one', spot_now=10, ts='2026-01-01T00:00:00Z'),
                self.decision(200, asset='ETH', git_sha='one', spot_now=20, ts='2026-02-01T00:00:00Z'),
                self.decision(300, asset='SOL', git_sha='other', spot_now=30)]
        report = self.run_analysis(self.dataset(rows))
        self.assertEqual(len(report['asset_stability']), 1)
        comparison = report['asset_stability'][0]
        self.assertEqual(comparison['shared_observed_utc_dates'], 0)
        self.assertEqual((comparison['left_asset'], comparison['right_asset']), ('BTC', 'ETH'))
        spot = next(v for v in comparison['variables'] if v['field'] == 'spot_now')
        self.assertEqual(spot['median_change_right_minus_left'], 10)
        self.assertEqual(spot['empirical_cdf_distance'], 1)

    def test_strict_numeric_quality_does_not_coerce(self):
        values = [0, '0', True, float('nan'), float('inf'), -3]
        result = summarize([('observed', v) for v in values])
        self.assertEqual(result['availability']['observed'], 6)
        self.assertEqual(result['valid_values'], 2)
        self.assertEqual(result['invalid_observed_values'], 4)
        self.assertEqual(result['negative_count'], 1)
        self.assertEqual(result['quantiles']['min'], -3)

    def test_mass_ties_and_single_value_are_explicit(self):
        result = summarize([('observed', v) for v in (9, 3, 1, 9, 3, 1)])
        self.assertEqual([m['value'] for m in result['top_exact_value_masses']], [1, 3, 9])
        result = summarize([('observed', 7), ('missing', None)])
        self.assertTrue(result['constant_among_valid'])
        self.assertEqual(result['valid_values'], 1)
        self.assertEqual(result['rows'], 2)

    def test_boundary_target_and_observation_identity_are_validated(self):
        data = self.dataset(boundaries=[self.boundary()])
        data['boundary_60'][0]['features']['target_tte'] = 30
        with self.assertRaisesRegex(ValueError, 'target_tte 60'):
            self.run_analysis(data)
        data = self.dataset([self.decision(), self.decision(101)])
        first, second = data['preferred_ge_60s']
        second['source_line'] = first['source_line']
        second['observation_id'] = first['observation_id']
        with self.assertRaisesRegex(ValueError, 'duplicate observation'):
            self.run_analysis(data)

    def test_accepted_numeric_string_eligibility_is_not_numeric_quality(self):
        data = self.dataset(boundaries=[self.boundary(target_tte='60')])
        field = self.variable(self.run_analysis(data), 'target_tte')
        self.assertEqual(field['availability']['observed'], 1)
        self.assertEqual(field['invalid_observed_values'], 1)
        self.assertIsNone(field['quantiles'])

    def test_median_difference_overflow_is_explicit(self):
        data = self.dataset([self.decision(100, spot_now=10 ** 308), self.decision(101, spot_now=-(10 ** 308))])
        left, right = data['preferred_ge_60s']
        result = next(v for v in compare_marginals([left], [right]) if v['field'] == 'spot_now')
        self.assertIsNone(result['median_change_right_minus_left'])
        self.assertEqual(result['median_change_status'], 'numeric_overflow')
        self.assertEqual(result['empirical_cdf_distance'], 1)

    def test_source_schema_and_persisted_cohort_conflicts_fail(self):
        for field, value in (('schema_version', 1), ('persisted_mode', 'live'), ('persisted_dry_run', False), ('persisted_session_tag', 'other')):
            data = self.dataset(boundaries=[self.boundary()])
            data['boundary_60'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.run_analysis(data)

    def test_no_outcome_or_strategy_dependence(self):
        data = self.dataset([self.decision()])
        original = self.run_analysis(data)
        data['preferred_ge_60s'][0]['outcome'] = {'arbitrary_future_payload': 999}
        data['preferred_ge_60s'][0]['strategy'] = 'irrelevant_strategy'
        data['preferred_ge_60s'][0]['action'] = 'BUY_YES'
        self.assertEqual(original, self.run_analysis(data))
        self.assertNotIn('arbitrary_future_payload', json.dumps(original))

    def test_populations_assets_revisions_and_times_never_merge(self):
        data = self.dataset([self.decision(git_sha='a'), self.decision(101, git_sha='b'), self.decision(asset='ETH')], [self.boundary()])
        result = self.run_analysis(data)
        self.assertEqual(len(result['groups']), 4)
        self.assertTrue(all(g['rows'] == 1 for g in result['groups']))
        group = next(i for i, g in enumerate(result['groups']) if g['population'] == 'boundary_60')
        self.assertEqual(self.variable(result, 'raw_tte', group=group)['quantiles']['p50'], 61)
        self.assertEqual(self.variable(result, 'tau_used', group=group)['quantiles']['p50'], 60)
        self.assertEqual(self.variable(result, 'target_tte', group=group)['quantiles']['p50'], 60)

    def test_low_venue_dislocation_zero_has_no_agreement_label(self):
        result = self.run_analysis(self.dataset([self.decision(dislocation=0, fresh_venue_count=1), self.decision(101, dislocation=0), self.decision(102, dislocation=0, fresh_venue_count=2)]))
        context = result['groups'][0]['measurement_context']
        self.assertEqual(context['dislocation_with_valid_venue_count'], 2)
        self.assertEqual(context['zero_dislocation_with_fewer_than_two_venues'], 1)
        self.assertEqual(context['zero_dislocation_with_unknown_or_invalid_venue_count'], 1)

    def test_volatility_point_mass_is_not_a_fallback_flag(self):
        result = self.run_analysis(self.dataset([self.decision(realized_vol_value=.30), self.decision(101, realized_vol_value=.30), self.decision(102)]))
        variable = self.variable(result, 'realized_vol_value')
        self.assertEqual(variable['exact_0_30_count'], 2)
        self.assertEqual(variable['valid_values'], 2)
        self.assertEqual(variable['availability']['missing'], 1)
        self.assertNotIn('is_fallback', json.dumps(result))

    def test_age_thresholds_are_optional_explicit_and_strict(self):
        data = self.dataset([self.decision(100 + i, quote_age_secs=v, per_venue_staleness={'venue': v}) for i, v in enumerate((-1, 0, 2, 3))])
        result = self.run_analysis(data)
        self.assertIsNone(self.variable(result, 'quote_age_secs')['age_check']['above_threshold_count'])
        result = self.run_analysis(data, quote_stale_seconds=2, venue_stale_seconds=2)
        for field, member in (('quote_age_secs', None), ('per_venue_staleness', 'venue')):
            age = self.variable(result, field, member)['age_check']
            self.assertEqual(age['valid_nonnegative_age_count'], 3)
            self.assertEqual(age['above_threshold_count'], 1)
        for limit in (-1, float('nan'), True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.run_analysis(data, quote_stale_seconds=limit)

    def test_nested_venue_denominators_and_invalid_parent(self):
        data = self.dataset([self.decision(per_venue_mids={'venue': 100}), self.decision(101, per_venue_mids={}), self.decision(102), self.decision(103, per_venue_mids='bad')])
        nested = self.variable(self.run_analysis(data), 'per_venue_mids', 'venue')
        self.assertEqual(nested['availability'], dict(observed=1, nonfinite=0, missing=2, structurally_unavailable=0, invalid_parent=1))
        self.assertEqual(nested['observed_fraction_all_rows'], .25)

    def test_nested_estimates_and_categorical_values(self):
        data = self.dataset([self.decision(raw_features={'lag_signal': .2}, lead_source='one'), self.decision(101, lead_source='two')])
        result = self.run_analysis(data)
        self.assertEqual(self.variable(result, 'raw_features', 'lag_signal')['category'], 'estimated_response')
        self.assertEqual(self.variable(result, 'raw_features', 'response_gap')['availability']['missing'], 2)
        self.assertEqual(self.variable(result, 'lead_source')['value_counts'], {'one': 1, 'two': 1})

    def test_duplicate_and_tampered_population_rejected(self):
        data = self.dataset([self.decision()])
        data['preferred_ge_60s'].append(copy.deepcopy(data['preferred_ge_60s'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate asset/window'):
            self.run_analysis(data)
        for field, value in (('source_population', 'boundary_60'), ('selection_rule', 'wrong'), ('observation_id', 'wrong'), ('session_tag', 'other'), ('source_sha256', 'f' * 64)):
            data = self.dataset([self.decision()])
            data['preferred_ge_60s'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.run_analysis(data)

    def test_contract_and_feature_leakage_rejected(self):
        data = self.dataset([self.decision()])
        data['preferred_ge_60s'][0]['availability']['target_tte'] = 'missing'
        with self.assertRaisesRegex(ValueError, 'availability contradicts'):
            self.run_analysis(data)
        data = self.dataset([self.decision()])
        data['preferred_ge_60s'][0]['features']['future_outcome'] = 1
        with self.assertRaisesRegex(ValueError, 'unexpected feature'):
            self.run_analysis(data)
        data = self.dataset([self.decision()])
        data['schema_provenance']['builder_schema_version'] = 1
        with self.assertRaisesRegex(ValueError, 'accepted P7B'):
            self.run_analysis(data)

    def test_p6_excluded_before_population_files_are_opened(self):
        root = self.root / 'p6'
        root.mkdir()
        (root / 'schema_provenance.json').write_text(json.dumps({'cohort': {'session_tag': 'p6c_d1_validation'}}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'P6C-E excluded'):
            load_and_analyze(root)

    def test_empty_population_not_interpreted_as_coverage(self):
        result = self.run_analysis(self.dataset())
        self.assertEqual(result['groups'], [])
        self.assertEqual(result['population_rows'], dict(preferred_ge_60s=0, boundary_60=0))

    def test_missing_book_is_not_a_zero_spread(self):
        result = self.run_analysis(self.dataset([self.decision()]))
        spread = self.variable(result, 'kalshi_spread')
        self.assertEqual(spread['zero_count'], 0)
        self.assertIsNone(spread['quantiles'])
        self.assertEqual(result['groups'][0]['measurement_context']['rows_with_all_four_quotes_null'], 1)
        self.assertEqual(result['groups'][0]['measurement_context']['rows_with_quote_fields_in_source_contract'], 1)

    def test_v1_structural_quotes_are_not_recorded_null_quotes(self):
        for spread in (None, .02):
            with self.subTest(spread=spread):
                path = self.root / 'v1.jsonl'
                row = self.decision(schema_version=1, actual_outcome='YES', yes_settled=1,
                                    outcome_source='spot_vs_price_to_beat', kalshi_spread=spread)
                path.write_text(json.dumps(row) + '\n', encoding='utf-8')
                data = build(dict(mode='paper', session_tag='synthetic_p7c', sample_kind='development',
                                  sources=[dict(type='research_v1', path=str(path))]))
                result = self.run_analysis(data)
                context = result['groups'][0]['measurement_context']
                self.assertEqual(context['rows_with_quote_fields_in_source_contract'], 0)
                self.assertEqual(context['rows_with_all_four_quotes_null'], 0)
                self.assertEqual(context['rows_with_spread_but_all_four_quotes_null'], 0)
                for field in ('yes_bid', 'yes_ask', 'no_bid', 'no_ask'):
                    availability = self.variable(result, field)['availability']
                    self.assertEqual(availability['structurally_unavailable'], 1)
                    self.assertEqual(availability['missing'], 0)

    def test_defined_quote_contract_counts_only_all_null_rows(self):
        for kind in ('decision', 'research_v2'):
            with self.subTest(kind=kind):
                factory = self.decision if kind == 'decision' else self.boundary
                rows = [factory(100), factory(101, kalshi_spread=.02), factory(102, yes_bid=.4)]
                data = self.dataset(decisions=rows) if kind == 'decision' else self.dataset(boundaries=rows)
                result = self.run_analysis(data)
                context = result['groups'][0]['measurement_context']
                self.assertEqual(context['rows_with_quote_fields_in_source_contract'], 3)
                self.assertEqual(context['rows_with_all_four_quotes_null'], 2)
                self.assertEqual(context['rows_with_spread_but_all_four_quotes_null'], 1)
                self.assertEqual(self.variable(result, 'yes_ask')['availability']['missing'], 3)

    def test_input_hashes_read_only_reproducible_and_no_original_reads(self):
        data = self.dataset([self.decision()])
        inputs = self.root / 'inputs'
        write_artifacts(data, inputs)
        before = {p.name: p.read_bytes() for p in inputs.iterdir()}
        # Original source exports can be unavailable; only three artifacts are read.
        (self.root / 'decisions.jsonl').unlink()
        (self.root / 'boundary.jsonl').unlink()
        result = load_and_analyze(inputs)
        self.assertEqual(result['research_tooling_baseline'], BASELINE)
        self.assertEqual(len(result['input_artifacts']), 3)
        self.assertEqual(before, {p.name: p.read_bytes() for p in inputs.iterdir()})
        one = write_report(result, self.root / 'one')
        two = write_report(load_and_analyze(inputs), self.root / 'two')
        self.assertEqual(one.read_bytes(), two.read_bytes())
        with self.assertRaises(ValueError):
            write_report(result, self.root / 'one')
        with self.assertRaises(ValueError):
            write_report(result, self.root / 'research_data' / 'output')

    def test_invalid_serialization_does_not_create_output(self):
        out = self.root / 'bad'
        with self.assertRaises(ValueError):
            write_report({'invalid': float('nan')}, out)
        self.assertFalse(out.exists())

    def test_protected_paths_rejected(self):
        for name in ('logs', 'sessions', '.env'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'protected'):
                load_and_analyze(self.root / name)

    def test_cli_synthetic_end_to_end(self):
        inputs = self.root / 'inputs'
        write_artifacts(self.dataset([self.decision(quote_age_secs=5)]), inputs)
        cli = Path(__file__).resolve().parents[1] / 'scripts' / 'analyze_variable_quality.py'
        out = self.root / 'cli'
        result = subprocess.run([sys.executable, str(cli), str(inputs), '--out-dir', str(out), '--quote-stale-seconds', '3'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads((out / 'variable_quality.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['settings']['quote_stale_seconds'], 3)
        self.assertEqual(self.variable(saved, 'quote_age_secs')['age_check']['above_threshold_count'], 1)


if __name__ == '__main__':
    unittest.main()
