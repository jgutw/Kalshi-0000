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
