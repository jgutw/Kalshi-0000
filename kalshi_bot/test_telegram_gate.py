"""Telegram release gate. No network."""
import unittest
from unittest.mock import patch

from kalshi_bot.telegram.handlers import handle_command


class TelegramGateTests(unittest.TestCase):
    def test_exposure_commands_are_refused_before_any_action(self):
        sent = []
        with patch("kalshi_bot.telegram.handlers.start_live_safe") as start, \
             patch("kalshi_bot.telegram.handlers.enqueue_take_cash") as cash, \
             patch("kalshi_bot.telegram.handlers.enqueue_bot_command") as queue, \
             patch("kalshi_bot.telegram.handlers.start_paper_round") as paper:
            for text in (
                "/resume_live micro",
                "/start_live",
                "/take_cash 100",
                "/go standard",
                "/start_round 500",
                "/resume",
                "/set_kelly 0.5",
                "/profile max_risk_micro",
                "/start round 5000",
            ):
                handle_command(text, sent.append)
            start.assert_not_called()
            cash.assert_not_called()
            queue.assert_not_called()
            paper.assert_not_called()
        self.assertTrue(sent)
        self.assertTrue(all("Refused" in line for line in sent))

    def test_pause_and_status_are_not_refused(self):
        sent = []
        with patch("kalshi_bot.telegram.handlers.enqueue_bot_command") as queue, \
             patch("kalshi_bot.telegram.formatters.format_status", return_value="ok"):
            handle_command("/pause", sent.append)
            handle_command("/status", sent.append)
        queue.assert_called_once()
        self.assertEqual(queue.call_args.args[0], "pause")
        self.assertIn("ok", sent)
