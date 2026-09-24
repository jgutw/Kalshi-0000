"""Telegram release gate. No network."""
import unittest
from unittest.mock import patch

from kalshi_bot.telegram.handlers import handle_command


class _Settings:
    chat_id = "chat-1"


class TelegramGateTests(unittest.TestCase):
    def test_unconfirmed_exposure_commands_do_not_act(self):
        sent = []
        with patch("kalshi_bot.telegram.settings.load_settings", return_value=_Settings), \
             patch("kalshi_bot.operator_control.start_live_safe") as start, \
             patch("kalshi_bot.operator_control.enqueue_take_cash") as cash, \
             patch("kalshi_bot.operator_control.enqueue_bot_command") as queue, \
             patch("kalshi_bot.operator_control.start_paper_round") as paper, \
             patch("kalshi_bot.operator_control.live_preflight_report", return_value=(
                 True, "LIVE TRADING REQUEST", {"problems": []})), \
             patch("kalshi_bot.operator_control.detect_modes", return_value={
                 "paper": False, "live": False, "shadow": False, "ambiguous": False,
                 "heartbeat": False, "paused": False, "profile": "",
             }), \
             patch("kalshi_bot.operator_control._sim_safety", return_value={
                 "balance": 100, "vault_balance": 0, "live_halt_reason": "",
                 "daily_dd": None, "consec_losses": 0,
             }):
            for text in (
                "/start_live",
                "/take_cash 100",
                "/go standard",
                "/start_round 500",
                "/set_kelly 0.5",
                "/profile max_risk_micro",
                "/start round 5000",
            ):
                handle_command(text, sent.append, chat_id="chat-1")
            start.assert_not_called()
            cash.assert_not_called()
            queue.assert_not_called()
            paper.assert_not_called()
        self.assertTrue(sent)

    def test_pause_and_status_are_not_refused(self):
        sent = []
        with patch("kalshi_bot.telegram.settings.load_settings", return_value=_Settings), \
             patch("kalshi_bot.telegram.handlers.enqueue_bot_command") as queue, \
             patch("kalshi_bot.telegram.formatters.format_status", return_value="ok"):
            handle_command("/pause", sent.append, chat_id="chat-1")
            handle_command("/status", sent.append, chat_id="chat-1")
        queue.assert_called_once()
        self.assertEqual(queue.call_args.args[0], "pause")
        self.assertIn("ok", sent)
