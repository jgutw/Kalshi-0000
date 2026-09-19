"""Synthetic fixture checks for the offline four-sources-of-edge analysis."""

from __future__ import annotations

from kalshi_bot.research.edge_sources import attribution_generation, flatten_trade, render_report
from kalshi_bot.signal_engine import kelly_binary


def trade(*, side="yes", exit_=1.0, entry=0.55, contracts=10, pnl=4.0, strategy="kalshi15m", decision=None):
    return {
        "ticker": "KXBTC15M-TEST", "asset": "BTC", "side": side,
        "entry": entry, "exit": exit_, "contracts": contracts,
        "amount_usdc": entry * contracts, "fees": 0.0, "pnl": pnl,
        "strategy": strategy, "live": False, "window_id": "2026-01-01 00:00",
        "window_id_ts": 1_700_000_000, "entry_ts": "2026-01-01T00:00:00+00:00",
        "decision_id": "d-1", "decision": decision or {
            "decision_id": "d-1", "per_venue_mids": {"coinbase": 100.0},
            "p_real": 0.6, "p_market": 0.5, "yes_ask": 0.55,
            "yes_bid": 0.53, "no_ask": 0.47, "kalshi_spread": 0.02,
            "size_usd": 6.0, "strategy": "lag_arb",
        },
    }


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS {name}")


def main() -> None:
    yes = flatten_trade(trade(side="yes", exit_=1.0))
    no_win = flatten_trade(trade(side="no", exit_=1.0, entry=0.45, pnl=5.5))
    no_loss = flatten_trade(trade(side="no", exit_=0.0, entry=0.45, pnl=-4.5))
    check("YES orientation", yes["side_won"] is True and yes["yes_settled"] is True)
    check("NO winner orientation", no_win["side_won"] is True and no_win["yes_settled"] is False)
    check("NO loser orientation", no_loss["side_won"] is False and no_loss["yes_settled"] is True)
    check("oriented Brier equality", abs(no_win["brier_yes"] - no_win["brier_traded"]) < 1e-12)

    mtm = flatten_trade(trade(side="yes", exit_=0.62, strategy="early_exit"))
    check("early exit excluded from Brier", mtm["settle_kind"] == "mtm" and mtm["brier_yes"] is None)
    slippage = flatten_trade(trade(entry=0.56))
    check("slippage arithmetic", abs(slippage["slippage_vs_ask"] - 0.01) < 1e-12)
    check("YES Kelly matches signal engine", abs(yes["kelly_fraction"] - kelly_binary(0.6, 0.5)) < 1e-12)
    check("NO Kelly uses complements", abs(no_win["kelly_fraction"] - kelly_binary(0.4, 0.5)) < 1e-12)
    check("thin generation not gold", attribution_generation({"decision_id": "x"}) == "c7_thin")

    doubled = flatten_trade(trade(contracts=20, pnl=8.0))
    check("contracts scale pnl but not R", doubled["pnl"] == 2 * yes["pnl"] and abs(doubled["R"] - yes["R"]) < 1e-12)
    report = render_report([yes, no_win], "synthetic")
    check("report has four layers and overlap", all(name in report for name in ("INFORMATION", "PRICING / MODEL", "EXECUTION", "SIZING", "OVERLAP")))
    print("11/11 edge-source fixture checks passed")


if __name__ == "__main__":
    main()
