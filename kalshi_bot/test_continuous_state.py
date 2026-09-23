"""P7D synthetic/adversarial fixtures only; never open a real research cohort."""
import copy
import hashlib
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kalshi_bot.research.market_state_dataset import build, write_artifacts
from kalshi_bot.research.variable_quality import load_and_analyze as p7c
from kalshi_bot.research.continuous_state import (
    ARTIFACTS, PANELS, analyze, checked_path, load_and_analyze, write_report,
)


class ContinuousStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def row(self, window=100, **fields):
        return dict(dict(asset='BTC',window_id_ts=window,ts='2026-01-01T12:00:00Z',
                         time_remaining=65,p_real=.6,spot_now=110,price_to_beat=100),**fields)

    def dataset(self, rows):
        path=self.root/'synthetic.jsonl'
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')
        return build(dict(mode='paper',session_tag='synthetic_p7d',sample_kind='development',
                          sources=[dict(type='decision',path=str(path))]))

    def run_analysis(self, data, **settings):
        return analyze(data['preferred_ge_60s'],data['schema_provenance'],**settings)

    def variable(self, report, name, group=0):
        return next(v for v in report['groups'][group]['variables'] if v['field']==name)

    def panel(self, report, name):
        return next(p for p in report['groups'][0]['panels'] if p['panel']==name)

    def inputs(self, rows):
        data=self.dataset(rows); root=self.root/'p7b'
        write_artifacts(data,root)
        report=self.root/'p7c.json'
        report.write_text(json.dumps(p7c(root),allow_nan=False),encoding='utf-8')
        index=self.root/'artifacts.sha256'
        index.write_bytes(''.join(hashlib.sha256((root/n).read_bytes()).hexdigest()+'  '+n+'\n' for n in sorted(ARTIFACTS)).encode())
        hashes=dict(p7c_sha256=hashlib.sha256(report.read_bytes()).hexdigest(),
                    aggregate_sha256=hashlib.sha256(index.read_bytes()).hexdigest())
        return root,report,index,hashes

    def test_state_accounting_nonfinite_and_finite_statistics(self):
        data=self.dataset([self.row(100+i,z_threshold=x) for i,x in enumerate([2,4,float('inf'),float('-inf'),float('nan'),None,'bad',True])])
        before=copy.deepcopy(data)
        report=self.run_analysis(data)
        v=self.variable(report,'z_threshold')
        self.assertEqual(v['counts'],dict(finite=2,observed=0,nonfinite=3,missing=1,structurally_unavailable=0,invalid=2,derivation_unavailable=0))
        self.assertEqual(v['nonfinite_kinds'],dict(positive_infinity=1,negative_infinity=1,nan=1))
        self.assertEqual(sum(v['counts'].values()),8)
        self.assertEqual(v['quantiles']['p50'],3)
        self.assertEqual(v['ecdf_and_exact_masses'],[dict(value=2,count=1,mass=.5,cdf=.5),dict(value=4,count=1,mass=.5,cdf=1.)])
        self.assertEqual(self.variable(report,'raw_distance')['counts']['structurally_unavailable'],8)
        self.assertEqual(self.variable(report,'response_beta')['counts']['missing'],8)
        self.assertEqual(data,before)
        json.dumps(report,allow_nan=False)

    def test_distance_derivations_keep_source_slots_and_provenance(self):
        data=self.dataset([self.row()]);report=self.run_analysis(data)
        row=report['groups'][0]['observations'][0]
        self.assertEqual(row['values']['d_logged']['value'],10)
        self.assertAlmostEqual(row['values']['r_logged']['value'],.1)
        self.assertAlmostEqual(row['values']['l_logged']['value'],math.log(1.1))
        self.assertIsNone(row['values']['raw_distance']['value'])
        self.assertEqual(row['observation_id'],data['preferred_ge_60s'][0]['observation_id'])
        self.assertEqual(row['source_sha256'],data['preferred_ge_60s'][0]['source_sha256'])
        spec=next(s for s in report['registry'] if s['name']=='r_logged')
        self.assertEqual(spec['inputs'],['spot_now','price_to_beat'])
        self.assertEqual(spec['namespace'],'research_only')

    def test_zero_negative_invalid_and_extreme_distance_inputs(self):
        cases=[dict(price_to_beat=0),dict(spot_now=-2),dict(price_to_beat=-2),dict(spot_now=None),
               dict(spot_now='100'),dict(price_to_beat=float('inf')),dict(spot_now=1e308,price_to_beat=-1e308),
               dict(spot_now=1e308,price_to_beat=1e-308)]
        report=self.run_analysis(self.dataset([self.row(100+i,**v) for i,v in enumerate(cases)]))
        cells=[r['values'] for r in report['groups'][0]['observations']]
        self.assertEqual(cells[0]['d_logged']['value'],110)
        self.assertEqual(cells[0]['r_logged']['reason'],'zero_strike')
        for i in (0,1,2):self.assertEqual(cells[i]['l_logged']['reason'],'nonpositive_log_input')
        for i in (3,4,5):self.assertEqual(cells[i]['d_logged']['reason'],'input_not_finite')
        self.assertEqual(cells[5]['d_logged']['input_states'],{'price_to_beat':'nonfinite'})
        self.assertEqual(cells[6]['d_logged']['state'],'derivation_unavailable')
        self.assertEqual(cells[7]['r_logged']['state'],'derivation_unavailable')
        self.assertTrue(math.isfinite(cells[7]['l_logged']['value']))
        json.dumps(report,allow_nan=False)

    def test_pairwise_joint_counts_are_not_marginal_minimum(self):
        rows=[self.row(100,alpha_micro=1,raw_features={'obi':None}),
              self.row(101,alpha_micro=None,raw_features={'obi':2}),
              self.row(102,alpha_micro=3,raw_features={'obi':4})]
        report=self.run_analysis(self.dataset(rows))
        pair=next(p for p in report['groups'][0]['availability']['pairwise'] if p['left']=='alpha_micro' and p['right']=='raw_features.obi')
        self.assertEqual((pair['left_available'],pair['right_available'],pair['jointly_finite']),(2,2,1))
        panel=self.panel(report,'alpha_inputs')
        self.assertEqual(panel['complete_case_n'],0)
        self.assertEqual(panel['pairs'][0]['finite_pair_n'],1)
        self.assertEqual(sum(p['count'] for p in panel['excluded_patterns']),panel['excluded_n'])
        patterns=report['groups'][0]['availability']['patterns']
        self.assertEqual(sum(p['count'] for p in patterns),3)

    def test_tied_rank_and_constant_pair_statuses(self):
        data=self.dataset([self.row(100+i,spot_now=x,price_to_beat=y) for i,(x,y) in enumerate([(1,2),(1,3),(3,4)])])
        pair=self.panel(self.run_analysis(data),'spot_strike')['pairs'][0]
        self.assertAlmostEqual(pair['spearman'],math.sqrt(3)/2)
        data=self.dataset([self.row(100+i,spot_now=1,price_to_beat=i+2) for i in range(3)])
        pair=self.panel(self.run_analysis(data),'spot_strike')['pairs'][0]
        self.assertIsNone(pair['pearson']);self.assertEqual(pair['pearson_status'],'constant_input')
        self.assertIsNone(pair['spearman']);self.assertEqual(pair['spearman_status'],'constant_input')

    def test_pair_exclusions_include_either_nonfinite_and_exact_n(self):
        data=self.dataset([self.row(100+i,p_base=x,z_threshold=y) for i,(x,y) in enumerate([(1,2),(2,4),(float('inf'),8),(3,float('nan'))])])
        p=self.panel(self.run_analysis(data),'probability_threshold')['pairs'][0]
        self.assertEqual((p['finite_pair_n'],p['excluded_n']),(2,2))
        self.assertAlmostEqual(p['pearson'],1);self.assertAlmostEqual(p['spearman'],1)
        self.assertEqual(sum(x['count'] for x in p['excluded_patterns']),2)

    def test_zero_iqr_and_discrete_scores_not_scaled(self):
        data=self.dataset([self.row(100+i,spot_confidence=.6,conviction=4) for i in range(4)])
        report=self.run_analysis(data,scale=True)
        fit=next(v for v in report['groups'][0]['scaling'] if v['field']=='spot_now')
        self.assertEqual(fit['status'],'zero_iqr')
        self.assertEqual(fit['finite_n'],4)
        self.assertTrue(all(x['value'] is None for x in fit['observations']))
        self.assertNotIn('spot_confidence',[v['field'] for v in report['groups'][0]['scaling']])
        self.assertNotIn('conviction',[v['field'] for v in report['groups'][0]['scaling']])
        self.assertTrue(self.variable(report,'spot_confidence')['discrete'])

    def test_scaling_fits_within_asset_and_records_all_rows(self):
        data=self.dataset([self.row(100+i,asset=a,spot_now=x) for a,values in [('BTC',[1,2,3]),('ETH',[10,20,30])] for i,x in enumerate(values)])
        report=self.run_analysis(data,scale=True)
        for g,median,iqr in zip(report['groups'],[2,20],[1,10]):
            fit=next(v for v in g['scaling'] if v['field']=='spot_now')
            self.assertEqual((fit['median'],fit['iqr']),(median,iqr))
            self.assertEqual([x['value'] for x in fit['observations']],[-1,0,1])
            self.assertEqual(len(fit['fit_observation_ids']),3)

    def test_temporal_gaps_unknown_timestamps_and_daily_states(self):
        rows=[self.row(100,ts='2026-01-01T23:00:00-02:00',z_threshold=float('inf')),
              self.row(101,ts='2026-01-06T00:00:00Z'),self.row(102,ts=None),
              self.row(103,ts='2026-01-04T00:00:00'),self.row(104,ts='bad')]
        report=self.run_analysis(self.dataset(rows));t=report['groups'][0]['temporal']
        self.assertEqual([d['utc_date'] for d in t['daily']],['2026-01-02','2026-01-06'])
        self.assertEqual(t['adjacent_observed_dates'][0]['calendar_gap_days'],4)
        self.assertEqual((t['timestamp_coverage']['missing'],t['timestamp_coverage']['invalid'],t['timestamp_coverage']['naive_timezone_unknown']),(1,1,1))
        self.assertEqual(report['rows'],5)
        self.assertEqual(next(v for v in t['daily'][0]['variables'] if v['field']=='z_threshold')['nonfinite_kinds'],{'positive_infinity':1})

    def test_namespace_separation_and_invalid_nested_parent(self):
        data=self.dataset([self.row(lag_signal=2,response_gap=None,raw_features={'lag_signal':7,'response_gap':8}),self.row(101,per_venue_mids='bad')])
        report=self.run_analysis(data)
        row=report['groups'][0]['observations'][0]['values']
        self.assertEqual(row['lag_signal']['value'],2)
        self.assertEqual(row['raw_features.lag_signal']['value'],7)
        self.assertEqual(row['response_gap']['state'],'missing')
        self.assertEqual(row['raw_features.response_gap']['value'],8)
        self.assertEqual(self.variable(report,'per_venue_mids')['counts']['invalid'],1)

    def test_venue_member_registry_is_collision_safe_and_strict(self):
        data=self.dataset([self.row(per_venue_mids={'a.b':3,'a':4}),self.row(101,per_venue_mids='bad')])
        report=self.run_analysis(data)
        self.assertEqual(self.variable(report,'per_venue_mids["a.b"]')['counts']['finite'],1)
        self.assertEqual(self.variable(report,'per_venue_mids["a.b"]')['reasons'],{'invalid_parent':1})

    def test_outcomes_and_action_metadata_cannot_affect_results(self):
        data=self.dataset([self.row(),self.row(101)])
        before=copy.deepcopy(data)
        a=self.run_analysis(data)
        for row in data['preferred_ge_60s']:
            row['outcome']={'arbitrary':object()}
            row['strategy']='irrelevant';row['action']='irrelevant';row['reason']='irrelevant'
        self.assertEqual(a,self.run_analysis(data))
        self.assertEqual(a['rows'],len(before['preferred_ge_60s']))
        self.assertEqual({r['observation_id'] for g in a['groups'] for r in g['observations']},
                         {r['observation_id'] for r in before['preferred_ge_60s']})

    def test_asset_comparisons_unpaired_and_revision_isolated(self):
        data=self.dataset([self.row(asset='BTC',ts='2026-01-01T00:00:00Z'),
                           self.row(asset='ETH',ts='2026-01-03T00:00:00Z'),self.row(asset='SOL',git_sha='different')])
        report=self.run_analysis(data)
        self.assertEqual(len(report['asset_comparisons']),1)
        c=report['asset_comparisons'][0]
        self.assertEqual((c['left_asset'],c['right_asset']),('BTC','ETH'))
        self.assertEqual(c['shared_observed_dates'],[])
        self.assertEqual(len(report['groups']),3)

    def test_empty_population_and_predeclared_panels(self):
        report=self.run_analysis(self.dataset([]))
        self.assertEqual(report['rows'],0);self.assertEqual(report['groups'],[])
        self.assertEqual(len(report['panel_definitions']),6)
        self.assertEqual([p['name'] for p in report['panel_definitions']],[n for n,_ in PANELS])

    def test_metadata_protection_and_tampered_rows_fail(self):
        original=self.dataset([self.row()])
        for field,value in [('session_tag','p6c_d1_validation'),('sample_kind','prospective'),('mode','live')]:
            data=copy.deepcopy(original);data['schema_provenance']['cohort'][field]=value
            with self.assertRaises(ValueError):self.run_analysis(data)
        data=copy.deepcopy(original);data['preferred_ge_60s'].append(data['preferred_ge_60s'][0])
        with self.assertRaises(ValueError):self.run_analysis(data)
        for name in ['logs','sessions','.env','research_data','p6c_d1_validation']:
            with self.assertRaises(ValueError):checked_path(self.root/name)

    def test_hash_gated_loading_reproducible_and_no_source_reopen(self):
        root,report,index,hashes=self.inputs([self.row(z_threshold=float('inf'))])
        (self.root/'synthetic.jsonl').unlink()
        result=load_and_analyze(root,report,index,**hashes)
        self.assertEqual(result,load_and_analyze(root,report,index,**hashes))
        path=write_report(result,self.root/'output')
        def reject(t):raise AssertionError(t)
        self.assertEqual(json.loads(path.read_text(),parse_constant=reject)['rows'],1)
        with self.assertRaises(ValueError):write_report(result,self.root/'output')
        for key in hashes:
            bad=dict(hashes);bad[key]='0'*64
            with self.assertRaises(ValueError):load_and_analyze(root,report,index,**bad)
        (root/'preferred_ge_60s.jsonl').write_text('[]\n')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):load_and_analyze(root,report,index,**hashes)

    def test_p7c_linkage_mismatch_rejected_even_with_matching_report_hash(self):
        root,report,index,hashes=self.inputs([self.row()])
        r=json.loads(report.read_text());r['input_artifacts'][0]['sha256']='0'*64
        report.write_text(json.dumps(r));hashes['p7c_sha256']=hashlib.sha256(report.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError,'input hashes'):load_and_analyze(root,report,index,**hashes)

    def test_cli_synthetic_end_to_end(self):
        root,report,index,hashes=self.inputs([self.row(),self.row(101,spot_now=120)])
        script=Path(__file__).resolve().parents[1]/'scripts'/'analyze_continuous_state.py'
        out=self.root/'cli'
        proc=subprocess.run([sys.executable,str(script),str(root),'--p7c-report',str(report),
                             '--p7c-sha256',hashes['p7c_sha256'],'--artifact-index',str(index),
                             '--aggregate-sha256',hashes['aggregate_sha256'],'--out-dir',str(out),
                             '--within-asset-scaling'],capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stderr)
        saved=json.loads((out/'continuous_state.json').read_text())
        self.assertEqual(saved['rows'],2)
        self.assertTrue(saved['settings']['within_asset_scaling'])


if __name__=='__main__':
    unittest.main()
