"""Synthetic-only invariants for the offline P7B builder."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from kalshi_bot.research.market_state_dataset import RULE_A, RULE_V1, build, safe_path, write_artifacts


class MarketStateDatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_build(self, *sources, **cohort):
        manifest = dict(mode="paper", session_tag="fixture", sample_kind="development", sources=[])
        manifest.update(cohort)
        for i, (kind, rows) in enumerate(sources):
            path = self.root / f"source{i}.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            manifest["sources"].append(dict(type=kind, path=str(path)))
        return build(manifest)

    def row(self, **values):
        return dict(dict(asset="BTC", window_id_ts=100, ts="tick", p_real=.6, time_remaining=65), **values)

    def boundary(self, **values):
        return self.row(**dict(dict(schema_version=2, record_kind="boundary_snapshot", target_tte=60, raw_tte=61.2, tau_used=60, snapshot_ts="boundary", fresh_venue_count=1, dislocation=0, realized_vol_value=.30), **values))

    def outcome(self, **values):
        return self.row(**dict(dict(schema_version=2, record_kind="window_outcome", actual_outcome="YES", yes_settled=1, outcome_source="spot_vs_price_to_beat", exit_spot=999), **values))

    def test_selection_and_separate_times(self):
        rows = [self.row(ts="first"), self.row(ts="last", time_remaining=60), self.row(ts="later", time_remaining=59), self.row(p_real=float("nan")), self.row(time_remaining=None)]
        result = self.run_build(("decision", rows), ("research_v2", [self.boundary(), self.boundary(asset="ETH", target_tte=30)]))
        a, b = result["preferred_ge_60s"], result["boundary_60"]
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["observation_timestamp"], "last")
        self.assertEqual(len(b), 1)
        self.assertEqual([b[0]["features"][f] for f in ("time_remaining", "raw_tte", "tau_used", "target_tte")], [65, 61.2, 60, 60])
        self.assertIsNone(a[0]["features"]["realized_vol_value"])
        self.assertEqual(result["population_overlap"][0]["membership"], "both")
        self.assertNotIn("features", result["population_overlap"][0])

    def test_last_logged_order_does_not_freeze_or_sort(self):
        result = self.run_build(("decision", [self.row(ts="z", time_remaining=60), self.row(time_remaining=59), self.row(ts="a", time_remaining=62)]))
        self.assertEqual(result["preferred_ge_60s"][0]["observation_timestamp"], "a")

    def test_protected_paths_and_fractional_identity_rejected(self):
        for name in ("logs/input.jsonl", "sessions/input.jsonl", ".env"):
            with self.assertRaises(ValueError):
                safe_path(self.root / name)
        with self.assertRaises(ValueError):
            self.run_build(("decision", [self.row(window_id_ts=100.5)]))

    def test_cli_manifest_relative_paths(self):
        self.run_build(("decision", [self.row()]))
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps(dict(mode="paper", session_tag="fixture", sample_kind="development", sources=[dict(type="decision", path="source0.jsonl")])), encoding="utf-8")
        script = Path(__file__).resolve().parents[1] / "scripts" / "build_market_state_dataset.py"
        result = subprocess.run([sys.executable, str(script), str(manifest), "--out-dir", str(self.root / "cli")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(list((self.root / "cli").iterdir())), 6)

    def test_state_and_outcome_isolation(self):
        result = self.run_build(("research_v2", [self.boundary(), self.outcome(asset="ETH"), self.outcome()]))
        row = result["boundary_60"][0]
        self.assertIsNone(row["features"]["kalshi_spread"])
        self.assertEqual(row["features"]["dislocation"], 0)
        self.assertEqual(row["features"]["fresh_venue_count"], 1)
        self.assertEqual(row["features"]["realized_vol_value"], .30)
        self.assertNotIn("realized_vol_is_fallback", row["features"])
        self.assertNotIn("exit_spot", row["features"])
        self.assertEqual(row["outcome"]["provenance"]["asset"], "BTC")
        self.assertEqual(row["mode"], "paper")
        self.assertEqual(row["availability"]["lag_signal"], "structurally_unavailable")

    def test_wrong_identity_does_not_join(self):
        for outcome in (self.outcome(asset="ETH"), self.outcome(window_id_ts=101), self.outcome(git_sha="other"), self.outcome(ticker="other")):
            with self.subTest(outcome=outcome):
                result = self.run_build(("research_v2", [self.boundary(), outcome]))
                self.assertIsNone(result["boundary_60"][0]["outcome"])

    def test_decision_outcome_requires_persisted_provenance(self):
        identity = dict(session_tag="fixture", dry_run=True, git_sha="fixture-sha", ticker="BTC-test")
        result = self.run_build(("decision", [self.row(**identity)]), ("research_v2", [self.outcome(**identity)]))
        self.assertIsNotNone(result["preferred_ge_60s"][0]["outcome"])
        result = self.run_build(("decision", [self.row()]), ("research_v2", [self.outcome()]))
        self.assertIsNone(result["preferred_ge_60s"][0]["outcome"])

    def test_raw_features_do_not_leak_future_or_portfolio_fields(self):
        result = self.run_build(("decision", [self.row(raw_features=dict(obi=.2, response_gap=.1, exit_spot=999, portfolio_gross=.5))]))
        self.assertEqual(result["preferred_ge_60s"][0]["features"]["raw_features"], dict(obi=.2, response_gap=.1))

    def test_duplicates_and_mixed_cohorts_fail(self):
        for rows in ([self.boundary(), self.boundary()], [self.boundary(mode="live")], [self.boundary(session_tag="other")], [self.boundary(dry_run=False)], [self.boundary(), self.outcome(), self.outcome()]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.run_build(("research_v2", rows))
        with self.assertRaises(ValueError):
            self.run_build(("decision", []), session_tag="p6c_d1_validation")

    def test_abstention_is_reason_only_and_last_recognized(self):
        result = self.run_build(("decision", [self.row(p_real=None, reason="signal_warmup"), self.row(p_real=None, reason="vol_too_high(0.99)"), self.row(p_real=None, reason="unknown")]))
        row = result["abstention_census"][0]
        self.assertEqual(row["reason_category"], "excessive_volatility")
        self.assertNotIn("features", row)
        self.assertNotIn("realized_vol_value", row)
        self.assertEqual(result["preferred_ge_60s"], [])

    def test_warmup_then_eligible_is_not_abstention_across_files(self):
        result = self.run_build(
            ("decision", [self.row(p_real=None, reason="signal_warmup")]),
            ("decision", [self.row()]),
        )
        self.assertEqual(len(result["preferred_ge_60s"]), 1)
        self.assertEqual(result["abstention_census"], [])

    def test_eligible_trade_then_position_open_is_not_abstention(self):
        result = self.run_build(("decision", [self.row(action="BUY_YES"), self.row(p_real=None, reason="position_open")]))
        self.assertEqual(len(result["preferred_ge_60s"]), 1)
        self.assertEqual(result["abstention_census"], [])

    def test_never_eligible_census_preserves_asset_window_and_last_reason(self):
        result = self.run_build(
            ("decision", [self.row(p_real=None, reason="signal_warmup"), self.row(asset="ETH")]),
            ("decision", [self.row(p_real=None, reason="spot_confidence_low"), self.row(p_real=None, reason="unknown")]),
        )
        census = result["abstention_census"]
        self.assertEqual(len(census), 1)
        self.assertEqual((census[0]["asset"], census[0]["window_id_ts"]), ("BTC", 100))
        self.assertEqual(census[0]["reason"], "spot_confidence_low")
        self.assertEqual(census[0]["source_line"], 1)
        self.assertNotIn("features", census[0])

    def test_v1_provenance_is_eligibility_only_and_documented(self):
        v1 = self.run_build(("research_v1", [self.outcome(schema_version=1)]))
        decisions = self.run_build(("decision", [self.row()]))
        self.assertEqual(v1["preferred_ge_60s"][0]["selection_rule"], RULE_V1)
        self.assertEqual(decisions["preferred_ge_60s"][0]["selection_rule"], RULE_A)
        self.assertNotEqual(RULE_V1, RULE_A)
        self.assertEqual(v1["schema_provenance"]["selection_rules"]["A_research_v1"], RULE_V1)
        doc = (Path(__file__).parent / "research" / "P7B_DATASET.md").read_text(encoding="utf-8-sig")
        self.assertIn(RULE_V1, doc)
        self.assertIn(RULE_A, doc)

    def test_ineligible_v1_fails_closed(self):
        for values in (dict(p_real=None), dict(p_real=float("nan")), dict(time_remaining=59), dict(time_remaining=None), dict(time_remaining=float("inf"))):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, "ineligible research_v1 preferred row"):
                self.run_build(("research_v1", [self.outcome(schema_version=1, **values)]))

    def test_decision_schema_availability_and_variable_fallback_semantics(self):
        result = self.run_build(("decision", [self.row(kalshi_spread=None, target_tte=None, realized_vol_value=.30)]))
        row = result["preferred_ge_60s"][0]
        counts = result["coverage_missingness"]["preferred_ge_60s"]["fields"]
        for field in ("target_tte", "actual_tte", "raw_distance", "relative_distance", "log_distance"):
            self.assertEqual(row["availability"][field], "structurally_unavailable")
            self.assertEqual(counts[field], {"structurally_unavailable": 1})
        for field in ("spot_now", "kalshi_spread", "raw_tte", "tau_used"):
            self.assertEqual(row["availability"][field], "missing")
            self.assertEqual(counts[field], {"missing": 1})
        self.assertEqual(counts["realized_vol_value"], {"observed": 1})
        self.assertEqual(row["features"]["realized_vol_value"], .30)
        self.assertEqual(result["schema_provenance"]["variable_semantics"]["realized_vol_value"]["provenance"], "ambiguous_fallback")
        self.assertNotIn("realized_vol_is_fallback", row["features"])

    def test_taxonomy_retains_actual_series_semantics(self):
        result = self.run_build(("decision", [self.row(dist_from_threshold=-2, kalshi_prob_change_1s=.01, raw_features=dict(lag_signal=.2, response_gap=.3))]))
        taxonomy = result["schema_provenance"]["taxonomy"]
        self.assertIn("dist_from_threshold", taxonomy["market"])
        self.assertNotIn("dist_from_threshold", taxonomy["model"])
        self.assertIn("kalshi_prob_change_1s", taxonomy["market"])
        self.assertNotIn("kalshi_prob_change_1s", taxonomy["estimated_response"])
        row = result["preferred_ge_60s"][0]
        self.assertEqual(row["features"]["dist_from_threshold"], -2)
        self.assertEqual(row["features"]["kalshi_prob_change_1s"], .01)
        self.assertTrue(any("nested lag_signal and response_gap are estimates" in s for s in result["schema_provenance"]["semantics"]))

    def test_v1_and_missingness(self):
        v1 = self.outcome(schema_version=1)
        result = self.run_build(("research_v1", [v1]))
        row = result["preferred_ge_60s"][0]
        self.assertEqual(row["outcome"]["outcome_source"], "spot_vs_price_to_beat")
        self.assertEqual(row["availability"]["kalshi_spread"], "structurally_unavailable")
        self.assertEqual(row["availability"]["spot_now"], "missing")
        with self.assertRaises(ValueError):
            self.run_build(("research_v1", [v1]), ("decision", []))

    def test_overlap_all_memberships_and_live(self):
        result = self.run_build(("decision", [self.row(), self.row(asset="ETH")]), ("research_v2", [self.boundary(asset="ETH"), self.boundary(asset="SOL")]), mode="live")
        self.assertEqual([r["membership"] for r in result["population_overlap"]], ["A_only", "both", "B_only"])
        self.assertTrue(all(r["mode"] == "live" for r in result["boundary_60"]))

    def test_artifacts_reproducible_and_sources_unchanged(self):
        result = self.run_build(("research_v2", [self.boundary(), self.outcome()]))
        source = self.root / "source0.jsonl"
        before = source.read_bytes()
        for name in ("out1", "out2"):
            write_artifacts(result, self.root / name)
        self.assertEqual(len(list((self.root / "out1").iterdir())), 6)
        for path in (self.root / "out1").iterdir():
            self.assertEqual(path.read_bytes(), (self.root / "out2" / path.name).read_bytes())
        self.assertEqual(before, source.read_bytes())
        with self.assertRaises(ValueError):
            write_artifacts(result, self.root / "out1")


if __name__ == "__main__":
    unittest.main()
