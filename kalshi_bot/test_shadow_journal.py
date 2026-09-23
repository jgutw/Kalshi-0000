"""Synthetic durability/recovery tests. No runtime data, credentials or feeds."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from .shadow import ShadowSession, ShadowClient, reject_live_start_in_shadow
from .shadow_journal import JournalError, canonical


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "shadow_data"
        self.session = ShadowSession(self.root, "session1", "shadow-test", "a" * 40)
        self.addCleanup(self.session.close)
        self.path = self.session.journal.path

    def restart(self):
        self.session.close()
        self.session = ShadowSession.recover(self.root, "session1")
        self.addCleanup(self.session.close)
        return self.session.journal

    def request(self, **kwargs):
        result = dict(asset="BTC", window_id_ts=900, decision_id="d1", strategy="test",
                      ticker="BTC-TEST", side="yes", count=2, limit_price=.4)
        result.update(kwargs)
        return result

    def intend(self, **kwargs):
        return self.session.journal.intend(**self.request(**kwargs))

    def filled(self):
        order = self.intend()
        execution = self.session.journal.synthetic_execution(execution_id="e1",
                    intended_order_id=order["intended_order_id"], quantity=2, price=.42)
        return order, execution

    def test_intent_key_retry_restart(self):
        order = self.intend()
        before = self.path.read_bytes()
        journal = self.restart()
        recovered = journal.state["orders"][order["idempotency_key"]]
        self.assertEqual(order, recovered)
        self.assertEqual(self.intend(), order)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(journal.state["positions"], {})
        self.assertEqual(json.loads(order["idempotency_key"]), ["shadow-entry-v1", "session1", "BTC", 900])

    def test_conflicting_retries(self):
        self.intend()
        self.restart()
        before = self.path.read_bytes()
        for values in ({"count": 3}, {"side": "no"}, {"decision_id": "d2"},
                       {"ticker": "other"}, {"strategy": "other"}, {"limit_price": .5}):
            with self.subTest(values=values), self.assertRaises(JournalError):
                self.intend(**values)
        self.assertEqual(before, self.path.read_bytes())

    def test_assets_and_windows_independent(self):
        self.intend()
        self.intend(asset="ETH")
        self.intend(window_id_ts=1800)
        self.assertEqual(len(self.restart().state["orders"]), 3)

    def test_missing_identity_and_no_fabrication(self):
        client = ShadowClient(self.session)
        with self.assertRaises(TypeError):
            client.place_market_order("TEST", "yes", 2)
        for change in ({"window_id_ts": True}, {"window_id_ts": 1.5}, {"decision_id": ""},
                       {"strategy": None}, {"quotes": {"unknown": .5}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.intend(**change)
        order = self.intend()
        self.assertEqual(set(order["request"]["quotes"].values()), {None})

    def test_open_position_and_execution_retry(self):
        order, execution = self.filled()
        self.assertNotEqual(order["intended_order_id"], execution["position_id"])
        before = self.path.read_bytes()
        journal = self.restart()
        pos = journal.state["positions"][execution["position_id"]]
        self.assertEqual((pos["status"], pos["quantity"], pos["entry_price"]), ("OPEN", 2, .42))
        self.assertEqual(journal.synthetic_execution(execution_id="e1", intended_order_id=order["intended_order_id"],
                                                    quantity=2, price=.42), execution)
        self.assertEqual(before, self.path.read_bytes())
        with self.assertRaises(JournalError):
            journal.synthetic_execution(execution_id="e1", intended_order_id=order["intended_order_id"], quantity=2, price=.5)

    def test_closed_position_and_consumed_key(self):
        order, execution = self.filled()
        close = dict(close_id="c1", position_id=execution["position_id"], reason="synthetic exit")
        self.session.journal.close_position(**close)
        journal = self.restart()
        self.assertEqual(journal.state["positions"][execution["position_id"]]["status"], "CLOSED")
        before = self.path.read_bytes()
        journal.close_position(**close)
        self.assertEqual(self.intend()["intended_order_id"], order["intended_order_id"])
        self.assertEqual(before, self.path.read_bytes())
        with self.assertRaises(ValueError):
            journal.close_position(**dict(close, close_id="c2"))

    def test_no_fill_and_invalid_transitions(self):
        order = self.intend()
        j = self.session.journal
        with self.assertRaises(ValueError):
            j.synthetic_execution(execution_id="e0", intended_order_id=order["intended_order_id"], quantity=1, price=.4)
        j.synthetic_execution(execution_id="e1", intended_order_id=order["intended_order_id"], quantity=0)
        j = self.restart()
        self.assertEqual(j.state["positions"], {})
        self.assertEqual(next(iter(j.state["orders"].values()))["status"], "NOT_FILLED")
        with self.assertRaises(ValueError):
            j.synthetic_execution(execution_id="e2", intended_order_id=order["intended_order_id"], quantity=2, price=.4)

    def test_prewrite_failure_consumes_nothing_and_poisons(self):
        with patch.object(self.session.journal, "_write_bytes", side_effect=OSError("before write")):
            with self.assertRaises(OSError):
                self.intend()
        with self.assertRaises(JournalError):
            self.intend()
        self.assertEqual(self.restart().state["orders"], {})
        self.intend()

    def test_partial_write_fails_recovery_without_repair(self):
        def partial(data):
            self.session.journal._stream.write(data[:30])
            raise OSError("partial write")
        with patch.object(self.session.journal, "_write_bytes", side_effect=partial), self.assertRaises(OSError):
            self.intend()
        before = self.path.read_bytes()
        self.session.close()
        with self.assertRaises(JournalError):
            ShadowSession.recover(self.root, "session1")
        self.assertEqual(before, self.path.read_bytes())

    def test_fsync_failure_requires_recovery_before_retry(self):
        with patch("kalshi_bot.shadow_journal.os.fsync", side_effect=OSError("sync uncertain")), self.assertRaises(OSError):
            self.intend()
        with self.assertRaises(JournalError):
            self.intend()
        j = self.restart()
        before = self.path.read_bytes()
        self.assertEqual(len(j.state["orders"]), 1)
        self.intend()
        self.assertEqual(before, self.path.read_bytes())

    def test_crash_after_sync_before_publish_replays_position(self):
        order = self.intend()
        j = self.session.journal
        with patch.object(j, "_publish", side_effect=RuntimeError("lost acknowledgement")), self.assertRaises(RuntimeError):
            j.synthetic_execution(execution_id="e1", intended_order_id=order["intended_order_id"], quantity=2, price=.4)
        with self.assertRaises(JournalError):
            _ = j.state
        self.assertEqual(len(self.restart().state["positions"]), 1)

    def test_close_lost_ack_reconstructs_closed(self):
        _, execution = self.filled()
        with patch.object(self.session.journal, "_publish", side_effect=RuntimeError("crash")), self.assertRaises(RuntimeError):
            self.session.journal.close_position(close_id="c1", position_id=execution["position_id"], reason="test")
        positions = self.restart().state["positions"]
        self.assertEqual(positions[execution["position_id"]]["status"], "CLOSED")

    def test_recovery_sync_failure_exposes_no_writer_and_releases_lock(self):
        self.intend()
        self.session.close()
        with patch("kalshi_bot.shadow_journal.os.fsync", side_effect=OSError("recovery sync")), self.assertRaises(OSError):
            ShadowSession.recover(self.root, "session1")
        self.assertEqual(len(self.restart().state["orders"]), 1)

    def test_abrupt_synthetic_process_exit_releases_ownership(self):
        self.session.close()
        script = (
            "from kalshi_bot.shadow import ShadowSession\nimport sys, os\n"
            "s=ShadowSession.recover(sys.argv[1], 'session1')\n"
            "s.journal.intend(asset='BTC',window_id_ts=900,decision_id='d1',strategy='test',"
            "ticker='BTC-TEST',side='yes',count=2,limit_price=.4)\nos._exit(0)"
        )
        result = subprocess.run([sys.executable, "-B", "-c", script, str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.restart().state["orders"]), 1)
        before = self.path.read_bytes()
        self.intend()
        self.assertEqual(before, self.path.read_bytes())

    def test_acknowledgement_after_sync(self):
        j = self.session.journal
        original = j._sync
        def sync():
            self.assertEqual(j._state["orders"], {})
            self.assertTrue(self.path.read_bytes().endswith(b"\n"))
            original()
        with patch.object(j, "_sync", side_effect=sync) as called:
            self.intend()
            called.assert_called_once()

    def test_second_writer_and_release(self):
        with self.assertRaises(OSError):
            ShadowSession.recover(self.root, "session1")
        self.intend()
        self.assertEqual(len(self.restart().state["orders"]), 1)

    def test_second_process_writer_rejected(self):
        script = "from kalshi_bot.shadow import ShadowSession\nimport sys\ntry:\n s=ShadowSession.recover(sys.argv[1], 'session1')\nexcept OSError:\n sys.exit(0)\ns.close()\nsys.exit(9)"
        result = subprocess.run([sys.executable, "-B", "-c", script, str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_threads_exact_retry_one_event_and_boundary(self):
        j = self.session.journal
        original = j._write_bytes
        def write(data):
            with self.assertRaises(RuntimeError):
                reject_live_start_in_shadow()
            original(data)
        with patch.object(j, "_write_bytes", side_effect=write), ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda _: self.intend()["intended_order_id"], range(10)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(self.path.read_bytes().splitlines()), 1)

    def test_corrupt_and_truncated_lines_fail_closed(self):
        self.intend()
        self.session.close()
        good = self.path.read_bytes()
        for bad in (good[:-1], good + b'{"partial":', good + b'not-json\n', good + b'\n',
                    good.replace(b'"count":2', b'"count":3'), good + b'\xff\n', good + good):
            self.path.write_bytes(bad)
            with self.subTest(bad=bad[-20:]), self.assertRaises(JournalError):
                ShadowSession.recover(self.root, "session1")
            self.assertEqual(self.path.read_bytes(), bad)
        self.path.write_bytes(good)
        self.assertEqual(len(self.restart().state["orders"]), 1)

    def test_valid_hash_invalid_semantics_rejected(self):
        self.intend()
        self.session.close()
        original = json.loads(self.path.read_text())
        for field, value in (("seq", 2), ("shadow_session_id", "other"), ("mode", "LIVE"), ("kind", "UNKNOWN")):
            event = dict(original)
            event[field] = value
            event.pop("sha256")
            event["sha256"] = hashlib.sha256(canonical(event).encode()).hexdigest()
            self.path.write_text(canonical(event) + "\n", encoding="utf-8")
            with self.assertRaises(JournalError):
                ShadowSession.recover(self.root, "session1")

    def test_duplicate_json_keys_and_nonfinite_fail_closed(self):
        self.intend()
        self.session.close()
        good = self.path.read_bytes()
        for bad in (good.replace(b'"seq":1', b'"seq":1,"seq":1'),
                    good.replace(b'"limit_price":0.4', b'"limit_price":NaN'),
                    good.replace(b'"limit_price":0.4', b'"limit_price":1e999')):
            self.assertNotEqual(good, bad)
            self.path.write_bytes(bad)
            with self.assertRaises(JournalError):
                ShadowSession.recover(self.root, "session1")
            self.assertEqual(self.path.read_bytes(), bad)

    def test_bad_metadata_and_recovery_flags(self):
        self.session.close()
        path = self.path.parent / "session.json"
        original = json.loads(path.read_text())
        for change in ({"mode": "LIVE"}, {"session_tag": "p6c_d1_validation"},
                       {"shadow_session_id": "other"}, {"startup_utc": "2026-01-01T00:00:00"}):
            path.write_text(json.dumps(dict(original, **change)))
            with self.assertRaises(ValueError):
                ShadowSession.recover(self.root, "session1")
        path.write_text(json.dumps(original))
        for flag in ("live", "fresh_round"):
            with self.assertRaises(ValueError):
                ShadowSession.recover(self.root, "session1", **{flag: True})

    def test_corrupt_transition_with_valid_hash(self):
        self.intend()
        self.session.close()
        original = json.loads(self.path.read_text())
        for mutation in ("key", "request"):
            event = json.loads(json.dumps(original))
            if mutation == "key":
                event["payload"]["idempotency_key"] = "wrong-key"
            else:
                event["payload"]["request"]["asset"] = "btc"
            event.pop("sha256")
            event["sha256"] = hashlib.sha256(canonical(event).encode()).hexdigest()
            self.path.write_text(canonical(event) + "\n")
            with self.assertRaises(JournalError):
                ShadowSession.recover(self.root, "session1")

    def test_missing_journal_and_old_schema_fail_closed(self):
        self.session.close()
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            ShadowSession.recover(self.root, "session1")
        metadata_path = self.path.parent / "session.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["schema_version"] = 1
        metadata_path.write_text(json.dumps(metadata))
        with self.assertRaises(ValueError):
            ShadowSession.recover(self.root, "session1")

    def test_recovery_protected_paths_and_no_legacy_reads(self):
        for name in ("logs", "sessions", "research_data", "p6c_d1_validation"):
            with self.assertRaises(ValueError):
                ShadowSession.recover(Path(self.tmp.name) / name / "shadow_data", "session1")
        self.session.close()
        original = __import__("io").open
        opened = []
        def checked(path, *args, **kwargs):
            resolved = Path(path).resolve()
            self.assertTrue(resolved.is_relative_to(self.root))
            opened.append(resolved.name)
            return original(path, *args, **kwargs)
        with patch("io.open", side_effect=checked):
            self.restart()
        self.assertEqual(set(opened), {"session.json", "writer.lock", "lifecycle.jsonl"})

    def test_generic_append_cannot_bypass_lifecycle(self):
        for name in ("intended_orders", "positions", "simulated_fills", "outcomes", "trades"):
            with self.assertRaises(ValueError):
                self.session.append(name, {})

    def test_state_is_detached(self):
        self.intend()
        state = self.session.journal.state
        state["orders"].clear()
        self.assertEqual(len(self.session.journal.state["orders"]), 1)


if __name__ == "__main__":
    unittest.main()
