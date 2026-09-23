"""Offline tests for the read-only Shadow report. No network."""
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from kalshi_bot.shadow import ShadowSession
from kalshi_bot.shadow_entry import ShadowEntry, record_starting_balance
from kalshi_bot.shadow_report import SessionPathError, analyze, canonical, resolve_session
from kalshi_bot.shadow_settlement import SETTLEMENT_METHOD

SHA = "0123456789abcdef0123456789abcdef01234567"
WINDOW = 1_700_000_000
WHEN = datetime.fromtimestamp(WINDOW + 900, tz=timezone.utc)


def _decision(**overrides):
    decision = {
        "action": "BUY_YES", "size_usd": 10.0, "strategy": "lag_arb",
        "decision_id": "d1", "p_base": 0.55, "p_real": 0.62, "p_market": 0.41,
    }
    decision.update(overrides)
    return decision


class Engine:
    def __init__(self, symbol, window=WINDOW):
        self.spec = type("S", (), {"symbol": symbol, "series_ticker": "KX"})()
        self._ticker = f"KX-{symbol}"
        self._window_id = window
        self._open_pos = None
        self._price_to_beat = 100.0
        self.synthetic_spot = type("Spot", (), {"spot_mid": None})()


class Market:
    def __init__(self, book):
        self.book = book

    def fetch_execution_book(self, ticker):
        return self.book


def _session(root, sid="s1"):
    session = ShadowSession(root, sid, "era1b", SHA, config_identity={"runner": "report"})
    record_starting_balance(session, 1000.0)
    return session


