"""Synthetic methodological checks for the offline lag-mechanism analyzer."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from kalshi_bot.research import lag_mechanism
from kalshi_bot.research.lag_mechanism import build_lag_dataset, clustered_ci, concordance, directional_help_summary, render_report
from scripts import analyze_lag_mechanism
from scripts.analyze_lag_mechanism import report_path


def check(name, condition):
    if not condition: raise AssertionError(name)
    print(f"PASS {name}")


W = int(datetime(2026, 8, 10, tzinfo=timezone.utc).timestamp())

def decision(asset="BTC", window=W, base=.5, real=.6, lag=.2, remaining=90, **extra):
    return {"asset": asset, "window_id_ts": window, "ticker": f"KX{asset}", "p_base": base, "p_real": real, "lag_signal": lag, "time_remaining": remaining, "p_market": .45, "lag_confidence": .7, "response_gap": .02, **extra}


def session(name, decisions, outcomes):
    return {"name": name, "decisions": decisions, "windows": [{"asset": asset, "window_id_ts": window, "actual_outcome": outcome} for asset, window, outcome in outcomes], "trades": []}


def main():
    rows, coverage = build_lag_dataset([session("2026-08-10_a", [decision(real=.55, remaining=300), decision(real=.65, remaining=90)], [("BTC", W, "YES")])])
    check("many ticks collapse to one last preferred row", len(rows) == 1 and rows[0]["p_real"] == .65)
    rows, _ = build_lag_dataset([session("2026-08-10_a", [decision(remaining=45)], [("BTC", W, "YES")])])
    check("fallback does not enter headline", not rows)
    duplicate = session("2026-08-11_b", [decision(real=.6)], [("BTC", W, "YES")])
    rows, coverage = build_lag_dataset([session("2026-08-10_a", [decision(real=.6)], [("BTC", W, "YES")]), duplicate])
    check("global identical duplicate is retained once", len(rows) == 1 and coverage["duplicate_keys"] == 1)
    rows, coverage = build_lag_dataset([session("2026-08-10_a", [decision(base=None, real=.6)], [("BTC", W, "YES")]), session("2026-08-11_b", [decision(base=None, real=.6)], [("BTC", W, "YES")])])
    check("duplicate rows with missing p_base do not falsely conflict", len(rows) == 1 and coverage["duplicate_keys"] == 1)
    rows, coverage = build_lag_dataset([session("2026-08-10_a", [decision(real=.6)], [("BTC", W, "YES")]), session("2026-08-11_b", [decision(real=.5)], [("BTC", W, "YES")])])
    check("conflicting global key excluded", not rows and coverage["conflicting_keys"] == 1)
    rows, _ = build_lag_dataset([session("2026-08-10", [decision(lag=None)], [("BTC", W, "YES")])])
    check("missing lag stays in universe but lacks lag complete case", len(rows) == 1 and not rows[0]["lag_signal_present"] and rows[0]["lag_residual_help"] is None)
    rows, _ = build_lag_dataset([session("2026-08-10", [decision(response_gap=.3, raw_features={"response_gap": -.4})], [("BTC", W, "YES")])])
    check("top level response_gap takes precedence", rows[0]["response_gap"] == .3 and rows[0]["response_gap_source"] == "top_level")
    rows, _ = build_lag_dataset([session("2026-08-10", [decision(response_gap=None, raw_features={"response_gap": .3})], [("BTC", W, "YES")])])
    check("nested response_gap is recovered", rows[0]["response_gap"] == .3 and rows[0]["response_gap_source"] == "raw_features")
    rows, _ = build_lag_dataset([session("2026-08-10", [decision(response_gap=float("nan"), raw_features={"response_gap": float("nan")})], [("BTC", W, "YES")])])
    check("missing nonfinite response_gap stays missing", rows[0]["response_gap"] is None)
    gap_rows, _ = build_lag_dataset([session("2026-08-10", [decision(base=.6, real=.7, response_gap=.2), decision("ETH", W + 900, base=.6, real=.5, response_gap=-.2), decision("SOL", W + 1800, base=.6, real=.6, response_gap=.2), decision("XRP", W + 2700, base=.6, real=.6, response_gap=0)], [("BTC", W, "YES"), ("ETH", W + 900, "NO"), ("SOL", W + 1800, "NO"), ("XRP", W + 2700, "YES")])])
    check("gap directional residual help signs and zero are explicit", gap_rows[0]["gap_residual_help"] > 0 and gap_rows[1]["gap_residual_help"] > 0 and gap_rows[2]["gap_residual_help"] < 0 and gap_rows[3]["gap_residual_help"] == 0)
    gap_stats = directional_help_summary(gap_rows, "gap_direction", "gap_residual_help")
    check("gap zero direction stays in denominator", gap_stats["n"] == 4 and gap_stats["n_nonzero"] == 3 and gap_stats["n_zero"] == 1)
    rows, _ = build_lag_dataset([session("2026-08-10", [decision(base=.6, real=.7, lag=.2), decision("ETH", W + 900, base=.6, real=.5, lag=-.2), decision("SOL", W + 1800, base=.6, real=.6, lag=0)], [("BTC", W, "YES"), ("ETH", W + 900, "NO"), ("SOL", W + 1800, "YES")])])
    check("lag direction and zero state are explicit", [row["lag_direction"] for row in rows] == [1, -1, 0])
    check("base residual and directional help orient YES NO", rows[0]["base_residual"] == .4 and rows[0]["lag_residual_help"] > 0 and rows[1]["lag_residual_help"] > 0 and rows[2]["lag_residual_help"] == 0)
    check("dBrier uses matched base and real", rows[0]["d_brier"] < 0)
    cluster_rows, _ = build_lag_dataset([session("2026-08-10", [decision("BTC", W, base=.2, real=.8), decision("ETH", W, base=.8, real=.2)], [("BTC", W, "YES"), ("ETH", W, "YES")])])
    check("shared window assets resample as one cluster", clustered_ci(cluster_rows, "d_brier", iterations=20) == (0.0, 0.0))
    con = concordance(rows)
    check("concordance separates lag structural and dp directions", any("lag=1 structural=1 dp=1" == label for label in con))
    check("large dp classification", rows[0]["large_dp"] is False and build_lag_dataset([session("2026-08-10", [decision(base=.2, real=.4)], [("BTC", W, "YES")])])[0][0]["large_dp"] is True)
    august_end = int(datetime(2026, 8, 31, 23, 45, tzinfo=timezone.utc).timestamp())
    september = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
    rows, _ = build_lag_dataset([session("date_fence", [decision(window=august_end), decision("ETH", september)], [("BTC", august_end, "YES"), ("ETH", september, "YES")])])
    check("August fence includes Aug 31 and excludes Sep 1", len(rows) == 1 and rows[0]["window_id_ts"] == august_end)
    check("missing production CWM is not fabricated", all(not row["cwm_available"] for row in rows))
    report = render_report(rows, {"sessions_read": ["fixture"], "duplicate_keys": 0, "conflicting_keys": 0}, "fixture")
    check("tick and C7 appendices are safe explicit skips", "rotated archived ticks" in report and "no separate valid C7 dataset" in report)
    check("report separates incremental information and scaling", "INFORMATION BEYOND p_base" in report and "p_real OVERLAY / SCALING" in report)
    check("reports-only output path", report_path(Path("reports") / "fixture") and _rejects_outside_reports())
    source = Path(lag_mechanism.__file__).read_text(encoding="utf-8") + Path(analyze_lag_mechanism.__file__).read_text(encoding="utf-8")
    check("no calibration log dependency or production model import", "kalshi_calibration" not in source and "MicroAlphaModel" not in source)
    check("lag report prints all and nonzero directional fractions", "fraction>0 all=" in report and "nonzero=" in report)
    print("24/24 lag-mechanism fixture checks passed")


def _rejects_outside_reports():
    try: report_path(Path("outside_reports"))
    except ValueError: return True
    return False


if __name__ == "__main__": main()
