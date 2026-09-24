"""Telegram operator V2. No network and no process start."""
import json
import unittest
from unittest.mock import patch

from kalshi_bot.operator_control import CONFIRM, ConfirmError
from kalshi_bot.telegram.handlers import handle_command


class _Settings:
    chat_id = "chat-1"
    token = ""


def _run(text, chat="chat-1"):
    sent = []
    with patch("kalshi_bot.telegram.settings.load_settings", return_value=_Settings):
        handle_command(text, sent.append, chat_id=chat)
    return sent


class OperatorTests(unittest.TestCase):
    def setUp(self):
        CONFIRM.clear()

    def test_unauthorized_chat_does_nothing(self):
        with patch("kalshi_bot.operator_control.preflight_account") as preflight:
            sent = _run("/account", chat="other")
        preflight.assert_not_called()
        self.assertEqual(sent, ["Unauthorized."])
        self.assertNotIn("Available", " ".join(sent))

    def test_start_is_help_only(self):
        sent = _run("/start round 5000")
        self.assertTrue(any("does not start a trader" in line for line in sent))

    def test_confirmation_is_one_use_and_bound(self):
        token = CONFIRM.issue("start_live", "chat-1", {"profile": "max_risk_micro"})
        payload = CONFIRM.take("start_live", "chat-1", token, now=0)
        self.assertEqual(payload["profile"], "max_risk_micro")
        with self.assertRaises(ConfirmError):
            CONFIRM.take("start_live", "chat-1", token, now=0)
        other = CONFIRM.issue("resume", "chat-1", {}, now=0)
        with self.assertRaises(ConfirmError):
            CONFIRM.take("start_live", "chat-1", other, now=0)
        with self.assertRaises(ConfirmError):
            CONFIRM.take("resume", "chat-2", CONFIRM.issue("resume", "chat-1", {}, now=0), now=0)

    def test_confirmation_expires(self):
        token = CONFIRM.issue("resume", "chat-1", {}, now=0)
        with self.assertRaises(ConfirmError):
            CONFIRM.take("resume", "chat-1", token, now=61)

    def test_start_live_preflight_failure_does_not_start(self):
        report = (False, "Refused: available too low", {"problems": ["available too low"]})
        with patch("kalshi_bot.operator_control.live_preflight_report", return_value=report), \
             patch("kalshi_bot.operator_control.start_live_safe") as start:
            sent = _run("/start_live")
        start.assert_not_called()
        self.assertTrue(any("Refused" in line for line in sent))

    def test_stale_preflight_blocks_confirm(self):
        ok = (True, "LIVE TRADING REQUEST\nREAL CAPITAL", {"problems": []})
        bad = (False, "Refused: trader appeared", {"problems": ["trader appeared"]})
        with patch("kalshi_bot.operator_control.live_preflight_report", return_value=ok), \
             patch("kalshi_bot.operator_control.start_live_safe") as start:
            sent = _run("/start_live")
            token = sent[-1].split()[-1]
            with patch("kalshi_bot.operator_control.live_preflight_report", return_value=bad):
                confirmed = _run(f"/confirm_live {token}")
        start.assert_not_called()
        self.assertTrue(any("Refused" in line for line in confirmed))

    def test_conflicting_trader_refuses_paper(self):
        modes = {"paper": False, "live": True, "shadow": False, "ambiguous": False,
                 "heartbeat": True, "paused": False, "profile": "max_risk_micro"}
        with patch("kalshi_bot.operator_control.detect_modes", return_value=modes), \
             patch("kalshi_bot.operator_control.start_paper_round") as paper:
            sent = _run("/papertrade 500")
        paper.assert_not_called()
        self.assertTrue(any("LIVE" in line or "running" in line for line in sent))

    def test_resume_refuses_independent_halt(self):
        with patch("kalshi_bot.operator_control.detect_modes", return_value={
            "paper": True, "live": False, "shadow": False, "ambiguous": False,
            "heartbeat": True, "paused": True, "profile": "max_risk_paper",
        }), patch("kalshi_bot.operator_control.independent_halts", return_value=["live halt: balance"]), \
             patch("kalshi_bot.operator_control._sim_safety", return_value={"live_halt_reason": "balance"}), \
             patch("kalshi_bot.operator_control.set_entries_paused") as pause:
            sent = _run("/resume")
        pause.assert_not_called()
        self.assertTrue(any("Refused" in line for line in sent))

    def test_resume_confirm_clears_only_pause(self):
        with patch("kalshi_bot.operator_control.detect_modes", return_value={
            "paper": True, "live": False, "shadow": False, "ambiguous": False,
            "heartbeat": True, "paused": True, "profile": "max_risk_paper",
        }), patch("kalshi_bot.operator_control.independent_halts", return_value=[]), \
             patch("kalshi_bot.operator_control._sim_safety", return_value={"live_halt_reason": ""}), \
             patch("kalshi_bot.operator_control.set_entries_paused") as pause:
            sent = _run("/resume")
            token = sent[-1].split()[-1]
            _run(f"/confirm_resume {token}")
        pause.assert_called_once_with(False, source="telegram_confirm_resume")

    def test_paper_and_shadow_identity(self):
        modes = {"paper": False, "live": False, "shadow": False, "ambiguous": False,
                 "heartbeat": False, "paused": False, "profile": ""}
        with patch("kalshi_bot.operator_control.detect_modes", return_value=modes), \
             patch("kalshi_bot.operator_control.start_paper_round", return_value={
                 "session_tag": "round_1", "profile": "max_risk_paper", "capital": 500,
             }) as paper:
            preview = _run("/papertrade 500")
            self.assertIn("PAPER — NO REAL CAPITAL", preview[-1])
            paper.assert_not_called()
            token = preview[-1].split()[-1]
            started = _run(f"/confirm_paper {token}")
        paper.assert_called_once()
        self.assertIn("PAPER — NO REAL CAPITAL", started[-1])

    def test_shadow_refuses_existing_era_directory(self):
        sent = _run("/shadow shadow_era1b_001 1000")
        self.assertTrue(any("Refused" in line for line in sent))

    def test_vault_confirmation_does_not_enqueue_early(self):
        with patch("kalshi_bot.operator_control._sim_safety", return_value={
            "balance": 100, "vault_balance": 0, "live_halt_reason": "", "daily_dd": None, "consec_losses": 0,
        }), patch("kalshi_bot.operator_control.enqueue_take_cash") as cash:
            sent = _run("/take_cash 25")
            cash.assert_not_called()
            self.assertIn("Projected", sent[-1])
            token = sent[-1].split()[-1]
            _run(f"/confirm_take_cash {token}")
        cash.assert_called_once()

    def test_setting_rejects_out_of_range(self):
        with patch("kalshi_bot.operator_control.enqueue_bot_command") as queue:
            sent = _run("/set_max_pos 90")
        queue.assert_not_called()
        self.assertTrue(any("Refused" in line for line in sent))

    def test_audit_has_no_secret_or_raw_challenge(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            with patch("kalshi_bot.operator_control.AUDIT_PATH", path), \
                 patch("kalshi_bot.operator_control.LOGS_DIR", Path(tmp)):
                from kalshi_bot.operator_control import audit
                audit("chat-1", "/start_live", "start_live", "challenge", "awaiting", challenge="abc123")
            text = path.read_text(encoding="utf-8")
        self.assertNotIn("abc123", text)
        self.assertNotIn("chat-1", text)
        record = json.loads(text)
        self.assertEqual(record["outcome"], "challenge")
        self.assertTrue(record["challenge"])

    def test_aliases_do_not_start(self):
        with patch("kalshi_bot.operator_control.start_live_safe") as live, \
             patch("kalshi_bot.operator_control.start_paper_round") as paper:
            sent = _run("/go standard")
            _run("/safe_live")
            _run("/start_round 500")
        live.assert_not_called()
        paper.assert_not_called()
        self.assertTrue(any("Use /start_live" in line for line in sent))
