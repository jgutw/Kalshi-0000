"""Offline specifications for top-of-book counterfactual execution and durable replay."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .shadow import ShadowClient, ShadowSession
from .shadow_execution import (simulate, observation, restore_observation, MODEL_ID,
                               FULL, NOT_MARKETABLE, NO_LIQUIDITY, INVALID_BOOK)
from .shadow_journal import JournalError, canonical


class ModelTests(unittest.TestCase):
    def order(self, side="yes", limit=.52, count=2):
        return dict(intended_order_id="intent-1", timestamp_utc="2026-09-23T00:00:00+00:00",
                    request=dict(side=side, count=count, limit_price=limit))

    def book(self):
        return {"yes": [[.46, 10]], "no": [[.52, 10]]}

    def assert_no_fill(self, result, disposition):
        self.assertEqual(result["disposition"], disposition)
        self.assertEqual(result["filled_quantity"], 0)
        self.assertIsNone(result["simulated_price"])
        self.assertIsNone(result["simulated_price_minus_side_ask"])
        self.assertIsNone(result["simulated_price_minus_requested_limit"])

    def test_marketable_yes(self):
        result = simulate(self.order(), self.book())
        self.assertEqual((result["disposition"], result["filled_quantity"], result["simulated_price"]), (FULL, 2, .48))

    def test_marketable_no(self):
        result = simulate(self.order(side="no", limit=.6), self.book())
        self.assertEqual((result["disposition"], result["filled_quantity"], result["simulated_price"]), (FULL, 2, .54))

    def test_exact_touch_yes_decimal(self):
        result = simulate(self.order(limit=.58), {"yes": [], "no": [[.42, 3]]})
        self.assertEqual((result["disposition"], result["simulated_price"]), (FULL, .58))

    def test_exact_touch_no_decimal(self):
        result = simulate(self.order(side="no", limit=.58), {"yes": [[.42, 3]], "no": []})
        self.assertEqual((result["disposition"], result["simulated_price"]), (FULL, .58))

    def test_nonmarketable_and_no_limit(self):
        for limit in (.47, .4799999999999999, None):
            with self.subTest(limit=limit):
                self.assert_no_fill(simulate(self.order(limit=limit), self.book()), NOT_MARKETABLE)

    def test_missing_opposite_bid(self):
        for book in ({}, {"yes": [[.46, 10]]}, {"no": []}, {"no": None}):
            with self.subTest(book=book):
                self.assert_no_fill(simulate(self.order(), book), NO_LIQUIDITY)

    def test_malformed_book(self):
        for book in (None, [], "bad", {"no": "bad"}, {"no": [None]}, {"no": [[]]}, {"no": [[.52, 10, 99]]}):
            with self.subTest(book=book):
                self.assert_no_fill(simulate(self.order(), book), INVALID_BOOK)

    def test_nonfinite_prices(self):
        for price in (float("inf"), -float("inf"), float("nan")):
            result = simulate(self.order(), {"no": [[price, 10]]})
            self.assert_no_fill(result, INVALID_BOOK)
            json.dumps(result, allow_nan=False)
            self.assertEqual(len(result["observation"]["nonfinite"]), 1)

    def test_invalid_complement_prices(self):
        for price in (0, 1, -1, 2, True, "0.52", None, 5e-324):
            with self.subTest(price=price):
                self.assert_no_fill(simulate(self.order(), {"no": [[price, 10]]}), INVALID_BOOK)

    def test_invalid_sizes(self):
        for size in (0, -1, 2.5, None, "10", True, float("inf"), float("nan")):
            with self.subTest(size=size):
                self.assert_no_fill(simulate(self.order(), {"no": [[.52, size]]}), NO_LIQUIDITY)

    def test_missing_size(self):
        self.assert_no_fill(simulate(self.order(), {"no": [[.52]]}), NO_LIQUIDITY)

    def test_quantity_below_equal_above_top(self):
        for count, disposition in ((9, FULL), (10, FULL), (11, NO_LIQUIDITY)):
            result = simulate(self.order(count=count), self.book())
            self.assertEqual(result["disposition"], disposition)
            self.assertEqual(result["filled_quantity"], count if disposition == FULL else 0)

    def test_equal_price_aggregation_only(self):
        book = {"no": [[.5, 100], [.52, 2], [.52, 3]]}
        result = simulate(self.order(count=5), book)
        self.assertEqual((result["disposition"], result["displayed_executable_top_quantity"]), (FULL, 5))
        self.assert_no_fill(simulate(self.order(count=6), book), NO_LIQUIDITY)

    def test_fractional_rows_whole_aggregate(self):
        book = {"no": [[.52, .5], [.52, 1.5]]}
        self.assertEqual(simulate(self.order(), book)["filled_quantity"], 2)

    def test_invalid_best_size_not_offset_by_other_rows(self):
        for size in (0, -1, None):
            book = {"no": [[.52, size], [.52, 100], [.51, 1000]]}
            self.assert_no_fill(simulate(self.order(), book), NO_LIQUIDITY)

    def test_worse_prices_not_walked(self):
        self.assert_no_fill(simulate(self.order(count=20), {"no": [[.52, 12], [.51, 1000]]}), NO_LIQUIDITY)

    def test_worse_size_is_irrelevant_but_invalid_price_rejected(self):
        book = {"no": [[.52, 10], [.51, None]]}
        self.assertEqual(simulate(self.order(), book)["disposition"], FULL)
        book["no"].append([float("nan"), 10])
        self.assert_no_fill(simulate(self.order(), book), INVALID_BOOK)

    def test_price_improvement_not_limit_fill(self):
        result = simulate(self.order(limit=.52), self.book())
        self.assertEqual(result["simulated_price"], .48)
        self.assertEqual(result["simulated_price_minus_requested_limit"], -.04)
        self.assertEqual(result["simulated_price_minus_side_ask"], 0)

    def test_spread_half_spread_from_same_book(self):
        result = simulate(self.order(), self.book())
        self.assertEqual((result["yes_bid"], result["yes_ask"], result["no_bid"], result["no_ask"]), (.46, .48, .52, .54))
        self.assertEqual((result["yes_spread"], result["half_spread"]), (.02, .01))

    def test_missing_spread_stays_null(self):
        result = simulate(self.order(), {"no": [[.52, 10]]})
        self.assertEqual(result["disposition"], FULL)
        self.assertIsNone(result["yes_spread"])
        self.assertIsNone(result["half_spread"])
        self.assertIsNone(result["yes_bid"])

    def test_crossed_and_locked_book(self):
        self.assert_no_fill(simulate(self.order(), {"yes": [[.51, 10]], "no": [[.52, 10]]}), INVALID_BOOK)
        locked = simulate(self.order(), {"yes": [[.48, 10]], "no": [[.52, 10]]})
        self.assertEqual(locked["disposition"], FULL)
        self.assertEqual(locked["yes_spread"], 0)

    def test_mid_and_displayed_market_quotes_not_execution_inputs(self):
        book = dict(self.book(), yes_mid=.01, yes_bid_dollars=.99, yes_ask_dollars=.01)
        self.assertEqual(simulate(self.order(), book)["simulated_price"], .48)
        self.assert_no_fill(simulate(self.order(), {"yes_ask_dollars": .01, "yes_mid": .01}), NO_LIQUIDITY)

    def test_fees_unavailable(self):
        result = simulate(self.order(), self.book())
        self.assertEqual(result["fee_model"], "unavailable")
        self.assertIsNone(result["fee_amount"])
        self.assertNotIn("pnl", result)

    def test_poll_age_is_metadata_only(self):
        for age in (None, 0, 10, 1000000):
            result = simulate(self.order(), self.book(), yes_mid_poll_age_secs=age)
            self.assertEqual((result["disposition"], result["simulated_price"]), (FULL, .48))
            self.assertEqual(result["yes_mid_poll_age_secs"], age)
            self.assertIsNone(result["book_exchange_timestamp"])

    def test_supplied_timestamp_preserved_without_clock_inference(self):
        stamp = "2026-09-23T00:00:00Z"
        result = simulate(self.order(), self.book(), book_exchange_timestamp=stamp)
        self.assertEqual(result["book_exchange_timestamp"], stamp)
        self.assertNotIn("simulation_timestamp_utc", result)

    def test_invalid_optional_metadata_rejected(self):
        for age in (-1, float("nan"), True, "1"):
            with self.assertRaises(ValueError):
                simulate(self.order(), self.book(), yes_mid_poll_age_secs=age)
        with self.assertRaises(ValueError):
            simulate(self.order(), self.book(), book_exchange_timestamp={})

    def test_determinism_and_detached_observation(self):
        book = self.book()
        original = deepcopy(book)
        result = simulate(self.order(), book)
        self.assertEqual(result, simulate(self.order(), {"no": original["no"], "yes": original["yes"]}))
        self.assertEqual(book, original)
        book["no"][0][0] = .99
        self.assertEqual(result["observation"]["book"], original)

    def test_pure_no_io_clock_or_randomness(self):
        with patch("socket.socket", side_effect=AssertionError("network")), \
             patch("builtins.open", side_effect=AssertionError("file")), \
             patch("time.time", side_effect=AssertionError("clock")), \
             patch("uuid.uuid4", side_effect=AssertionError("random")):
            self.assertEqual(simulate(self.order(), self.book())["disposition"], FULL)

    def test_no_arbitrary_objects(self):
        with self.assertRaises(ValueError):
            simulate(self.order(), {"no": object()})


class JournalExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "shadow_data"
        self.session = ShadowSession(self.root, "sim", "test-shadow", "a" * 40)
        self.addCleanup(self.session.close)
        self.client = ShadowClient(self.session)
        self.request = dict(ticker="BTC-TEST", side="yes", count=2, limit_price=.52,
                            asset="BTC", window_id_ts=900, decision_id="d1", strategy="test")
        self.intent = self.client.place_market_order(**self.request)
        self.book = {"yes": [[.46, 10]], "no": [[.52, 10]]}
        self.path = self.session.journal.path

    def restart(self):
        self.session.close()
        self.session = ShadowSession.recover(self.root, "sim")
        self.addCleanup(self.session.close)
        return self.session.journal.state

    def simulate(self, book=None, **metadata):
        return self.session.simulate_execution(self.intent.order_id, self.book if book is None else book, **metadata)

    def rewrite_last(self, mutate, rehash=True):
        self.session.close()
        records = [json.loads(line) for line in self.path.read_text().splitlines()]
        mutate(records[-1])
        if rehash:
            records[-1].pop("sha256")
            records[-1]["sha256"] = hashlib.sha256(canonical(records[-1]).encode()).hexdigest()
        self.path.write_text("".join(canonical(row) + "\n" for row in records), encoding="utf-8")

    def test_full_execution_and_position_survive_restart(self):
        payload = self.simulate()
        state = self.restart()
        order = next(iter(state["orders"].values()))
        self.assertEqual(order["execution"], payload)
        position = state["positions"][payload["position_id"]]
        self.assertEqual((position["quantity"], position["entry_price"], position["status"]), (2, .48, "OPEN"))
        event = json.loads(self.path.read_text().splitlines()[-1])
        self.assertEqual(event["kind"], "SIMULATED_EXECUTION")
        self.assertEqual(payload["result"]["model_id"], MODEL_ID)
        self.assertEqual(position["opened_utc"], event["timestamp_utc"])

    def test_no_fill_survives_without_position(self):
        payload = self.simulate({"no": [[.52, 1]]})
        state = self.restart()
        self.assertEqual(payload["result"]["disposition"], NO_LIQUIDITY)
        self.assertEqual(state["positions"], {})
        self.assertIsNone(payload["position_id"])
        self.assertEqual(next(iter(state["orders"].values()))["status"], "NOT_FILLED")

    def test_nonfinite_book_evidence_is_strict_and_recoverable(self):
        payload = self.simulate({"no": [[float("inf"), 10]]})
        self.assertEqual(payload["result"]["disposition"], INVALID_BOOK)
        encoded = self.path.read_text()
        self.assertNotIn("Infinity", encoded)
        state = self.restart()
        self.assertEqual(next(iter(state["orders"].values()))["execution"], payload)
        self.assertEqual(state["positions"], {})

    def test_missing_and_nonfinite_size_distinguished_in_evidence(self):
        missing = observation({"no": [[.52, None]]})
        invalid = observation({"no": [[.52, float("nan")]]})
        self.assertNotEqual(missing, invalid)
        payload = self.simulate(restore_observation(invalid))
        self.assertEqual(payload["result"]["disposition"], NO_LIQUIDITY)
        self.assertEqual(next(iter(self.restart()["orders"].values()))["execution"], payload)

    def test_duplicate_simulation_rejected_even_after_restart(self):
        self.simulate()
        self.restart()
        before = self.path.read_bytes()
        with self.assertRaises(JournalError):
            self.simulate()
        self.assertEqual(before, self.path.read_bytes())

    def test_later_trade_through_cannot_fill_prior_no_fill(self):
        self.simulate({"no": [[.4, 100]]})
        self.assertEqual(self.restart()["positions"], {})
        with self.assertRaises(JournalError):
            self.simulate({"no": [[.6, 100]]})

    def test_key_remains_consumed_and_place_order_unfilled(self):
        self.assertFalse(self.intent)
        self.simulate()
        self.restart()
        client = ShadowClient(self.session)
        result = client.place_market_order(**self.request)
        self.assertFalse(result)
        self.assertEqual(result.order_id, self.intent.order_id)
        with self.assertRaises(JournalError):
            client.place_market_order(**dict(self.request, count=3))

    def test_synthetic_execution_still_supported_and_exclusive(self):
        self.session.journal.synthetic_execution(execution_id="synthetic", intended_order_id=self.intent.order_id, quantity=2, price=.5)
        self.assertEqual(next(iter(self.restart()["orders"].values()))["execution"]["source"], "synthetic_test_input")
        with self.assertRaises(JournalError):
            self.simulate()

    def test_simulation_blocks_later_synthetic_execution(self):
        self.simulate()
        with self.assertRaises(ValueError):
            self.session.journal.synthetic_execution(execution_id="synthetic", intended_order_id=self.intent.order_id, quantity=2, price=.5)

    def test_unknown_order_never_consumes_snapshot(self):
        before = self.path.read_bytes()
        with self.assertRaises(JournalError):
            self.session.simulate_execution("unknown", object())
        self.assertEqual(before, self.path.read_bytes())

    def test_malformed_payload_recovery_fails(self):
        self.simulate()
        self.rewrite_last(lambda event: event["payload"].pop("result"))
        with self.assertRaises(JournalError):
            self.restart()

    def test_tampered_observation_hash_failure(self):
        self.simulate()
        self.rewrite_last(lambda event: event["payload"]["result"]["observation"]["book"]["no"][0].__setitem__(1, 1), rehash=False)
        with self.assertRaises(JournalError):
            self.restart()

    def test_tampered_observation_valid_hash_model_failure(self):
        self.simulate()
        self.rewrite_last(lambda event: event["payload"]["result"]["observation"]["book"]["no"][0].__setitem__(1, 1))
        with self.assertRaises(JournalError):
            self.restart()

    def test_tampered_simulated_price_valid_hash_failure(self):
        self.simulate()
        self.rewrite_last(lambda event: event["payload"]["result"].__setitem__("simulated_price", .52))
        with self.assertRaises(JournalError):
            self.restart()

    def test_unknown_model_version_fails_recovery(self):
        self.simulate()
        self.rewrite_last(lambda event: event["payload"]["result"].__setitem__("model_id", "future_model"))
        with self.assertRaises(JournalError):
            self.restart()

    def test_bad_position_identity_fails_recovery(self):
        self.simulate()
        self.rewrite_last(lambda event: event["payload"].__setitem__("position_id", "invented"))
        with self.assertRaises(JournalError):
            self.restart()

    def test_simulation_lost_ack_recovers_and_rejects_duplicate(self):
        with patch.object(self.session.journal, "_publish", side_effect=RuntimeError("crash")), self.assertRaises(RuntimeError):
            self.simulate()
        self.assertEqual(len(self.restart()["positions"]), 1)
        with self.assertRaises(JournalError):
            self.simulate()

    def test_no_fetch_no_live_transport(self):
        with patch.object(ShadowClient, "get_orderbook", side_effect=AssertionError("fetch")), \
             patch("socket.socket", side_effect=AssertionError("network")):
            self.assertEqual(self.simulate()["result"]["disposition"], FULL)
        import ast
        for name in ("shadow_execution.py", "shadow_journal.py", "shadow.py"):
            tree = ast.parse(Path(__file__).with_name(name).read_text())
            imported = [alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names]
            self.assertNotIn("KalshiClient", imported)

    def test_position_close_still_works(self):
        result = self.simulate()
        self.session.journal.close_position(close_id="c1", position_id=result["position_id"], reason="test")
        self.assertEqual(self.restart()["positions"][result["position_id"]]["status"], "CLOSED")


if __name__ == "__main__":
    unittest.main()
