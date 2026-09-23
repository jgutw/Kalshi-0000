"""Offline Shadow entry pipeline. No live sockets and no production artifacts."""
import ast
import base64
import inspect
import subprocess
import sys
import tempfile
import textwrap
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_bot.asset_engine import AssetEngine
from kalshi_bot.config import ASSETS
from kalshi_bot.shadow import ShadowSession
from kalshi_bot.shadow_entry import (
    ShadowEntry, entry_from_decision, load_starting_balance, private_sim,
    production_entry, record_starting_balance,
)
from kalshi_bot.shadow_execution import FULL, NOT_MARKETABLE
from kalshi_bot.shadow_feeds import public_feed_coros
from kalshi_bot.signal_engine import AssetSignalEngine
from kalshi_bot.shadow_journal import strict_loads
from kalshi_bot.shadow_market import BookIdentityError, BookReadError, ReadOnlyKalshi, sign_get
from kalshi_bot.sim_state import SimState
import run_shadow

SHA = "0123456789abcdef0123456789abcdef01234567"
ROOT = Path(__file__).resolve().parent.parent


def _forbid(*args, **kwargs):
    raise AssertionError("production state write")


def _decision(**overrides):
    decision = {
        "action": "BUY_YES", "size_usd": 10.0, "strategy": "lag_arb",
        "decision_id": "d1", "p_base": 0.55, "p_real": 0.62, "p_market": 0.41,
    }
    decision.update(overrides)
    return decision


class Stub:
    def __init__(self):
        self.spec = SimpleNamespace(symbol="BTC", series_ticker="KXBTC15M")
        self.signal = SimpleNamespace(prices=[])
        self.synthetic_spot = SimpleNamespace(spot_mid=None)
        self._ticker = "KX-T"
        self._window_id = 1_700_000_000
        self._window_start = float(self._window_id)
        self._open_pos = None
        self._price_to_beat = 100.0
        self._last_price_val = None
        self._last_price_ts = 0.0
        self._kalshi_prob_history = deque()
        self._total_ticks = 0
        self._market = None
        self._close_time_utc = None
        self._price_to_beat_source = ""
        self._last_decision = None
        self._window_fills = []
        self.lag_tracker = SimpleNamespace(update=lambda *args: None, lag_signal=0, lag_confidence=0)
        self.next_decision = _decision(action="WAIT", reason="test", size_usd=0)
        self.sim = SimpleNamespace(save=_forbid)

    def _wait(self, reason, yes):
        return {"action": "WAIT", "reason": reason, "size_usd": 0, "p_market": yes}

    def make_decision(self, yes):
        return dict(self.next_decision)


class Market:
    def __init__(self, session, book=None, fail=None):
        self.session = session
        self.book = book if book is not None else {"yes": [[0.39, 25]], "no": [[0.60, 25]]}
        self.fail = fail
        self.calls = []

    def fetch_execution_book(self, ticker):
        self.calls.append((ticker, set(self.session.journal.state["orders"])))
        if self.fail:
            raise self.fail
        return self.book


def _session(root, sid="s1"):
    session = ShadowSession(root, sid, "era1", SHA, config_identity={"runner": "test"})
    record_starting_balance(session, 1000.0)
    return session


def _kinds(session):
    path = session.directory / "lifecycle.jsonl"
    if not path.exists() or path.stat().st_size == 0:
        return []
    return [strict_loads(line)["kind"] for line in path.read_text(encoding="utf-8").splitlines() if line]


