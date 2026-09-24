"""Control Room environment routing. No network and no Streamlit runtime."""
import unittest
from pathlib import Path

from dashboard.environments import (
    LIVE_ACK_KEY,
    acknowledge_live,
    enter_shadow,
    live_controls_open,
    sizing_basis_label,
)

ROOT = Path(__file__).resolve().parents[1]


class EnvironmentTests(unittest.TestCase):
    def test_live_controls_stay_closed_until_acknowledged(self):
        state = {}
        self.assertFalse(live_controls_open(state))
        self.assertTrue(live_controls_open(acknowledge_live(state)))
        self.assertFalse(live_controls_open(state))

    def test_shadow_drops_live_control_state(self):
        cleaned = enter_shadow({
            LIVE_ACK_KEY: True,
            "vault_manual_amt": 100,
            "asset_detail_symbol": "BTC",
            "shadow_session": "shadow_era1b_001",
        })
        self.assertEqual(cleaned, {"shadow_session": "shadow_era1b_001"})

    def test_sizing_label_uses_the_durable_flag_only(self):
        self.assertEqual(sizing_basis_label(False), "FROZEN-BALANCE SIZING")
        self.assertEqual(sizing_basis_label(True), "REALIZED-GROSS-EQUITY SIZING")
        self.assertIsNone(sizing_basis_label(None))

    def test_shadow_app_has_no_live_capital_path(self):
        source = (ROOT / "dashboard" / "shadow_app.py").read_text(encoding="utf-8")
        for banned in (
            "enqueue_take_cash", "enqueue_set_config", "vault_panel",
            "place_order", "telegram", "dashboard.pages", "runpy",
        ):
            self.assertNotIn(banned, source)

    def test_control_room_does_not_hide_live_buttons_behind_a_mode_flag(self):
        source = (ROOT / "dashboard" / "control_room.py").read_text(encoding="utf-8")
        self.assertNotIn('mode == "shadow"', source)
        self.assertNotIn("hide_live_buttons", source)
        self.assertIn("LIVE MODE — REAL CAPITAL CAN BE AFFECTED", (ROOT / "dashboard" / "environments.py").read_text(encoding="utf-8"))
        self.assertIn("shadow_app", source)
        self.assertNotIn("enqueue_take_cash", source)
        entry = ROOT / "control_room.py"
        self.assertTrue(entry.is_file())
        self.assertFalse((entry.parent / "pages").exists())
        self.assertNotIn("dashboard/control_room.py", entry.read_text(encoding="utf-8").replace("\\", "/"))
        self.assertIn("from dashboard.control_room import main", entry.read_text(encoding="utf-8"))


class TruthfulnessTests(unittest.TestCase):
    def test_mode_labels(self):
        from dashboard.operator_view import production_banner, production_mode, shadow_banner, shadow_running
        self.assertEqual(production_mode("running", "paper"), "PAPER")
        self.assertEqual(production_banner("PAPER"), "PAPER — NO REAL CAPITAL")
        self.assertNotIn("LIVE", production_banner("PAPER"))
        self.assertEqual(production_mode("running", "live"), "LIVE")
        self.assertEqual(production_mode("stopped", "live"), "UNKNOWN")
        self.assertEqual(production_mode("running", ""), "UNKNOWN")
        self.assertIn("MODE UNKNOWN", production_banner("UNKNOWN"))
        self.assertNotIn("LIVE —", production_banner("UNKNOWN"))
        self.assertFalse(shadow_running("stopped", "shadow"))
        self.assertTrue(shadow_running("running", "shadow"))
        self.assertIn("HISTORICAL — NOT RUNNING", shadow_banner(False))
        self.assertIn("RUNNING NOW", shadow_banner(True))

    def test_scratch_is_not_a_loss(self):
        from dashboard.operator_view import trade_outcome
        self.assertEqual(trade_outcome(1), "win")
        self.assertEqual(trade_outcome(-1), "loss")
        self.assertEqual(trade_outcome(0), "scratch")

    def test_live_ceiling_is_not_the_dashboard_default(self):
        from dashboard.operator_view import loss_streak_ceiling
        self.assertEqual(loss_streak_ceiling("LIVE", 8), 3)
        self.assertIsNone(loss_streak_ceiling("UNKNOWN", 8))

    def test_shadow_page_does_not_spawn_or_default_era1b(self):
        source = (ROOT / "dashboard" / "shadow_app.py").read_text(encoding="utf-8")
        self.assertNotIn("Popen", source)
        self.assertNotIn("subprocess", source)
        self.assertNotIn('default="shadow_era1b_001"', source)
        self.assertIn("HISTORICAL — NOT RUNNING", source)
        self.assertIn("end time unavailable", source)

    def test_operational_loaders_do_not_mock_money(self):
        source = (ROOT / "dashboard" / "data" / "loaders.py").read_text(encoding="utf-8")
        self.assertNotIn("generate_trades", source)
        self.assertNotIn("generate_portfolio", source)

    def test_exchange_failure_is_not_local_equity(self):
        source = (ROOT / "dashboard" / "pages" / "00_ops.py").read_text(encoding="utf-8")
        self.assertIn("EXCHANGE DATA UNAVAILABLE", source)
        self.assertIn("not exchange", source)
        self.assertNotIn("generate_portfolio", source)
        room = (ROOT / "dashboard" / "control_room.py").read_text(encoding="utf-8")
        self.assertNotIn('title="Live', room)
        self.assertIn("Loss-streak ceiling: 3", (ROOT / "dashboard" / "pages" / "00_ops.py").read_text(encoding="utf-8"))
        self.assertNotIn("reconcile()", (ROOT / "dashboard" / "shadow_app.py").read_text(encoding="utf-8"))
