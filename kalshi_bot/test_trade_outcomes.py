"""Offline trade-outcome rows. No logs and no trader."""
import unittest

from kalshi_bot.research.trade_outcomes import build_trade_outcome, rows_from_trades


def _trade(**extra):
    row = {
        "asset": "BTC",
        "strategy": "lag_arb",
        "side": "yes",
        "entry": 0.61,
        "exit": 0.0,
        "pnl": -61.0,
        "fees": 0.2,
        "live": False,
        "decision_id": "A1",
        "decision": {
            "p_real": 0.69,
            "p_market": 0.60,
            "time_remaining": 83,
            "realized_vol": 0.72,
            "kalshi_spread": 0.03,
            "lag_confidence": 0.81,
            "response_gap": 0.021,
        },
    }
    row.update(extra)
    return row


class TradeOutcomeTests(unittest.TestCase):
    def test_yes_loss_with_edge_is_a_model_failure(self):
        row = build_trade_outcome(_trade())
        self.assertEqual(row["mode"], "PAPER")
        self.assertEqual(row["result"], "loss")
        self.assertAlmostEqual(row["p_side"], 0.69)
        self.assertAlmostEqual(row["edge"], 0.09)
        self.assertIn("model_failure", row["labels"])
        self.assertIn("wide_spread", row["labels"])
        self.assertEqual(row["fee_treatment"], "net_of_logged_fees")

    def test_no_side_is_complemented(self):
        row = build_trade_outcome(_trade(side="no", exit=1.0, pnl=43.0, decision={
            "p_real": 0.36,
            "p_market": 0.44,
            "kalshi_spread": 0.01,
        }))
        self.assertAlmostEqual(row["p_side"], 0.64)
        self.assertAlmostEqual(row["q_side"], 0.56)
        self.assertEqual(row["result"], "win")
        self.assertNotIn("model_failure", row["labels"])

    def test_scratch_is_not_a_loss(self):
        row = build_trade_outcome(_trade(exit=0.61, pnl=0.0))
        self.assertEqual(row["result"], "scratch")
        self.assertNotIn("model_failure", row["labels"])

    def test_missing_side_does_not_invent_an_edge(self):
        row = build_trade_outcome(_trade(side=None))
        self.assertIsNone(row["edge"])
        self.assertIn("side", row["coverage_missing"])
        self.assertNotIn("model_failure", row["labels"])

    def test_unknown_mode_is_not_live(self):
        row = build_trade_outcome(_trade(live=None))
        self.assertEqual(row["mode"], "UNKNOWN")

    def test_contract_win_with_negative_pnl_is_execution_drag(self):
        row = build_trade_outcome(_trade(exit=1.0, pnl=-0.4))
        self.assertIn("execution_drag", row["labels"])
        self.assertNotIn("model_failure", row["labels"])

    def test_rows_keep_one_per_trade(self):
        rows = rows_from_trades([_trade(), _trade(decision_id="A2", pnl=10, exit=1)])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["result"], "win")


if __name__ == "__main__":
    unittest.main()