class OrderConstructionTests(unittest.TestCase):
    def test_buy_yes_and_buy_no_match_production_formula(self):
        yes, reason = entry_from_decision(
            _decision(), 0.40, p_base_min=0.02, min_entry=0.02, max_entry=0.98, spot_confidence_min=0.30,
        )
        no, _ = entry_from_decision(
            _decision(action="BUY_NO"), 0.40, p_base_min=0.02, min_entry=0.02, max_entry=0.98,
            spot_confidence_min=0.30,
        )
        self.assertIsNone(reason)
        self.assertEqual((yes.side, yes.price, yes.count), ("yes", 0.40, max(1, int(10.0 / 0.40))))
        self.assertEqual((no.side, no.price, no.count), ("no", 1.0 - 0.40, max(1, int(10.0 / (1.0 - 0.40)))))

    def test_small_size_still_requests_one_contract(self):
        entry, reason = production_entry(_decision(size_usd=0.5), 0.40)
        self.assertIsNone(reason)
        self.assertEqual(entry.count, 1)

    def test_production_gates_reject_before_an_order_exists(self):
        low, low_reason = production_entry(_decision(), 0.01)
        band, band_reason = production_entry(_decision(p_real=0.50), 0.40)
        weak, weak_reason = production_entry(_decision(spot_confidence=0.10), 0.40)
        self.assertIsNone(low)
        self.assertEqual(low_reason, "below_p_base_min")
        self.assertEqual(band_reason, "p_real_band")
        self.assertEqual(weak_reason, "spot_confidence")
        self.assertIsNone(band)
        self.assertIsNone(weak)

    def test_divisor_floor_matches_executable_production_expression(self):
        # de24128 divides by max(entry, 0.01). The 0.02 gate rejects anything below that,
        # so a submitted order uses entry itself. The expression is still the production one.
        entry, reason = entry_from_decision(
            _decision(size_usd=10.0), 0.02, p_base_min=0.02, min_entry=0.02, max_entry=0.98,
            spot_confidence_min=0.30,
        )
        self.assertIsNone(reason)
        self.assertEqual(entry.count, max(1, int(10.0 / max(0.02, 0.01))))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "shadow_data"
        self.root.mkdir()
        self.session = _session(self.root)
        self.market = Market(self.session)
        self.pipeline = ShadowEntry(self.session, self.market)
        self.engine = Stub()

    def tearDown(self):
        self.session.close()
        self.tmp.cleanup()

    def test_wait_records_decision_and_does_not_intend_or_fetch(self):
        decision = self.pipeline.submit(self.engine, _decision(action="WAIT", reason="signal_warmup", size_usd=0), 0.40)
        self.assertEqual(decision["action"], "WAIT")
        self.assertEqual(self.market.calls, [])
        self.assertEqual(_kinds(self.session), [])
        self.assertIsNone(self.engine._open_pos)
        rows = (self.session.directory / "decisions.jsonl").read_text(encoding="utf-8")
        self.assertIn("signal_warmup", rows)
        self.assertIn(SHA, rows)

    def test_full_fill_intends_before_one_book_then_opens(self):
        self.pipeline.submit(self.engine, _decision(), 0.40)
        self.assertEqual(len(self.market.calls), 1)
        ticker, orders_at_fetch = self.market.calls[0]
        self.assertEqual(ticker, "KX-T")
        self.assertTrue(orders_at_fetch)
        self.assertEqual(_kinds(self.session), ["INTENDED", "SIMULATED_EXECUTION"])
        state = self.session.journal.state
        order = next(iter(state["orders"].values()))
        self.assertEqual(order["status"], "FILLED")
        self.assertEqual(order["request"]["count"], 25)
        self.assertEqual(order["request"]["limit_price"], 0.40)
        position = next(iter(state["positions"].values()))
        self.assertEqual(position["status"], "OPEN")
        self.assertEqual(position["request"]["side"], "yes")
        result = order["execution"]["result"]
        self.assertEqual(result["disposition"], FULL)
        self.assertIsNotNone(self.engine._open_pos)

    def test_no_fill_does_not_open(self):
        self.market.book = {"yes": [[0.39, 25]], "no": [[0.50, 25]]}
        self.pipeline.submit(self.engine, _decision(), 0.40)
        self.assertEqual(len(self.market.calls), 1)
        order = next(iter(self.session.journal.state["orders"].values()))
        self.assertEqual(order["status"], "NOT_FILLED")
        self.assertEqual(order["execution"]["result"]["disposition"], NOT_MARKETABLE)
        self.assertEqual(self.session.journal.state["positions"], {})
        self.assertIsNone(self.engine._open_pos)

    def test_book_failure_keeps_intended_and_does_not_retry(self):
        self.market.fail = BookReadError("down")
        self.pipeline.submit(self.engine, _decision(), 0.40)
        self.pipeline.submit(self.engine, _decision(), 0.40)
        self.assertEqual(len(self.market.calls), 1)
        self.assertEqual(_kinds(self.session), ["INTENDED"])
        order = next(iter(self.session.journal.state["orders"].values()))
        self.assertEqual(order["status"], "INTENDED")
        self.assertIsNone(order["execution"])
        text = (self.session.directory / "operational_events.jsonl").read_text(encoding="utf-8")
        self.assertIn("execution_book_failed", text)
        self.assertIn("intention_already_consumed", text)
        self.assertIsNone(self.engine._open_pos)

    def test_identity_mismatch_does_not_simulate(self):
        self.market.fail = BookIdentityError("other ticker")
        self.pipeline.submit(self.engine, _decision(), 0.40)
        self.assertEqual(_kinds(self.session), ["INTENDED"])
        self.assertIn("execution_book_identity_mismatch",
                      (self.session.directory / "operational_events.jsonl").read_text(encoding="utf-8"))

    def test_on_yes_mid_uses_make_decision_and_not_execute(self):
        flags = []
        self.engine._execute = lambda *a, **k: flags.append("execute")
        self.engine.on_price_update = lambda *a, **k: flags.append("price")
        self.engine.next_decision = _decision(action="WAIT", reason="from-decision", size_usd=0)
        decision = self.pipeline.on_yes_mid(self.engine, 0.40)
        self.assertEqual(flags, [])
        self.assertEqual(decision["reason"], "from-decision")
        self.assertEqual(self.market.calls, [])


