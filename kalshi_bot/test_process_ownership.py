"""Process ownership and numeric guards. No trader is started."""
import json
import math
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from kalshi_bot import process_ownership as own
from kalshi_bot.operator_control import require_finite
from kalshi_bot.runtime_control import setting_bounds


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = patch.multiple(
            own,
            OWNERSHIP_PATH=root / "own.json",
            LOCK_PATH=root / "start.lock",
            SHADOW_STOP_PATH=root / "shadow_stop.request",
            LOGS_DIR=root,
        )
        self.paths.start()
        self.inspect = patch.object(own, "inspect_process")
        self.scan = patch.object(own, "scan_traders", return_value=[])
        self.inspect = self.inspect.start()
        self.scan.start()

    def tearDown(self):
        patch.stopall()
        self.tmp.cleanup()

    def _own(self, mode="live", pid=10, created="T1"):
        own._write_record({
            "mode": mode, "pid": pid, "created": created, "session": "s",
            "marker": own.MARKERS[mode], "started_at": "now",
        })

    def test_live_process_with_stale_heartbeat_is_running(self):
        self._own()
        self.inspect.return_value = {"state": "alive", "created": "T1", "command": "run_kalshi_bot.py --live"}
        found = own.reconcile()
        self.assertEqual(found.state, "running")
        self.assertEqual(found.mode, "live")

    def test_missing_heartbeat_file_still_uses_ownership(self):
        self._own(mode="paper")
        self.inspect.return_value = {"state": "alive", "created": "T1", "command": "run_kalshi_bot.py"}
        self.assertEqual(own.reconcile().mode, "paper")

    def test_unreadable_ownership_fails_closed(self):
        own.OWNERSHIP_PATH.write_text("{", encoding="utf-8")
        self.assertEqual(own.reconcile().state, "unknown")
        self.assertIsNotNone(own.refuse_if_conflict("live"))

    def test_dead_process_is_reconciled(self):
        self._own()
        self.inspect.return_value = {"state": "dead"}
        self.assertEqual(own.reconcile().state, "dead")
        self.assertFalse(own.OWNERSHIP_PATH.exists())
        self.assertIsNone(own.refuse_if_conflict("paper"))

    def test_pid_reuse_does_not_keep_the_old_identity(self):
        self._own(created="OLD")
        self.inspect.return_value = {"state": "alive", "created": "NEW", "command": "run_kalshi_bot.py --live"}
        found = own.reconcile()
        self.assertEqual(found.state, "reused")
        self.assertFalse(own.OWNERSHIP_PATH.exists())
        self.inspect.return_value = {"state": "dead"}
        self.assertIsNone(own.refuse_if_conflict("paper"))

    def test_shadow_ownership_blocks_live(self):
        self._own(mode="shadow")
        self.inspect.return_value = {"state": "alive", "created": "T1", "command": "run_shadow.py"}
        reason = own.refuse_if_conflict("live")
        self.assertIn("shadow", reason)

    def test_stop_with_live_process_does_not_archive_on_timeout(self):
        self._own()
        self.inspect.return_value = {"state": "alive", "created": "T1", "command": "run_kalshi_bot.py --live"}
        clock = {"t": 0.0}
        with patch("kalshi_bot.process_ownership.time.monotonic", side_effect=lambda: clock["t"]), \
             patch("kalshi_bot.process_ownership.time.sleep", side_effect=lambda s: clock.__setitem__("t", clock["t"] + s)), \
             patch("kalshi_bot.runtime_control.enqueue_bot_command") as queue:
            result = own.request_graceful_stop()
        queue.assert_called_once()
        self.assertEqual(result["outcome"], "timeout")
        self.assertTrue(own.OWNERSHIP_PATH.exists())

    def test_stop_verified_when_process_exits(self):
        self._own()
        self.inspect.side_effect = [
            {"state": "alive", "created": "T1", "command": "run_kalshi_bot.py --live"},
            {"state": "dead"},
        ]
        with patch("kalshi_bot.runtime_control.enqueue_bot_command") as queue:
            result = own.request_graceful_stop()
        queue.assert_called_once()
        self.assertEqual(result["outcome"], "stopped")
        self.assertFalse(own.OWNERSHIP_PATH.exists())

    def test_stop_shadow_requests_flag_without_kill(self):
        self._own(mode="shadow")
        self.inspect.side_effect = [
            {"state": "alive", "created": "T1", "command": "run_shadow.py"},
            {"state": "dead"},
        ]
        result = own.request_graceful_stop()
        self.assertEqual(result["outcome"], "stopped")
        self.assertEqual(result["mode"], "shadow")

    def test_second_start_loses_the_lock(self):
        gate = threading.Event()
        release = threading.Event()
        errors = []

        def hold():
            with own.start_lock():
                gate.set()
                release.wait(2)

        thread = threading.Thread(target=hold)
        thread.start()
        gate.wait(2)
        try:
            with own.start_lock():
                errors.append("acquired")
        except RuntimeError as exc:
            errors.append(str(exc))
        release.set()
        thread.join(2)
        self.assertIn("another start is in progress", errors[0])

    def test_non_finite_numbers_are_rejected(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.assertRaises(ValueError):
                require_finite(value)

    def test_live_consec_limit_is_the_trader_ceiling(self):
        self.assertEqual(setting_bounds("set_consec_losses", live=True), (2, 3))
        self.assertEqual(setting_bounds("set_consec_losses", live=False), (2, 8))


if __name__ == "__main__":
    unittest.main()
