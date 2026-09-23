"""Offline Shadow research settlement. No network and no production artifacts."""
import hashlib
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from kalshi_bot.config import cfg
from kalshi_bot.shadow import ShadowSession
from kalshi_bot.shadow_entry import ShadowEntry, private_sim, record_starting_balance
from kalshi_bot.shadow_journal import JournalError, canonical, strict_loads
from kalshi_bot.shadow_settlement import (
    SETTLEMENT_METHOD,
    gross_pnl,
    research_close_payload,
    side_payoff,
    yes_settled,
)
import run_shadow

SHA = "0123456789abcdef0123456789abcdef01234567"
WINDOW = 1_700_000_000
CLOSE = WINDOW + cfg.WINDOW_SECS


def _decision(**overrides):
    decision = {
        "action": "BUY_YES", "size_usd": 10.0, "strategy": "lag_arb",
        "decision_id": "d1", "p_base": 0.55, "p_real": 0.62, "p_market": 0.41,
    }
    decision.update(overrides)
    return decision


class Engine:
    def __init__(self):
        self.spec = SimpleNamespace(symbol="BTC", series_ticker="KXBTC15M")
        self.signal = SimpleNamespace(prices=[], trades=[], is_ready=lambda: False)
        self.synthetic_spot = SimpleNamespace(spot_mid=None, source_count=0, staleness=0.0)
        self._ticker = "KX-T"
        self._window_id = WINDOW
        self._window_start = float(WINDOW)
        self._open_pos = None
        self._price_to_beat = 100.0
        self._price_to_beat_source = "floor_strike"
        self._last_price_val = None
        self._last_price_ts = 0.0
        self._kalshi_prob_history = []
        self._total_ticks = 0
        self._market = None
        self._close_time_utc = None
        self._last_decision = None
        self._window_fills = []
        self.lag_tracker = SimpleNamespace(update=lambda *args: None)
        self.sim = private_sim(10_000.0)
        self._visible_window = WINDOW

    def _get_window_id(self):
        return self._visible_window


class Market:
    def __init__(self, book):
        self.book = book

    def fetch_execution_book(self, ticker):
        return self.book


def _book(action):
    if action == "BUY_YES":
        return {"yes": [[0.39, 100]], "no": [[0.60, 100]]}
    return {"yes": [[0.40, 100]], "no": [[0.39, 100]]}


def _kinds(session):
    path = session.directory / "lifecycle.jsonl"
    return [strict_loads(line)["kind"] for line in path.read_text(encoding="utf-8").splitlines() if line]