def _fill(session, symbol, action, book, spot, window=WINDOW, decision_id="d1"):
    engine = Engine(symbol, window)
    pipeline = ShadowEntry(session, Market(book))
    pipeline.submit(engine, _decision(action=action, decision_id=decision_id), 0.40)
    engine.synthetic_spot.spot_mid = spot
    pipeline.settle_if_due(engine, now=window + 900)
    return engine


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "shadow_data"
        self.root.mkdir()

    def _snapshot(self, directory):
        return {
            path: path.read_bytes()
            for path in directory.rglob("*")
            if path.is_file()
        }

    def test_no_trades_no_fill_open_and_four_closes(self):
        empty = _session(self.root, "empty")
        empty.append("decisions", {"action": "WAIT", "reason": "lag_absent", "asset": "BTC"})
        empty.close()
        report = analyze(empty.directory, now=WHEN)
        self.assertEqual(report["decisions"]["total"], 1)
        self.assertEqual(report["equity"]["realized_equity"], 1000.0)
        self.assertEqual(report["equity"]["gross_exposure"], 0.0)
        self.assertEqual(report["equity"]["drawdown_usd"], 0.0)
        self.assertIsNone(report["equity"]["fees"])
        self.assertIsNone(report["equity"]["net_equity"])
        self.assertFalse(report["equity"]["equity_feeds_sizing"])
        self.assertEqual(report["funnel"]["intended"], 0)
        self.assertTrue(report["lifecycle_chain_ok"])
        self.assertIsNone(report["performance"]["win_rate"])

        session = _session(self.root, "book")
        engine = Engine("BTC")
        ShadowEntry(session, Market({"yes": [[0.39, 1]], "no": [[0.50, 1]]})).submit(
            engine, _decision(), 0.40,
        )
        session.close()
        report = analyze(session.directory, now=WHEN)
        self.assertEqual(report["funnel"]["NO_FILL_NOT_MARKETABLE"], 1)
        self.assertEqual(report["funnel"]["filled"], 0)
        self.assertEqual(report["funnel"]["open"], 0)

        session = _session(self.root, "open")
        engine = Engine("ETH")
        ShadowEntry(session, Market({"yes": [[0.39, 100]], "no": [[0.60, 100]]})).submit(
            engine, _decision(decision_id="open-1"), 0.40,
        )
        session.close()
        report = analyze(session.directory, now=datetime.fromtimestamp(WINDOW + 100, tz=timezone.utc))
        self.assertEqual(report["funnel"]["open"], 1)
        self.assertEqual(report["open_positions"][0]["price_to_beat"], 100.0)
        self.assertGreater(report["open_positions"][0]["seconds_to_boundary"], 0)
        self.assertEqual(report["equity"]["realized_equity"], 1000.0)
        self.assertEqual(report["equity"]["gross_exposure"], 10.0)
        self.assertEqual(report["equity"]["cash_gross"], 990.0)
        self.assertEqual(report["equity"]["concurrent_open"], 1)

        cases = [
            ("yes-win", "BTC", "BUY_YES", {"yes": [[0.39, 100]], "no": [[0.60, 100]]}, 110.0, 15.0, 1),
            ("yes-loss", "ETH", "BUY_YES", {"yes": [[0.39, 100]], "no": [[0.60, 100]]}, 90.0, -10.0, 0),
            ("no-win", "SOL", "BUY_NO", {"yes": [[0.40, 100]], "no": [[0.39, 100]]}, 90.0, None, 0),
            ("no-loss", "XRP", "BUY_NO", {"yes": [[0.40, 100]], "no": [[0.39, 100]]}, 110.0, None, 1),
        ]
        for sid, symbol, action, book, spot, gross, settled in cases:
            with self.subTest(sid=sid):
                trading = _session(self.root, sid)
                _fill(trading, symbol, action, book, spot, decision_id=sid)
                trading.close()
                before = self._snapshot(trading.directory)
                report = analyze(trading.directory, now=WHEN)
                self.assertEqual(self._snapshot(trading.directory), before)
                self.assertTrue(report["lifecycle_chain_ok"])
                self.assertEqual(report["funnel"]["closed"], 1)
                close = report["closed_positions"][0]
                self.assertEqual(close["settlement_method"], SETTLEMENT_METHOD)
                self.assertIsNone(close["fees"])
                self.assertIsNone(close["net_pnl"])
                self.assertEqual(close["yes_settled"], settled)
                if gross is not None:
                    self.assertEqual(close["gross_pnl"], gross)
                    self.assertEqual(report["equity"]["realized_equity"], 1000.0 + gross)
                    self.assertIsNone(report["equity"]["net_equity"])
                else:
                    self.assertEqual(
                        close["gross_pnl"],
                        close["quantity"] * (close["side_payoff"] - close["entry"]),
                    )

    def test_multiple_assets_malformed_line_and_chain(self):
        session = _session(self.root, "multi")
        _fill(session, "BTC", "BUY_YES", {"yes": [[0.39, 100]], "no": [[0.60, 100]]}, 110.0, decision_id="b")
        _fill(session, "ETH", "BUY_NO", {"yes": [[0.40, 100]], "no": [[0.39, 100]]}, 90.0,
              window=WINDOW + 900, decision_id="e")
        session.close()
        decisions = session.directory / "decisions.jsonl"
        decisions.write_text(decisions.read_text(encoding="utf-8") + "not-json\n", encoding="utf-8")
        report = analyze(session.directory, now=datetime.fromtimestamp(WINDOW + 1800, tz=timezone.utc))
        self.assertEqual(report["malformed_advisory_lines"], 1)
        self.assertEqual(sorted(row["asset"] for row in report["closed_positions"]), ["BTC", "ETH"])
        self.assertGreater(report["performance"]["gross_pnl"], 0)
        self.assertTrue(report["lifecycle_chain_ok"])
        life = session.directory / "lifecycle.jsonl"
        lines = life.read_text(encoding="utf-8").splitlines()
        lines[0] = lines[0].replace("INTENDED", "MUTATED", 1)
        life.write_text("\n".join(lines) + "\n", encoding="utf-8")
        broken = analyze(session.directory, now=WHEN)
        self.assertFalse(broken["lifecycle_chain_ok"])
        self.assertTrue(broken["performance_withheld"])
        self.assertIsNone(broken["performance"])
        self.assertIsNone(broken["open_positions"])
        self.assertIsNone(broken["closed_positions"])
        self.assertIsNone(broken["funnel"]["intended"])
        self.assertIsNone(broken["equity"])
        self.assertGreater(broken["decisions"]["total"], 0)

    def _closed_session(self, sid="chain"):
        session = _session(self.root, sid)
        _fill(session, "BTC", "BUY_YES", {"yes": [[0.39, 100]], "no": [[0.60, 100]]}, 110.0, decision_id=sid)
        session.append("decisions", {"action": "WAIT", "reason": "lag_absent", "asset": "BTC"})
        session.close()
        return session

    def _lines(self, session):
        path = session.directory / "lifecycle.jsonl"
        return path, [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _write_lines(self, path, lines):
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_realized_equity_drawdown_after_a_later_loss(self):
        session = _session(self.root, "dd")
        _fill(session, "BTC", "BUY_YES", {"yes": [[0.39, 100]], "no": [[0.60, 100]]}, 110.0, decision_id="win")
        _fill(session, "ETH", "BUY_NO", {"yes": [[0.40, 100]], "no": [[0.39, 100]]}, 110.0,
              window=WINDOW + 900, decision_id="loss")
        session.close()
        report = analyze(session.directory, now=datetime.fromtimestamp(WINDOW + 1800, tz=timezone.utc))
        equity = report["equity"]
        self.assertEqual(equity["peak_realized_equity"], 1015.0)
        self.assertAlmostEqual(equity["realized_equity"], 1005.4)
        self.assertAlmostEqual(equity["drawdown_usd"], 9.6)
        self.assertAlmostEqual(equity["drawdown_pct"], 9.6 / 1015.0)
        self.assertAlmostEqual(equity["cumulative_return"], 5.4 / 1000.0)
        self.assertIsNone(equity["net_equity"])

    def test_torn_tail_keeps_verified_prefix(self):
        session = self._closed_session("torn")
        path, lines = self._lines(session)
        self._write_lines(path, lines + ['{"kind": "INTENDED"'])
        report = analyze(session.directory, now=WHEN)
        self.assertTrue(report["lifecycle_chain_ok"])
        self.assertTrue(report["lifecycle_torn_tail"])
        self.assertFalse(report["performance_withheld"])
        self.assertEqual(report["funnel"]["closed"], 1)
        self.assertEqual(report["performance"]["gross_pnl"], 15.0)
        self.assertIn("trailing incomplete record", report["lifecycle_chain_note"])

    def test_corrupt_final_hash_withholds_performance(self):
        session = self._closed_session("bad-hash")
        path, lines = self._lines(session)
        event = json.loads(lines[-1])
        event["sha256"] = "0" * 64
        lines[-1] = json.dumps(event)
        self._write_lines(path, lines)
        report = analyze(session.directory, now=WHEN)
        self.assertFalse(report["lifecycle_chain_ok"])
        self.assertFalse(report["lifecycle_torn_tail"])
        self.assertIsNone(report["performance"])
        self.assertIsNone(report["closed_positions"])
        self.assertIsNone(report["funnel"]["filled"])
        self.assertGreater(report["decisions"]["total"], 0)

    def test_middle_hash_and_previous_link_fail_closed(self):
        session = self._closed_session("middle")
        path, lines = self._lines(session)
        self.assertGreaterEqual(len(lines), 3)
        middle = json.loads(lines[1])
        middle["sha256"] = "1" * 64
        lines[1] = json.dumps(middle)
        self._write_lines(path, lines)
        report = analyze(session.directory, now=WHEN)
        self.assertFalse(report["lifecycle_chain_ok"])
        self.assertIsNone(report["open_positions"])
        self.assertIsNone(report["performance"])

        session = self._closed_session("link")
        path, lines = self._lines(session)
        last = json.loads(lines[-1])
        last.pop("sha256")
        last["previous_hash"] = "ab" * 32
        last["sha256"] = hashlib.sha256(canonical(last).encode("utf-8")).hexdigest()
        lines[-1] = json.dumps(last)
        self._write_lines(path, lines)
        report = analyze(session.directory, now=WHEN)
        self.assertFalse(report["lifecycle_chain_ok"])
        self.assertIn("previous hash mismatch", report["lifecycle_chain_note"])
        self.assertIsNone(report["closed_positions"])
        self.assertIsNone(report["performance"])

    def test_path_traversal_is_rejected(self):
        session = self._closed_session("inside")
        self.assertEqual(resolve_session(self.root, "inside"), session.directory.resolve())
        for name in ("../outside", "inside/../inside", "..", r"..\outside", "a/b"):
            with self.subTest(name=name):
                with self.assertRaises(SessionPathError):
                    resolve_session(self.root, name)

    def test_sources_do_not_trade(self):
        report_source = Path(__file__).with_name("shadow_report.py").read_text(encoding="utf-8")
        app_source = Path(__file__).resolve().parents[1].joinpath("dashboard", "shadow_app.py").read_text(encoding="utf-8")
        for source in (report_source, app_source):
            self.assertNotIn("requests.post", source)
            self.assertNotIn("get_market_result", source)
            self.assertNotIn("place_order", source)
            self.assertNotIn(".write_text", source)
            self.assertNotIn("ShadowJournal", source)
