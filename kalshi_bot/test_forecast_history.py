"""Synthetic methodological tests for the offline forecast-history analysis."""
from __future__ import annotations

from pathlib import Path

from kalshi_bot.research import forecast_history
from kalshi_bot.research.forecast_history import (
    build_forecast_history, calibration_diagnostics, clustered_bootstrap, directional_accuracy, paired_metrics, render_report,
)
from scripts import analyze_forecast_history
from scripts.analyze_forecast_history import report_path


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS {name}")


def decision(asset: str, window: int, p_base: float, p_real: float, horizon=90, **extra) -> dict:
    return {"asset": asset, "window_id_ts": window, "ticker": f"KX{asset}", "p_base": p_base, "p_real": p_real, "time_remaining": horizon, **extra}


def session(name: str, decisions: list[dict], outcomes: list[tuple[str, int, str]], trades=None) -> dict:
    return {"name": name, "decisions": decisions, "windows": [{"asset": asset, "window_id_ts": window, "actual_outcome": outcome} for asset, window, outcome in outcomes], "trades": trades or []}


def main() -> None:
    w = 1722470400
    rows, coverage = build_forecast_history([session("2026-08-02", [decision("BTC", w, .4, .7, 300), decision("BTC", w, .4, .8, 90)], [("BTC", w, "YES")])])
    check("many session ticks collapse and keep last preferred", len(rows) == 1 and rows[0]["p_real"] == .8)
    rows, coverage = build_forecast_history([session("2026-08-01", [decision("BTC", w, .4, .7)], [("BTC", w, "YES")]), session("2026-08-02", [decision("BTC", w, .4, .7)], [("BTC", w, "YES")])])
    check("global identical key is unique", len(rows) == 1 and coverage["duplicate_keys"] == 1)
    rows, coverage = build_forecast_history([session("2026-08-01", [decision("BTC", w, .4, .7)], [("BTC", w, "YES")]), session("2026-08-02", [decision("BTC", w, .4, .6)], [("BTC", w, "YES")])])
    check("conflicting global key excluded", not rows and coverage["conflicting_keys"] == 1)
    rows, _ = build_forecast_history([session("2026-08-01", [decision("BTC", w, .2, .8)], [("BTC", w, "YES")])])
    metrics = paired_metrics(rows)
    check("headline scores are matched and real can improve", metrics["n_matched"] == 1 and metrics["mean_d_brier"] < 0)
    unpaired_rows, unpaired_coverage = build_forecast_history([session("2026-08-01", [decision("BTC", w, .2, .8), decision("ETH", w + 900, None, .7)], [("BTC", w, "YES"), ("ETH", w + 900, "YES")])])
    unpaired_report = render_report(unpaired_rows, unpaired_coverage, "fixture")
    check("headline excludes unpaired p_real-only scores", len(unpaired_rows) == 2 and "N_matched=1" in unpaired_report)
    direction_rows, _ = build_forecast_history([session("2026-08-01", [decision("BTC", w, .8, .6), decision("ETH", w + 900, .2, .4)], [("BTC", w, "YES"), ("ETH", w + 900, "YES")])])
    check("directional help uses dp rather than 0.5 classifier", directional_accuracy([direction_rows[0]]) == 0.0 and directional_accuracy([direction_rows[1]]) == 1.0)
    help_rows, _ = build_forecast_history([session("2026-08-01", [decision("BTC", w, .4, .6, alpha_micro=.3), decision("ETH", w + 900, .6, .4, alpha_micro=-.3)], [("BTC", w, "YES"), ("ETH", w + 900, "NO")])])
    report = render_report(help_rows, {"sessions_read": ["fixture"], "duplicate_keys": 0, "conflicting_keys": 0, "trades_missing_window_id_ts": 0}, "fixture")
    check("YES NO alpha directional help", "directional help=1.0000" in report)
    check("horizon bins retained", all(row["horizon_bin"] == "60-119s" for row in help_rows))
    clustered_rows, _ = build_forecast_history([session("2026-08-01", [decision("BTC", w, .2, .8), decision("ETH", w, .8, .2)], [("BTC", w, "YES"), ("ETH", w, "YES")])])
    bootstrap = clustered_bootstrap(clustered_rows, iterations=10)
    check("cluster bootstrap resamples shared window together", bootstrap["n_rows"] == 2 and bootstrap["n_clusters"] == 1 and bootstrap["mean_d_brier_ci"] == (0.0, 0.0))
    diagnostic = calibration_diagnostics(help_rows, "p_real")
    check("calibration in the large is mean p minus mean y", abs(diagnostic["calibration_in_the_large"] - ((.6 + .4) / 2 - .5)) < 1e-12)
    check("report says Brier is not calibration", "not, by itself, proof of improved calibration" in report)
    check("reports-only path", report_path(Path("reports") / "fixture") and _rejects_outside_reports())
    analysis_sources = Path(forecast_history.__file__).read_text(encoding="utf-8") + Path(analyze_forecast_history.__file__).read_text(encoding="utf-8")
    check("no calibration jsonl is consulted", "kalshi_calibration" not in analysis_sources)
    check("alpha label distinguishes logit units from dp", "logit units, not probability delta" in report and "alpha_micro = p_real-p_base" not in report)
    headline_start = report.index("HEADLINE MATCHED")
    headline_end = report.index("RELIABILITY / CALIBRATION")
    headline = report[headline_start:headline_end]
    check("headline prints identical Brier denominators", "p_base: N=2 Brier=" in headline and "p_real: N=2 Brier=" in headline)
    print("15/15 forecast-history fixture checks passed")


def _rejects_outside_reports() -> bool:
    try:
        report_path(Path("outside_reports"))
    except ValueError:
        return True
    return False


if __name__ == "__main__":
    main()