def _events(session, name):
    path = session.directory / f"{name}.jsonl"
    if not path.exists():
        return []
    return [strict_loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _close(session):
    positions = session.journal.state["positions"]
    closed = [item for item in positions.values() if item["status"] == "CLOSED"]
    return closed[0]["close"]


class RuleTests(unittest.TestCase):
    def test_equality_settles_yes_at_zero(self):
        self.assertEqual(yes_settled(100.0, 100.0), 0)
        self.assertEqual(yes_settled(100.01, 100.0), 1)
        self.assertEqual(yes_settled(99.0, 100.0), 0)

    def test_bought_side_orientation_and_exact_gross(self):
        self.assertEqual(side_payoff("yes", 1), 1)
        self.assertEqual(side_payoff("yes", 0), 0)
        self.assertEqual(side_payoff("no", 1), 0)
        self.assertEqual(side_payoff("no", 0), 1)
        self.assertEqual(gross_pnl(25, 0.40, 1), 15.0)
        self.assertEqual(gross_pnl(25, 0.40, 0), -10.0)
        payload = research_close_payload({
            "position_id": "p",
            "quantity": 25,
            "entry_price": 0.40,
            "price_to_beat": 100.0,
            "request": {"side": "yes", "window_id_ts": WINDOW},
        }, observed_spot=110.0, observation_unix=CLOSE)
        self.assertEqual(payload["settlement_method"], SETTLEMENT_METHOD)
        self.assertIsNone(payload["fees"])
        self.assertIsNone(payload["net_pnl"])
        self.assertEqual(payload["gross_pnl"], 15.0)
        self.assertNotIn("mae", payload)
        self.assertNotIn("mfe", payload)


class SettlementLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "shadow_data"
        self.root.mkdir()
        self.session = ShadowSession(self.root, "s1", "era1", SHA, config_identity={"runner": "test"})
        record_starting_balance(self.session, 1000.0)
        self.addCleanup(self.session.close)

    def _open(self, action="BUY_YES", *, strike=100.0, window=WINDOW, decision_id="d1"):
        engine = Engine()
        engine._window_id = window
        engine._window_start = float(window)
        engine._visible_window = window
        engine._price_to_beat = strike
        pipeline = ShadowEntry(self.session, Market(_book(action)))
        pipeline.submit(engine, _decision(action=action, decision_id=decision_id), 0.40)
        self.assertIsNotNone(engine._open_pos)
        self.assertEqual(engine.sim.balance, 10_000.0)
        return engine, pipeline

    def _settle(self, engine, pipeline, spot, now=None):
        if now is None:
            now = engine._window_id + cfg.WINDOW_SECS
        engine.synthetic_spot.spot_mid = spot
        pipeline.settle_if_due(engine, now=now)
        return engine

    def test_buy_yes_win_and_loss(self):
        engine, pipeline = self._open("BUY_YES")
        self._settle(engine, pipeline, 110.0)
        close = _close(self.session)
        self.assertEqual((close["yes_settled"], close["side"], close["side_payoff"]), (1, "yes", 1))
        self.assertEqual(close["gross_pnl"], 15.0)
        self.assertEqual(close["gross_pnl"], close["quantity"] * (close["side_payoff"] - close["entry_price"]))

        engine, pipeline = self._open("BUY_YES", window=WINDOW + 900, decision_id="d-yes-loss")
        self._settle(engine, pipeline, 90.0)
        close = _close(self.session)
        losing = [item["close"] for item in self.session.journal.state["positions"].values()]
        self.assertEqual(losing[-1]["yes_settled"], 0)
        self.assertEqual(losing[-1]["side_payoff"], 0)
        self.assertEqual(losing[-1]["gross_pnl"], -10.0)

    def test_buy_no_win_and_loss(self):
        engine, pipeline = self._open("BUY_NO")
        self._settle(engine, pipeline, 90.0)
        close = _close(self.session)
        self.assertEqual((close["yes_settled"], close["side"], close["side_payoff"]), (0, "no", 1))
        self.assertEqual(close["gross_pnl"], close["quantity"] * (1 - close["entry_price"]))

        engine, pipeline = self._open("BUY_NO", window=WINDOW + 900, decision_id="d-no-loss")
        self._settle(engine, pipeline, 110.0)
        closes = [item["close"] for item in self.session.journal.state["positions"].values()]
        loss = closes[-1]
        self.assertEqual((loss["yes_settled"], loss["side_payoff"]), (1, 0))
        self.assertEqual(loss["gross_pnl"], loss["quantity"] * (0 - loss["entry_price"]))
        self.assertLess(loss["gross_pnl"], 0)

    def test_no_close_before_boundary_and_first_spot_wins(self):
        engine, pipeline = self._open()
        self._settle(engine, pipeline, 110.0, now=CLOSE - 0.001)
        self.assertNotIn("CLOSED", _kinds(self.session))
        self.assertIsNotNone(engine._open_pos)
        self._settle(engine, pipeline, 110.0, now=CLOSE)
        self._settle(engine, pipeline, 50.0, now=CLOSE + 30)
        self._settle(engine, pipeline, 200.0, now=CLOSE + 60)
        closes = [kind for kind in _kinds(self.session) if kind == "CLOSED"]
        self.assertEqual(closes, ["CLOSED"])
        close = _close(self.session)
        self.assertEqual(close["observed_spot"], 110.0)
        self.assertEqual(close["observation_unix"], float(CLOSE))
        self.assertEqual(close["price_to_beat"], 100.0)
        self.assertEqual(close["settlement_method"], SETTLEMENT_METHOD)
        self.assertIsNone(close["fees"])
        self.assertIsNone(close["net_pnl"])
        self.assertEqual(engine.sim.balance, 10_000.0)

    def test_direct_retry_is_idempotent_and_conflict_appends_nothing(self):
        engine, pipeline = self._open()
        self._settle(engine, pipeline, 110.0)
        position_id = _close(self.session)["position_id"]
        before = (self.session.directory / "lifecycle.jsonl").read_bytes()
        again = self.session.journal.settle_research(
            position_id, observed_spot=110.0, observation_unix=CLOSE,
        )
        self.assertEqual(again["observed_spot"], 110.0)
        self.assertEqual(before, (self.session.directory / "lifecycle.jsonl").read_bytes())
        with self.assertRaises(JournalError):
            self.session.journal.settle_research(
                position_id, observed_spot=90.0, observation_unix=CLOSE + 1,
            )
        self.assertEqual(before, (self.session.directory / "lifecycle.jsonl").read_bytes())

    def test_restart_open_closes_once_and_restart_after_close_does_not(self):
        engine, pipeline = self._open()
        position_id = engine._open_pos.order_id
        self.session.close()
        recovered = ShadowSession.recover(self.root, "s1")
        self.addCleanup(recovered.close)
        pipeline = ShadowEntry(recovered, Market(_book("BUY_YES")))
        restored = Engine()
        restored._price_to_beat = 5.0
        pipeline.restore_open_positions({"BTC": restored})
        self.assertEqual(restored._open_pos.price_to_beat, 100.0)
        self.assertEqual(restored._open_pos.order_id, position_id)
        self._settle(restored, pipeline, 110.0)
        self.assertIsNone(restored._open_pos)
        self.assertEqual([kind for kind in _kinds(recovered) if kind == "CLOSED"], ["CLOSED"])
        recovered.close()

        again = ShadowSession.recover(self.root, "s1")
        self.addCleanup(again.close)
        pipeline = ShadowEntry(again, Market(_book("BUY_YES")))
        later = Engine()
        pipeline.restore_open_positions({"BTC": later})
        self.assertIsNone(later._open_pos)
        before = (again.directory / "lifecycle.jsonl").read_bytes()
        self._settle(later, pipeline, 50.0, now=CLOSE + 15)
        self.assertEqual(before, (again.directory / "lifecycle.jsonl").read_bytes())
        self.assertEqual(again.journal.state["positions"][position_id]["close"]["observed_spot"], 110.0)

    def test_window_roll_keeps_open_until_its_own_strike_resolves(self):
        engine, pipeline = self._open()
        before = (self.session.directory / "lifecycle.jsonl").read_bytes()
        engine.synthetic_spot.spot_mid = None
        pipeline.settle_if_due(engine, now=CLOSE)
        pipeline.settle_if_due(engine, now=CLOSE + 1)
        self.assertIsNotNone(engine._open_pos)
        self.assertEqual(
            [row["event"] for row in _events(self.session, "operational_events") if row["event"] == "settlement_waiting"],
            ["settlement_waiting"],
        )
        engine._visible_window = CLOSE
        self.assertEqual(pipeline.refresh_window(engine), "KXBTC15M")
        self.assertEqual(engine._window_id, CLOSE)
        self.assertIsNotNone(engine._open_pos)
        self.assertTrue((self.session.directory / "lifecycle.jsonl").read_bytes().startswith(before))
        engine._price_to_beat = 50.0
        self._settle(engine, pipeline, 110.0)
        close = _close(self.session)
        self.assertEqual(close["price_to_beat"], 100.0)
        self.assertEqual(close["yes_settled"], 1)
        self.assertIsNone(engine._open_pos)

        second = pipeline.submit(engine, _decision(decision_id="d2"), 0.40)
        self.assertEqual(second["action"], "BUY_YES")
        self.assertIsNotNone(engine._open_pos)
        self.assertEqual(engine._open_pos.window_id, CLOSE)
        statuses = sorted(item["status"] for item in self.session.journal.state["positions"].values())
        self.assertEqual(statuses, ["CLOSED", "OPEN"])

    def test_missing_strike_is_an_error_and_does_not_drop_the_position(self):
        engine, pipeline = self._open(strike=None)
        self.assertIsNone(engine._open_pos.price_to_beat)
        self._settle(engine, pipeline, 110.0)
        self._settle(engine, pipeline, 110.0, now=CLOSE + 5)
        self.assertIsNotNone(engine._open_pos)
        self.assertNotIn("CLOSED", _kinds(self.session))
        errors = [row for row in _events(self.session, "operational_events") if row["event"] == "settlement_error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(engine.sim.balance, 10_000.0)

    def test_tampered_resolution_fails_recovery(self):
        engine, pipeline = self._open()
        self._settle(engine, pipeline, 110.0)
        path = self.session.directory / "lifecycle.jsonl"
        self.session.close()
        records = [strict_loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        records[-1]["payload"]["yes_settled"] = 0
        records[-1].pop("sha256")
        records[-1]["sha256"] = hashlib.sha256(canonical(records[-1]).encode()).hexdigest()
        path.write_text("".join(canonical(row) + "\n" for row in records), encoding="utf-8")
        with self.assertRaises(JournalError) as caught:
            ShadowSession.recover(self.root, "s1")
        self.assertIn("does not match", str(caught.exception))

    def test_open_record_is_not_rewritten_and_loop_settles_before_roll(self):
        engine, pipeline = self._open()
        before = (self.session.directory / "lifecycle.jsonl").read_bytes()
        self._settle(engine, pipeline, 110.0)
        after = (self.session.directory / "lifecycle.jsonl").read_bytes()
        self.assertTrue(after.startswith(before))
        source = inspect.getsource(run_shadow._price_loop)
        self.assertLess(source.index("settle_if_due"), source.index("refresh_window"))
        runner = Path(run_shadow.__file__).read_text(encoding="utf-8")
        market = Path(__file__).resolve().parent.joinpath("shadow_market.py").read_text(encoding="utf-8")
        settlement = Path(__file__).resolve().parent.joinpath("shadow_settlement.py").read_text(encoding="utf-8")
        for text in (runner, market, settlement):
            self.assertNotIn("requests.post", text)
            self.assertNotIn("get_market_result", text)
        self.assertIn('"GET"', market)