class RecoveryTests(unittest.TestCase):
    def test_recovered_bare_intended_is_not_simulated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shadow_data"
            root.mkdir()
            session = _session(root, "recover-intended")
            entry, _ = production_entry(_decision(), 0.40)
            session.journal.intend(
                asset="BTC", window_id_ts=1_700_000_000, decision_id="d1", strategy="lag_arb",
                ticker="KX-T", side=entry.side, count=entry.count, limit_price=entry.price,
            )
            session.close()
            session = ShadowSession.recover(root, "recover-intended")
            market = Market(session)
            pipeline = ShadowEntry(session, market)
            pipeline.submit(Stub(), _decision(), 0.40)
            self.assertEqual(market.calls, [])
            self.assertEqual(_kinds(session), ["INTENDED"])
            self.assertIn("intention_already_consumed",
                          (session.directory / "operational_events.jsonl").read_text(encoding="utf-8"))
            session.close()

    def test_recovered_not_filled_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shadow_data"
            root.mkdir()
            session = _session(root, "recover-nofill")
            market = Market(session, book={"yes": [[0.39, 25]], "no": [[0.50, 25]]})
            ShadowEntry(session, market).submit(Stub(), _decision(), 0.40)
            session.close()
            session = ShadowSession.recover(root, "recover-nofill")
            market = Market(session, book={"yes": [[0.39, 25]], "no": [[0.60, 25]]})
            ShadowEntry(session, market).submit(Stub(), _decision(), 0.40)
            self.assertEqual(market.calls, [])
            self.assertEqual(session.journal.state["positions"], {})
            session.close()

    def test_recovered_open_restores_and_blocks_reentry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shadow_data"
            root.mkdir()
            session = _session(root, "recover-open")
            market = Market(session)
            ShadowEntry(session, market).submit(Stub(), _decision(), 0.40)
            session.close()
            session = ShadowSession.recover(root, "recover-open")
            engine = Stub()
            pipeline = ShadowEntry(session, Market(session, book={"yes": [[0.39, 100]], "no": [[0.60, 100]]}))
            pipeline.restore_open_positions({"BTC": engine})
            self.assertIsNotNone(engine._open_pos)
            pipeline.submit(engine, _decision(decision_id="d2"), 0.40)
            self.assertEqual(pipeline.market.calls, [])
            self.assertEqual(len(session.journal.state["orders"]), 1)
            self.assertEqual(len(session.journal.state["positions"]), 1)
            session.close()

    def test_recovery_rejects_a_different_starting_balance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shadow_data"
            root.mkdir()
            session = _session(root, "balance")
            session.close()
            session = ShadowSession.recover(root, "balance")
            self.assertEqual(load_starting_balance(session), 1000.0)
            session.close()


class EngineReuseTests(unittest.TestCase):
    def test_real_engine_decision_does_not_execute_or_write_production_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shadow_data"
            root.mkdir()
            session = _session(root, "engine")
            market = Market(session)
            spec = next(spec for spec in ASSETS if spec.symbol == "BTC")
            sim = private_sim(1000.0)
            sim.save = _forbid
            engine = AssetEngine(spec, market, sim, entries_paused_provider=lambda: False)
            engine._execute = _forbid
            engine._log_decision = _forbid
            engine._append_window_summary = _forbid
            engine._maybe_early_exit = _forbid
            engine.on_price_update = _forbid
            engine.on_window_advance = _forbid
            engine._ticker = "KX-T"
            engine._window_id = 1_700_000_000
            engine._window_start = 1_700_000_000.0
            decision = ShadowEntry(session, market).on_yes_mid(engine, 0.40)
            self.assertEqual(decision["action"], "WAIT")
            self.assertEqual(decision["reason"], "signal_warmup")
            self.assertEqual(market.calls, [])
            self.assertFalse(any(part.casefold() in {"logs", "sessions", "research_data"}
                                 for part in session.directory.parts))
            session.close()


