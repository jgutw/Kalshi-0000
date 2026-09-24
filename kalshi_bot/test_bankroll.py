"""Trading-bankroll cap. No network and no Kelly change."""
import unittest
from unittest.mock import patch

from kalshi_bot.bankroll import canonical_profile, sizing_cash


class BankrollTests(unittest.TestCase):
    def test_unset_uses_tradeable_cash(self):
        with patch("kalshi_bot.bankroll.trading_bankroll_cap", return_value=None):
            cash, basis = sizing_cash(1000, 250)
        self.assertEqual(cash, 750)
        self.assertIn("tradeable", basis)

    def test_cap_can_be_below_account_cash(self):
        with patch("kalshi_bot.bankroll.trading_bankroll_cap", return_value=2000):
            cash, _basis = sizing_cash(10000, 0)
        self.assertEqual(cash, 2000)

    def test_vault_is_removed_before_the_cap(self):
        with patch("kalshi_bot.bankroll.trading_bankroll_cap", return_value=5000):
            cash, _basis = sizing_cash(1000, 400)
        self.assertEqual(cash, 600)

    def test_aliases_point_at_existing_presets(self):
        self.assertEqual(canonical_profile("standard"), "max_risk_paper")
        self.assertEqual(canonical_profile("micro"), "max_risk_micro")
        self.assertEqual(canonical_profile("tight"), "live_safe")
        self.assertEqual(canonical_profile("aggressive"), "max_risk_micro")


if __name__ == "__main__":
    unittest.main()
