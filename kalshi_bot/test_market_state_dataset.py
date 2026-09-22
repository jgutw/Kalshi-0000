"""Synthetic-only invariants for the offline P7B builder."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from kalshi_bot.research.market_state_dataset import RULE_A, RULE_B, RULE_V1, build, safe_path, write_artifacts


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

    def fence(self, **cohort):
        cohort.setdefault("start_utc", "2026-08-09T00:00:00Z")
        cohort.setdefault("end_utc", "2026-09-01T00:00:00Z")
        return cohort

    def at(self, iso):
        text = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
        return datetime.fromisoformat(text).astimezone(timezone.utc)

    def window_at(self, iso):
        return int(self.at(iso).timestamp())

    def decision_at(self, iso, **values):
        return self.row(ts=iso, **values)

    def boundary_at(self, iso, **values):
        return self.boundary(ts=iso, snapshot_ts=iso, capture_time_utc=iso, **values)

    def outcome_at(self, iso, **values):
        row = self.outcome(window_id_ts=self.window_at(iso), **values)
        row.pop("ts", None)
        row.pop("snapshot_ts", None)
        return row

    def test_unfenced_build_is_unchanged(self):
        result = self.run_build(("decision", [self.row(ts="z", time_remaining=60), self.row(time_remaining=59), self.row(ts="a", time_remaining=62)]))
        self.assertEqual(result["preferred_ge_60s"][0]["observation_timestamp"], "a")
        self.assertEqual(result["preferred_ge_60s"][0]["selection_rule"], RULE_A)
        self.assertEqual(result["schema_provenance"]["builder_schema_version"], 2)
        self.assertEqual(result["schema_provenance"]["selection_rules"], {"A_decision": RULE_A, "A_research_v1": RULE_V1, "B": RULE_B})
        fence = result["schema_provenance"]["cohort_fence"]
        self.assertFalse(fence["applied"])
        self.assertIsNone(fence["start_utc"])
        self.assertIsNone(fence["end_utc"])
        self.assertEqual(fence["records_before"], fence["records_after"])
        self.assertEqual(fence["records_excluded"], 0)
        self.assertNotIn("2026-08-09", Path(__file__).with_name("research").joinpath("market_state_dataset.py").read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            self.run_build(("decision", [self.decision_at("2026-08-15T00:00:00Z")]), start_utc="2026-08-09T00:00:00Z")

    def test_cohort_fence_boundary_instants(self):
        rows = [
            self.decision_at("2026-08-08T23:59:59Z", window_id_ts=1, p_real=.1),
            self.decision_at("2026-08-09T00:00:00Z", window_id_ts=2, p_real=.2),
            self.decision_at("2026-08-09T01:00:00+01:00", window_id_ts=3, p_real=.3),
            self.decision_at("2026-08-15T12:00:00Z", window_id_ts=4, p_real=.4),
            self.decision_at("2026-08-31T23:59:59.999999Z", window_id_ts=5, p_real=.5),
            self.decision_at("2026-09-01T00:00:00Z", window_id_ts=6, p_real=.6),
            self.decision_at("2026-09-01T00:00:01Z", window_id_ts=7, p_real=.7),
        ]
        result = self.run_build(("decision", rows), **self.fence())
        kept = [row["window_id_ts"] for row in result["preferred_ge_60s"]]
        self.assertEqual(kept, [2, 3, 4, 5])
        self.assertEqual([row["observation_timestamp"] for row in result["preferred_ge_60s"]], [rows[i]["ts"] for i in (1, 2, 3, 4)])
        fence = result["schema_provenance"]["cohort_fence"]
        self.assertEqual(fence["start_utc"], "2026-08-09T00:00:00Z")
        self.assertEqual(fence["end_utc"], "2026-09-01T00:00:00Z")
        self.assertEqual(fence["interval"], "start <= cohort_time < end")
        self.assertEqual((fence["records_before"], fence["records_after"], fence["records_excluded"]), (7, 4, 3))

    def test_mixed_sessions_keep_source_identity_and_line_order(self):
        first = [
            self.decision_at("2026-08-10T00:00:00Z", window_id_ts=10, p_real=.2),
            self.decision_at("2026-07-01T00:00:00Z", window_id_ts=10, p_real=.9),
            self.decision_at("2026-08-20T00:00:00Z", window_id_ts=10, p_real=.4),
        ]
        second = [
            self.decision_at("2026-09-02T00:00:00Z", window_id_ts=10, p_real=.8),
            self.decision_at("2026-08-21T00:00:00Z", window_id_ts=11, p_real=.5),
        ]
        text = json.dumps(second[0]) + "\n\n" + json.dumps(second[1]) + "\n"
        early = self.root / "early.jsonl"
        early.write_text("".join(json.dumps(r) + "\n" for r in first), encoding="utf-8")
        later = self.root / "later.jsonl"
        later.write_text(text, encoding="utf-8")
        manifest = dict(mode="paper", session_tag="fixture", sample_kind="historical", sources=[
            dict(type="decision", path=str(early)), dict(type="decision", path=str(later)),
        ], **self.fence())
        result = build(manifest)
        chosen = {row["window_id_ts"]: row for row in result["preferred_ge_60s"]}
        self.assertEqual(chosen[10]["features"]["p_real"], .4)
        self.assertEqual(chosen[10]["source_line"], 3)
        self.assertEqual(chosen[10]["source_file"], str(early.resolve()))
        self.assertEqual(chosen[10]["observation_timestamp"], "2026-08-20T00:00:00Z")
        self.assertEqual(chosen[10]["selection_rule"], RULE_A)
        self.assertEqual(chosen[11]["source_line"], 3)
        self.assertEqual(chosen[11]["source_sha256"], hashlib.sha256(later.read_bytes()).hexdigest())
        self.assertEqual([row["source_line"] for row in result["preferred_ge_60s"]], [3, 3])
        counts = result["schema_provenance"]["cohort_fence"]["by_source"]
        self.assertEqual([(c["records_before"], c["records_after"], c["records_excluded"]) for c in counts], [(3, 2, 1), (2, 1, 1)])
        self.assertEqual(result["schema_provenance"]["sources"][0].keys(), {"path", "type", "sha256"})

    def test_invalid_cohort_timestamps_fail_closed(self):
        missing = self.decision_at("2026-08-15T00:00:00Z")
        missing.pop("ts")
        ambiguous_outcome = self.outcome_at("2026-08-15T00:00:00Z")
        ambiguous_outcome["ts"] = "2026-08-15T00:00:00Z"
        fractional_window = self.outcome_at("2026-08-15T00:00:00Z")
        fractional_window["window_id_ts"] = 100.5
        disagreeing_clocks = self.boundary_at("2026-08-15T00:00:00Z")
        disagreeing_clocks["ts"] = "2026-08-16T00:00:00Z"
        cases = (
            self.decision_at("2026-08-15T00:00:00"),
            missing,
            self.decision_at("not-a-timestamp", window_id_ts=4),
            disagreeing_clocks,
            ambiguous_outcome,
            fractional_window,
        )
        for row in cases:
            kind = "research_v2" if row.get("schema_version") == 2 else "decision"
            with self.subTest(row=row), self.assertRaisesRegex(ValueError, "cohort timestamp cannot establish membership"):
                self.run_build((kind, [row]), **self.fence())
        with self.assertRaisesRegex(ValueError, "both start_utc and end_utc"):
            self.run_build(("decision", [self.decision_at("2026-08-15T00:00:00Z")]), end_utc="2026-09-01T00:00:00Z")
        with self.assertRaisesRegex(ValueError, "start must be before end"):
            self.run_build(("decision", [self.decision_at("2026-08-15T00:00:00Z")]), start_utc="2026-09-01T00:00:00Z", end_utc="2026-08-09T00:00:00Z")

    def test_population_rules_apply_only_after_the_fence(self):
        decisions = [
            self.decision_at("2026-08-10T00:00:00Z", window_id_ts=20, time_remaining=70, p_real=.2),
            self.decision_at("2026-09-02T00:00:00Z", window_id_ts=20, time_remaining=80, p_real=.9),
            self.decision_at("2026-08-11T00:00:00Z", window_id_ts=20, time_remaining=59, p_real=.3),
        ]
        result = self.run_build(("decision", decisions), **self.fence())
        self.assertEqual(result["preferred_ge_60s"][0]["features"]["p_real"], .2)
        self.assertEqual(result["preferred_ge_60s"][0]["selection_rule"], RULE_A)
        self.assertEqual(result["preferred_ge_60s"][0]["source_line"], 1)
        boundaries = [
            self.boundary_at("2026-08-12T00:00:00Z", asset="ETH"),
            self.boundary_at("2026-08-12T00:00:00Z", asset="ETH", target_tte=30),
            self.boundary_at("2026-09-03T00:00:00Z", asset="SOL"),
        ]
        bounded = self.run_build(("research_v2", boundaries), **self.fence())
        self.assertEqual(len(bounded["boundary_60"]), 1)
        self.assertEqual(bounded["boundary_60"][0]["asset"], "ETH")
        self.assertEqual(bounded["boundary_60"][0]["selection_rule"], RULE_B)
        self.assertEqual(bounded["boundary_60"][0]["features"]["target_tte"], 60)
        self.assertEqual(bounded["boundary_60"][0]["source_line"], 1)
        with self.assertRaisesRegex(ValueError, "duplicate boundary identity"):
            self.run_build(("research_v2", [self.boundary_at("2026-08-12T00:00:00Z"), self.boundary_at("2026-08-13T00:00:00Z")]), **self.fence())
        inside = self.outcome(schema_version=1, snapshot_ts="2026-08-14T00:00:00Z")
        inside.pop("ts", None)
        outside = self.outcome(schema_version=1, window_id_ts=101, time_remaining=10, p_real=None, snapshot_ts="2026-07-01T00:00:00Z")
        outside.pop("ts", None)
        v1 = self.run_build(("research_v1", [outside, inside]), **self.fence())
        self.assertEqual(v1["preferred_ge_60s"][0]["selection_rule"], RULE_V1)
        self.assertEqual(v1["preferred_ge_60s"][0]["source_line"], 2)
        with self.assertRaisesRegex(ValueError, "ineligible research_v1 preferred row"):
            self.run_build(("research_v1", [dict(inside, time_remaining=10)]), **self.fence())

    def test_census_uses_only_the_filtered_cohort(self):
        rows = [
            self.decision_at("2026-08-10T00:00:00Z", window_id_ts=30, p_real=None, reason="signal_warmup"),
            self.decision_at("2026-07-01T00:00:00Z", window_id_ts=31, p_real=None, reason="vol_too_high"),
            self.decision_at("2026-08-11T00:00:00Z", window_id_ts=32, p_real=None, reason="signal_warmup"),
            self.decision_at("2026-08-12T00:00:00Z", window_id_ts=32, p_real=.4),
            self.decision_at("2026-08-13T00:00:00Z", window_id_ts=33, p_real=None, reason="spot_confidence_low"),
            self.decision_at("2026-09-05T00:00:00Z", window_id_ts=33, p_real=.7),
            self.decision_at("2026-08-14T00:00:00Z", window_id_ts=30, p_real=None, reason="unknown"),
        ]
        result = self.run_build(("decision", rows), **self.fence())
        census = {(row["window_id_ts"], row["reason"], row["source_line"]) for row in result["abstention_census"]}
        self.assertEqual(census, {(30, "signal_warmup", 1), (33, "spot_confidence_low", 5)})
        self.assertEqual(result["preferred_ge_60s"][0]["window_id_ts"], 32)

    def test_window_outcome_uses_window_clock_and_does_not_invent_rows(self):
        window = self.window_at("2026-08-20T00:00:00Z")
        snapshot = self.boundary_at("2026-08-20T00:00:00Z", window_id_ts=window, git_sha="same", ticker="BTC")
        inside = self.outcome_at("2026-08-20T00:00:00Z", git_sha="same", ticker="BTC")
        outside = self.outcome_at("2026-09-02T00:00:00Z", asset="ETH", git_sha="same", ticker="ETH")
        kept = self.run_build(("research_v2", [snapshot, outside, inside]), **self.fence())
        self.assertEqual(len(kept["boundary_60"]), 1)
        self.assertEqual(kept["boundary_60"][0]["outcome"]["provenance"]["window_id_ts"], self.window_at("2026-08-20T00:00:00Z"))
        self.assertEqual(kept["boundary_60"][0]["observation_timestamp"], "2026-08-20T00:00:00Z")
        dropped = self.run_build(("research_v2", [self.boundary_at("2026-07-01T00:00:00Z"), inside]), **self.fence())
        self.assertEqual(dropped["boundary_60"], [])
        self.assertEqual(dropped["schema_provenance"]["cohort_fence"]["records_excluded"], 1)
        edges = self.run_build(("research_v2", [
            self.outcome_at("2026-08-09T00:00:00Z", asset="DOGE"),
            self.outcome_at("2026-08-31T23:59:59Z", asset="SOL"),
            self.outcome_at("2026-09-01T00:00:00Z", asset="XRP"),
        ]), **self.fence())
        self.assertEqual((edges["schema_provenance"]["cohort_fence"]["records_after"], edges["schema_provenance"]["cohort_fence"]["records_excluded"]), (2, 1))
        self.assertEqual(edges["boundary_60"], [])

    def test_cli_fence_is_explicit_and_optional(self):
        self.run_build(("decision", [self.decision_at("2026-08-15T00:00:00Z", window_id_ts=40), self.decision_at("2026-09-02T00:00:00Z", window_id_ts=41)]))
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps(dict(mode="paper", session_tag="fixture", sample_kind="historical", sources=[dict(type="decision", path="source0.jsonl")])), encoding="utf-8")
        script = Path(__file__).resolve().parents[1] / "scripts" / "build_market_state_dataset.py"
        bare = subprocess.run([sys.executable, str(script), str(manifest), "--out-dir", str(self.root / "bare")], capture_output=True, text=True)
        self.assertEqual(bare.returncode, 0, bare.stderr)
        bare_meta = json.loads((self.root / "bare" / "schema_provenance.json").read_text(encoding="utf-8"))
        self.assertFalse(bare_meta["cohort_fence"]["applied"])
        self.assertEqual(len((self.root / "bare" / "preferred_ge_60s.jsonl").read_text(encoding="utf-8").splitlines()), 2)
        one_side = subprocess.run([sys.executable, str(script), str(manifest), "--out-dir", str(self.root / "one"), "--start-utc", "2026-08-09T00:00:00Z"], capture_output=True, text=True)
        self.assertNotEqual(one_side.returncode, 0)
        fenced = subprocess.run([sys.executable, str(script), str(manifest), "--out-dir", str(self.root / "fenced"), "--start-utc", "2026-08-09T00:00:00Z", "--end-utc", "2026-09-01T00:00:00Z"], capture_output=True, text=True)
        self.assertEqual(fenced.returncode, 0, fenced.stderr)
        meta = json.loads((self.root / "fenced" / "schema_provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["cohort_fence"]["records_after"], 1)
        self.assertEqual(meta["cohort_fence"]["start_utc"], "2026-08-09T00:00:00Z")
        conflict = dict(json.loads(manifest.read_text(encoding="utf-8")), start_utc="2026-08-01T00:00:00Z", end_utc="2026-08-02T00:00:00Z")
        (self.root / "conflict.json").write_text(json.dumps(conflict), encoding="utf-8")
        bad = subprocess.run([sys.executable, str(script), str(self.root / "conflict.json"), "--out-dir", str(self.root / "bad"), "--start-utc", "2026-08-09T00:00:00Z", "--end-utc", "2026-09-01T00:00:00Z"], capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)


if __name__ == "__main__":
    unittest.main()