class ReadOnlyClientTests(unittest.TestCase):
    def test_surface_has_no_write_methods(self):
        names = set(dir(ReadOnlyKalshi))
        for banned in ("_post", "post", "place_market_order", "cancel_order", "amend_order",
                       "batch_order", "intra_transfer_shards", "request"):
            self.assertNotIn(banned, names)
        self.assertNotIn("method", inspect.signature(ReadOnlyKalshi._get).parameters)
        self.assertNotIn("method", inspect.signature(sign_get).parameters)

    def test_get_semantics_and_get_only_signature(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        seen = {}

        def transport(url, *, headers, params, timeout):
            seen["url"] = url
            seen["headers"] = headers
            seen["params"] = params
            if url.endswith("/markets") and params.get("status") == "open":
                return 200, {"markets": [
                    {"ticker": "LATER", "status": "open", "close_time": "2099-01-01T00:30:00Z"},
                    {"ticker": "SOON", "status": "open", "close_time": "2099-01-01T00:15:00Z"},
                ]}
            if "/orderbook" in url:
                if params.get("depth") != 100:
                    return 500, None
                return 200, {"ticker": url.split("/markets/")[1].split("/")[0], "orderbook_fp": {
                    "yes_dollars": [["0.40", "3"]],
                    "no_dollars": [["0.55", "4"]],
                }}
            if url.endswith("/KX-T"):
                return 200, {"market": {"yes_bid_dollars": "0.40", "yes_ask_dollars": "0.50", "ticker": "KX-T"}}
            return 500, None

        client = ReadOnlyKalshi("https://example.test/trade-api/v2", "key", pem, transport)
        self.assertEqual(client.find_active_market("KXBTC15M")["ticker"], "SOON")
        self.assertEqual(client.get_yes_mid("KX-T"), 0.45)
        book = client.fetch_execution_book("KX-T")
        self.assertEqual(book["yes"], [[0.40, 3.0]])
        self.assertEqual(book["no"], [[0.55, 4.0]])
        signed = seen["headers"]
        path = urlparse(seen["url"]).path
        message = (signed["KALSHI-ACCESS-TIMESTAMP"] + "GET" + path).encode()
        key.public_key().verify(
            base64.b64decode(signed["KALSHI-ACCESS-SIGNATURE"]), message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        with self.assertRaises(Exception):
            key.public_key().verify(
                base64.b64decode(signed["KALSHI-ACCESS-SIGNATURE"]),
                (signed["KALSHI-ACCESS-TIMESTAMP"] + "POST" + path).encode(),
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )

    def test_failed_and_mismatched_books_fail_closed(self):
        def transport(url, *, headers, params, timeout):
            if "bad" in url:
                return 200, ["not-a-book"]
            if "other" in url:
                return 200, {"ticker": "SOMEONE-ELSE", "orderbook_fp": {"yes_dollars": [], "no_dollars": []}}
            return 503, None

        client = ReadOnlyKalshi("https://example.test/trade-api/v2", "key", "", transport)
        with self.assertRaises(BookReadError):
            client.fetch_execution_book("KX-T")
        with self.assertRaises(BookReadError):
            client.fetch_execution_book("bad")
        with self.assertRaises(BookIdentityError):
            client.fetch_execution_book("other")


class BoundaryTests(unittest.TestCase):
    def test_import_has_no_capital_modules(self):
        body = textwrap.dedent("""
            import run_shadow
            import kalshi_bot.shadow_market
            import kalshi_bot.shadow_entry
            import kalshi_bot.shadow_feeds
            forbidden = (
                "kalshi_bot.kalshi_client",
                "kalshi_bot.api_config",
                "kalshi_bot.kalshi_bot",
                "kalshi_bot.safe_live",
                "kalshi_bot.telegram",
                "kalshi_bot.telegram.handlers",
            )
            import sys
            for name in forbidden:
                assert name not in sys.modules, name
        """)
        env = {k: v for k, v in __import__("os").environ.items() if not k.startswith("KALSHI_")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = str(ROOT)
        result = subprocess.run(
            [sys.executable, "-B", "-c", body], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_runner_source_does_not_construct_the_production_bot(self):
        text = (ROOT / "run_shadow.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        names = [node.id for node in ast.walk(tree) if isinstance(node, ast.Name)]
        self.assertNotIn("KalshiClient", names)
        self.assertNotIn("KalshiMultiBot", names)
        self.assertNotIn("_execute", text)
        self.assertNotIn("SimState.load", text)

    def test_cli_rejects_live_fresh_round_and_p6_tag(self):
        base = ["--session-id", "s", "--session-tag", "era1", "--new", "--code-sha", SHA, "--starting-balance", "10"]
        for extra in (["--live"], ["--fresh-round"]):
            with self.assertRaises(SystemExit):
                run_shadow.parse_args(base + extra)
        with self.assertRaises(SystemExit):
            run_shadow.parse_args([
                "--session-id", "s", "--session-tag", "p6c_d1_validation", "--new",
                "--code-sha", SHA, "--starting-balance", "10",
            ])
        with self.assertRaises(SystemExit):
            run_shadow.parse_args(["--session-id", "s", "--session-tag", "era1"])

    def test_feed_plan_preserves_production_venue_coverage(self):
        engine = Stub()
        engine.signal = SimpleNamespace(
            update_trade_coinbase=lambda *a: None,
            update_book_coinbase=lambda *a: None,
            update_trade_binance=lambda *a: None,
            update_book_binance=lambda *a: None,
            update_trade_okx=lambda *a: None,
            update_book_okx=lambda *a: None,
        )
        engine.synthetic_spot = SimpleNamespace(update=lambda *a: None, spot_mid=None)
        engine.spec.symbol = "NEAR"
        near = public_feed_coros({"NEAR": engine})
        engine.spec.symbol = "BTC"
        btc = public_feed_coros({"BTC": engine})
        try:
            near_names = [name for name, _ in near]
            btc_names = [name for name, _ in btc]
        finally:
            for _, coro in near + btc:
                coro.close()
        self.assertEqual(near_names, ["NEAR:coinbase", "NEAR:binance", "NEAR:okx", "NEAR:kraken"])
        self.assertEqual(btc_names, [
            "BTC:coinbase", "BTC:binance", "BTC:okx", "BTC:kraken", "BTC:gemini",
        ])

    def test_coinbase_book_does_not_call_missing_method_and_trades_reach_ready(self):
        from kalshi_bot import shadow_feeds
        signal = AssetSignalEngine("btc")
        spots = []
        engine = SimpleNamespace(
            signal=signal,
            synthetic_spot=SimpleNamespace(update=lambda source, mid: spots.append((source, mid))),
        )

        def reject_missing(*_args, **_kwargs):
            raise AssertionError("update_book_coinbase")

        signal.update_book_coinbase = reject_missing
        captured = {}

        def spy(symbol, product_id, on_trade=None, on_book=None, on_mid=None):
            captured["on_trade"] = on_trade
            captured["on_book"] = on_book
            async def idle():
                return None
            return idle()

        original = shadow_feeds.run_coinbase_microstructure
        shadow_feeds.run_coinbase_microstructure = spy
        try:
            pairs = public_feed_coros({"BTC": engine})
        finally:
            shadow_feeds.run_coinbase_microstructure = original
        try:
            bids = [[100.0, 1.5], [99.0, 0.0]]
            asks = [[101.0, 2.0], [102.0, 0.25]]
            captured["on_book"](bids, asks)
            captured["on_book"](bids, asks)
            self.assertEqual(signal.book_bids_cb, {100.0: 1.5})
            self.assertEqual(signal.book_asks_cb, {101.0: 2.0, 102.0: 0.25})
            self.assertEqual(spots, [("coinbase", 100.5), ("coinbase", 100.5)])
            self.assertFalse(signal.is_ready())
            seen = []
            original_trade = signal.update_trade_coinbase

            def wrapped(price, qty, side):
                seen.append((price, qty, side))
                return original_trade(price, qty, side)

            signal.update_trade_coinbase = wrapped
            for i in range(20):
                captured["on_trade"](50000.0 + i, 0.01, "BUY")
            self.assertEqual(len(seen), 20)
            self.assertGreaterEqual(len(signal.prices), 20)
            self.assertGreaterEqual(len(signal.trades), 5)
            self.assertTrue(signal.is_ready())
        finally:
            for _, coro in pairs:
                coro.close()

    def test_private_sim_does_not_load_production_state(self):
        original = SimState.load
        SimState.load = lambda *a, **k: (_ for _ in ()).throw(AssertionError("load"))
        try:
            sim = private_sim(250.0)
        finally:
            SimState.load = original
        self.assertEqual(sim.balance, 250.0)
        self.assertEqual(sim.starting_balance, 250.0)


if __name__ == "__main__":
    unittest.main()
