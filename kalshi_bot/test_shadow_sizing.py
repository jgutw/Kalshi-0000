"""Era 1C realized-gross-equity sizing. No network. Does not touch live sessions."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from kalshi_bot.config import cfg
from kalshi_bot.shadow import ShadowSession
from kalshi_bot.shadow_journal import canonical
from kalshi_bot.shadow_entry import ShadowEntry, realized_gross_equity, record_starting_balance
from kalshi_bot.shadow_report import analyze
from kalshi_bot.signal_engine import kelly_binary, vol_position_scalar
from kalshi_bot.sim_state import OpenPosition, SimState

SHA = "0123456789abcdef0123456789abcdef01234567"
WINDOW = 1_700_000_000


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


def _session(root, sid, balance=1000.0):
    session = ShadowSession(root, sid, "era1c", SHA, config_identity={"runner": "test"})
    record_starting_balance(session, balance)
    return session


def _close(session, symbol, spot, decision_id, window=WINDOW):
    engine = Engine(symbol, window)
    ShadowEntry(session, Market({"yes": [[0.39, 100]], "no": [[0.60, 100]]})).submit(
        engine, _decision(decision_id=decision_id), 0.40,
    )
    engine.synthetic_spot.spot_mid = spot
    ShadowEntry(session, Market({"yes": [[0.39, 100]], "no": [[0.60, 100]]})).settle_if_due(
        engine, now=window + 900,
    )
    return engine


class SizingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "shadow_data"
        self.root.mkdir()

    def test_win_and_loss_move_the_denominator_and_restart_matches(self):
        session = _session(self.root, "equity")
        equity, block = realized_gross_equity(session)
        self.assertEqual(equity, 1000.0)
        self.assertIsNone(block)
        _close(session, "BTC", 110.0, "win")
        equity, block = realized_gross_equity(session)
        self.assertEqual(equity, 1015.0)
        self.assertIsNone(block)
        _close(session, "ETH", 90.0, "loss", window=WINDOW + 900)
        equity, _block = realized_gross_equity(session)
        self.assertEqual(equity, 1005.0)
        session.close()
        recovered = ShadowSession.recover(self.root, "equity")
        again, block = realized_gross_equity(recovered)
        self.assertEqual(again, 1000.0 + 15.0 - 10.0)
        self.assertIsNone(block)
        recovered.close()

    def test_open_premium_does_not_reduce_equity_and_exposure_uses_it(self):
        session = _session(self.root, "open")
        engine = Engine("BTC")
        ShadowEntry(session, Market({"yes": [[0.39, 100]], "no": [[0.60, 100]]})).submit(
            engine, _decision(decision_id="open"), 0.40,
        )
        equity, block = realized_gross_equity(session)
        self.assertEqual(equity, 1000.0)
        self.assertIsNone(block)
        premium = engine._open_pos.amount_usdc
        self.assertEqual(premium, 10.0)
        fraction = SimState().gross_open_exposure([engine._open_pos], denominator=equity)
        self.assertEqual(fraction, 0.01)
        shrunk = 900.0
        self.assertGreater(premium / shrunk, premium / equity)
        self.assertFalse(engine._open_pos is None)
        session.close()

    def test_duplicate_close_does_not_double_count(self):
        session = _session(self.root, "once")
        engine = _close(session, "BTC", 110.0, "once")
        before, _block = realized_gross_equity(session)
        with self.assertRaises(Exception):
            session.journal.settle_research(
                engine._open_pos.order_id if engine._open_pos else "missing",
                observed_spot=110.0, observation_unix=WINDOW + 900,
            )
        after, _block = realized_gross_equity(session)
        self.assertEqual(after, before)
        session.close()

    def test_corrupt_chain_blocks_without_fallback(self):
        session = _session(self.root, "bad")
        _close(session, "BTC", 110.0, "bad")
        session.close()
        path = session.directory / "lifecycle.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[0])
        event["sha256"] = "0" * 64
        lines[0] = json.dumps(event)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        leftover = type("S", (), {"directory": session.directory})()
        equity, reason = realized_gross_equity(leftover)
        self.assertIsNone(equity)
        self.assertEqual(reason, "sizing_blocked:lifecycle_integrity")

    def test_nonfinite_equity_blocks_without_fallback(self):
        session = _session(self.root, "inf")
        _close(session, "BTC", 110.0, "inf")
        session.close()
        path = session.directory / "lifecycle.jsonl"
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        event = json.loads(lines[-1])
        self.assertEqual(event["kind"], "CLOSED")
        event.pop("sha256")
        event["payload"]["gross_pnl"] = 1e308
        event["sha256"] = hashlib.sha256(canonical(event).encode()).hexdigest()
        lines[-1] = canonical(event)
        extra = json.loads(lines[-1])
        extra.pop("sha256")
        extra["seq"] += 1
        extra["event_id"] = f"{extra['shadow_session_id']}:{extra['seq']}"
        extra["previous_hash"] = event["sha256"]
        extra["payload"]["close_id"] = extra["payload"]["close_id"] + ":again"
        extra["payload"]["gross_pnl"] = 1e308
        extra["sha256"] = hashlib.sha256(canonical(extra).encode()).hexdigest()
        lines.append(canonical(extra))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        leftover = type("S", (), {"directory": session.directory})()
        equity, reason = realized_gross_equity(leftover)
        self.assertIsNone(equity)
        self.assertEqual(reason, "sizing_blocked:nonfinite_equity")

    def test_nonpositive_equity_blocks(self):
        session = _session(self.root, "flat", balance=10.0)
        _close(session, "BTC", 90.0, "loss")
        equity, reason = realized_gross_equity(session)
        self.assertIsNone(equity)
        self.assertEqual(reason, "sizing_blocked:nonpositive_equity")
        session.close()

    def test_risk_constants_and_vol_scalar_are_unchanged(self):
        self.assertEqual(cfg.KELLY_FRACTION, 0.50)
        self.assertEqual(cfg.MAX_POS_PCT, 0.08)
        self.assertEqual(cfg.PORTFOLIO_GROSS_CAP, 0.30)
        self.assertEqual(cfg.MIN_TRADE_USD, 5.0)
        self.assertEqual(vol_position_scalar(5.0), 0.5)
        self.assertEqual(vol_position_scalar(9.0), 0.0)
        frac = kelly_binary(0.70, 0.40)
        self.assertLessEqual(frac, cfg.MAX_POS_PCT)
        equity = 1100.0
        size = min(equity * frac, equity * cfg.MAX_POS_PCT)
        self.assertLessEqual(size, equity * 0.08 + 1e-9)
        self.assertGreater(size, 1000.0 * frac)

    def test_era1b_report_stays_false_and_marker_turns_it_true(self):
        session = _session(self.root, "report")
        session.close()
        report = analyze(session.directory)
        self.assertFalse(report["equity"]["equity_feeds_sizing"])
        marked = _session(self.root, "marked")
        marked.append("operational_events", {"event": "sizing_basis", "equity_feeds_sizing": True})
        marked.close()
        report = analyze(marked.directory)
        self.assertTrue(report["equity"]["equity_feeds_sizing"])

    def test_loss_can_leave_old_premium_above_thirty_percent(self):
        position = OpenPosition(
            window_id=1, market_ticker="KX", asset="BTC", side="yes",
            entry_price=0.40, contracts=1000, amount_usdc=400.0, entered_at=0, price_to_beat=100.0,
        )
        before = SimState().gross_open_exposure([position], denominator=2000.0)
        after = SimState().gross_open_exposure([position], denominator=1000.0)
        self.assertLessEqual(before + cfg.MAX_POS_PCT, cfg.PORTFOLIO_GROSS_CAP)
        self.assertGreater(after + cfg.MAX_POS_PCT, cfg.PORTFOLIO_GROSS_CAP)
        self.assertEqual(position.amount_usdc, 400.0)
