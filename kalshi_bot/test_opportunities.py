"""Synthetic fixture checks for offline decision-opportunity research."""

from __future__ import annotations

from pathlib import Path

from kalshi_bot.research.opportunities import brier, build_opportunities, log_loss, render_report
from scripts.analyze_decision_opportunities import report_path


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS {name}")


def decision(asset: str, window: int, p_real=None, time_remaining=None, **extra):
    return {"asset": asset, "window_id_ts": window, "ticker": f"KX{asset}", "p_real": p_real, "time_remaining": time_remaining, **extra}


def window(asset: str, window_id_ts: int, outcome: str):
    return {"asset": asset, "window_id_ts": window_id_ts, "actual_outcome": outcome, "window_id": str(window_id_ts)}


def main() -> None:
    w = 100
    ticks = [decision("BTC", w, 0.51, 500, ts=str(i)) for i in range(500)]
    rows, coverage = build_opportunities(ticks, [window("BTC", w, "YES")], [])
    check("500 ticks collapse to one opportunity", len(rows) == 1 and coverage["decision_ticks_read"] == 500 and rows[0]["decision_ticks"] == 500)

    rows, _ = build_opportunities([decision("BTC", w, 0.52, 120, ts="a"), decision("BTC", w, 0.55, 90, ts="b"), decision("BTC", w, 0.70, 30, ts="c")], [window("BTC", w, "YES")], [])
    check("preferred snapshot is last ge-60 model", rows[0]["p_real"] == 0.55 and rows[0]["snapshot_selection"] == "preferred_ge_60s")
    rows, _ = build_opportunities([decision("BTC", w, 0.70, 30)], [window("BTC", w, "YES")], [])
    check("late fallback is classified", rows[0]["p_real"] == 0.70 and rows[0]["snapshot_selection"] == "fallback_lt_60s")
    rows, _ = build_opportunities([decision("BTC", w, 0.70, None)], [window("BTC", w, "YES")], [])
    check("missing horizon fallback is excluded from headline", rows[0]["snapshot_selection"] == "fallback_missing_horizon" and "HEADLINE — preferred_ge_60s\np_base: N=0" in render_report(rows, {"ticks_per_window": [1], "decision_ticks_read": 1, "completed_windows": 1, "unique_asset_windows": 1, "conflicting_outcomes": 0, "duplicate_windows": 0, "retained_model_opportunities": 1, "skipped_no_model": 0, "early_wait_no_model": 0, "no_decision_ticks": 0, "trades_missing_window_id_ts": 0}, "fixture"))
    rows, coverage = build_opportunities([decision("BTC", w, None, 300, reason="circuit_breaker")], [window("BTC", w, "YES")], [])
    check("circuit-breaker-only window is not opportunity", not rows and coverage["early_wait_no_model"] == 1)

    rows, _ = build_opportunities([decision("BTC", w, 0.6, 300)], [window("BTC", w, "NO")], [])
    check("YES NO outcome orientation", rows[0]["yes_settled"] == 0 and rows[0]["actual_outcome"] == "NO")
    rows, _ = build_opportunities([decision("BTC", w, 0.6, 300)], [window("BTC", w, "YES")], [])
    check("YES outcome sets yes_settled", rows[0]["yes_settled"] == 1)
    rows, _ = build_opportunities([decision("BTC", w, 0.6, 300, action="WAIT")], [window("BTC", w, "YES")], [{"asset": "BTC", "window_id_ts": w, "side": "yes"}])
    check("executed population uses window trade not snapshot action", rows[0]["population"] == "executed")

    rows, _ = build_opportunities([decision("BTC", w, 0.6, 300), decision("ETH", w, 0.4, 300)], [window("BTC", w, "YES"), window("ETH", w, "NO")], [])
    check("assets sharing time bucket stay separate", len(rows) == 2)
    rows, coverage = build_opportunities([decision("BTC", w, 0.6, 300)], [window("BTC", w, "YES"), window("BTC", w, "NO")], [])
    check("conflicting outcomes are skipped", not rows and coverage["conflicting_outcomes"] == 1)
    rows, coverage = build_opportunities([decision("BTC", w, 0.6, 300)], [window("BTC", w, "YES"), window("BTC", w, "YES")], [])
    check("same-label windows are deduplicated", len(rows) == 1 and coverage["duplicate_windows"] == 1)
    rows, coverage = build_opportunities([decision("BTC", w, 0.6, 300)], [window("BTC", w, "YES")], [{"asset": "BTC", "side": "yes"}])
    check("trade missing window id is counted", coverage["trades_missing_window_id_ts"] == 1 and rows[0]["population"] == "model_wait")
    rows, coverage = build_opportunities([decision("BTC", w, 0.6, 300)], [window("BTC", w, "YES")], [])
    check("missing historical optional fields do not crash", rows[0].get("raw_features") is None)
    check("known Brier and log loss", abs(brier(0.8, 1) - 0.04) < 1e-12 and abs(log_loss(0.5, 1) - 0.6931471805599453) < 1e-12)
    check("derived output path confined to reports", report_path(Path("reports") / "fixture") and _rejects_outside_reports())
    check("report states the window-level dependence warning", "Statistical N is unique windows, not ticks." in render_report(rows, coverage, "fixture"))
    report_rows, report_coverage = build_opportunities(
        [decision("BTC", w, 0.8, 90, p_base=0.8), decision("BTC", w + 1, 0.1, 30, p_base=0.1)],
        [window("BTC", w, "YES"), window("BTC", w + 1, "NO")], [],
    )
    report = render_report(report_rows, report_coverage, "fixture")
    headline_start = report.index("HEADLINE — preferred_ge_60s")
    secondary_start = report.index("SECONDARY — fallback_lt_60s")
    check("headline Brier is preferred-only and fallback is labeled", headline_start < report.index("p_base: N=1 Brier=0.0400", headline_start) < secondary_start and "pooled (not headline)" in report)
    print("17/17 opportunity fixture checks passed")


def _rejects_outside_reports() -> bool:
    try:
        report_path(Path("outside_reports"))
    except ValueError:
        return True
    return False


if __name__ == "__main__":
    main()
