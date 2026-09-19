"""Pure offline construction and reporting for model-evaluated opportunities."""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from typing import Any, Iterable, Optional


OPPORTUNITY_FIELDS = (
    "asset", "ticker", "window_id", "window_id_ts", "snapshot_ts", "time_remaining",
    "snapshot_selection", "population", "actual_outcome", "yes_settled", "p_base",
    "p_real", "alpha_micro", "yes_price_raw", "p_market", "p_market_semantics",
    "ev", "strategy", "reason", "lag_confidence", "response_gap", "response_beta",
    "lag_signal", "raw_features", "spot_return_1s", "kalshi_prob_change_1s",
    "dislocation", "realized_vol", "regime", "is_lottery", "entry_for_size",
    "decision_ticks", "model_ticks",
)


def number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def key_for(row: dict) -> Optional[tuple[str, int]]:
    asset = row.get("asset")
    window = number(row.get("window_id_ts"))
    if not asset or window is None:
        return None
    return str(asset), int(window)


def brier(probability: Optional[float], outcome: Optional[int]) -> Optional[float]:
    if probability is None or outcome is None:
        return None
    return (probability - outcome) ** 2


def log_loss(probability: Optional[float], outcome: Optional[int]) -> Optional[float]:
    if probability is None or outcome is None:
        return None
    p = min(1.0 - 1e-12, max(1e-12, probability))
    return -(math.log(p) if outcome else math.log(1.0 - p))


def mean(values: Iterable[Optional[float]]) -> Optional[float]:
    data = [value for value in values if value is not None]
    return sum(data) / len(data) if data else None


def calibration(rows: Iterable[dict], field: str) -> dict:
    valid = [(number(row.get(field)), int(row["yes_settled"])) for row in rows if number(row.get(field)) is not None]
    return {
        "n": len(valid),
        "brier": mean(brier(p, y) for p, y in valid),
        "log_loss": mean(log_loss(p, y) for p, y in valid),
    }


def reliability(rows: Iterable[dict], field: str, buckets: int = 5) -> list[dict]:
    grouped: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for row in rows:
        p = number(row.get(field))
        if p is not None:
            grouped[min(buckets - 1, int(p * buckets))].append((p, int(row["yes_settled"])))
    return [
        {
            "bucket": f"{bucket / buckets:.1f}-{(bucket + 1) / buckets:.1f}",
            "n": len(points), "mean_p": mean(p for p, _ in points),
            "observed_yes": mean(float(y) for _, y in points),
        }
        for bucket, points in sorted(grouped.items())
    ]


def build_opportunities(decisions: Iterable[dict], windows: Iterable[dict], trades: Iterable[dict]) -> tuple[list[dict], dict]:
    """Collapse decisions to one eligible model snapshot per completed asset-window."""
    decision_rows = list(decisions)
    completed_by_outcome: dict[tuple[str, int], dict[str, dict]] = defaultdict(dict)
    duplicate_windows = 0
    for window in windows:
        key = key_for(window)
        outcome = str(window.get("actual_outcome") or "").upper()
        if key is not None and outcome in {"YES", "NO"}:
            if outcome in completed_by_outcome[key]:
                duplicate_windows += 1
            else:
                completed_by_outcome[key][outcome] = window

    conflicting_outcomes = sum(1 for outcomes in completed_by_outcome.values() if len(outcomes) > 1)
    completed = {
        key: next(iter(outcomes.values()))
        for key, outcomes in completed_by_outcome.items()
        if len(outcomes) == 1
    }

    by_window: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for decision in decision_rows:
        key = key_for(decision)
        if key in completed:
            by_window[key].append(decision)

    executed: set[tuple[str, int]] = set()
    trades_missing_window_id_ts = 0
    for trade in trades:
        if number(trade.get("window_id_ts")) is None:
            trades_missing_window_id_ts += 1
        key = key_for(trade)
        if key is not None:
            executed.add(key)
    rows: list[dict] = []
    skipped: list[dict] = []
    retained_tick_counts = []
    for key, window in completed.items():
        ticks = by_window.get(key, [])
        model_ticks = [tick for tick in ticks if number(tick.get("p_real")) is not None]
        if not model_ticks:
            last_reason = ticks[-1].get("reason") if ticks else None
            skipped.append({"key": key, "reason": last_reason or "no_decision_ticks"})
            continue
        preferred = [tick for tick in model_ticks if (number(tick.get("time_remaining")) is not None and number(tick.get("time_remaining")) >= 60.0)]
        snapshot = preferred[-1] if preferred else model_ticks[-1]
        snapshot_horizon = number(snapshot.get("time_remaining"))
        if preferred:
            selection = "preferred_ge_60s"
        elif snapshot_horizon is None:
            selection = "fallback_missing_horizon"
        else:
            selection = "fallback_lt_60s"
        outcome = str(window["actual_outcome"]).upper()
        row = {field: snapshot.get(field) for field in OPPORTUNITY_FIELDS}
        row.update({
            "asset": key[0], "window_id_ts": key[1],
            "ticker": snapshot.get("ticker") or window.get("ticker"),
            "window_id": snapshot.get("window_id") or window.get("window_id"),
            "snapshot_ts": snapshot.get("ts"),
            "time_remaining": snapshot_horizon,
            "snapshot_selection": selection,
            "population": "executed" if key in executed else "model_wait",
            "actual_outcome": outcome,
            "yes_settled": 1 if outcome == "YES" else 0,
            "p_market_semantics": "decision-row p_market; may be raw YES price on WAIT rows, unlike C7 entry p_market",
            "decision_ticks": len(ticks), "model_ticks": len(model_ticks),
        })
        rows.append(row)
        retained_tick_counts.append(len(ticks))
    coverage = {
        "decision_ticks_read": len(decision_rows),
        "completed_windows": len(completed),
        "conflicting_outcomes": conflicting_outcomes,
        "duplicate_windows": duplicate_windows,
        "unique_asset_windows": len({key_for(decision) for decision in decision_rows if key_for(decision) is not None}),
        "retained_model_opportunities": len(rows),
        "skipped_no_model": len(skipped),
        "early_wait_no_model": sum(1 for row in skipped if row["reason"] != "no_decision_ticks"),
        "no_decision_ticks": sum(1 for row in skipped if row["reason"] == "no_decision_ticks"),
        "trades_missing_window_id_ts": trades_missing_window_id_ts,
        "ticks_per_window": retained_tick_counts,
        "skipped_reasons": Counter(row["reason"] for row in skipped),
    }
    return rows, coverage


def format_number(value: Optional[float], digits: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def render_report(rows: list[dict], coverage: dict, source: str) -> str:
    tick_counts = coverage["ticks_per_window"]
    horizons = [number(row.get("time_remaining")) for row in rows if number(row.get("time_remaining")) is not None]
    horizon_bins = Counter("<60s" if value < 60 else "60-119s" if value < 120 else ">=120s" for value in horizons)
    selection = Counter(row["snapshot_selection"] for row in rows)
    populations = {name: [row for row in rows if row["population"] == name] for name in ("executed", "model_wait")}
    lines = [
        "DECISION-OPPORTUNITY RESEARCH — OFFLINE DESCRIPTIVE REPORT",
        f"Source: {source}",
        "Decision ticks were collapsed to one snapshot per asset×window. Statistical N is unique windows, not ticks.",
        "Executed means a closed trade exists on the window; features are the standardized opportunity snapshot, not C7 entry data; no WAIT receives hypothetical fill or P&L.",
        "",
        "COVERAGE",
        f"decision ticks read: {coverage['decision_ticks_read']}",
        f"completed windows with outcomes: {coverage['completed_windows']} | decision-bearing unique asset×windows: {coverage['unique_asset_windows']} | conflicting outcomes skipped: {coverage['conflicting_outcomes']} | same-label duplicate rows: {coverage['duplicate_windows']}",
        f"retained model-evaluated opportunities: {coverage['retained_model_opportunities']} | skipped no-model: {coverage['skipped_no_model']} (early_wait_no_model={coverage['early_wait_no_model']}, no_decision_ticks={coverage['no_decision_ticks']})",
        f"ticks/window: mean={format_number(mean(float(n) for n in tick_counts))} median={format_number(statistics.median(tick_counts) if tick_counts else None)}",
        "snapshot selection: " + ", ".join(f"{name}={selection.get(name, 0)}" for name in ("preferred_ge_60s", "fallback_lt_60s", "fallback_missing_horizon")),
        f"trades missing window_id_ts: {coverage['trades_missing_window_id_ts']} (those windows may appear model_wait despite a closed trade; no window_id_ts was invented)",
        "forecast horizon (seconds): mean=" + format_number(mean(horizons)) + " median=" + format_number(statistics.median(horizons) if horizons else None),
        "forecast horizon bins: " + ", ".join(f"{name}={horizon_bins.get(name, 0)}" for name in ("<60s", "60-119s", ">=120s")),
        "",
        "HEADLINE CALIBRATION — preferred_ge_60s only",
    ]
    def append_calibration_block(title: str, group: list[dict]) -> None:
        lines.append(title)
        for field in ("p_base", "p_real", "yes_price_raw"):
            summary = calibration(group, field)
            label = "yes_price_raw (raw decision YES price; separate from p_market)" if field == "yes_price_raw" else field
            lines.append(f"{label}: N={summary['n']} Brier={format_number(summary['brier'])} log-loss={format_number(summary['log_loss'])}")
            for point in reliability(group, field):
                lines.append(f"  {point['bucket']} N={point['n']} mean_p={format_number(point['mean_p'])} observed_yes={format_number(point['observed_yes'])}")

    headline = [row for row in rows if row["snapshot_selection"] == "preferred_ge_60s"]
    append_calibration_block("HEADLINE — preferred_ge_60s", headline)
    for category in ("fallback_lt_60s", "fallback_missing_horizon"):
        group = [row for row in rows if row["snapshot_selection"] == category]
        if group:
            append_calibration_block(f"SECONDARY — {category}", group)
    if rows:
        append_calibration_block("pooled (not headline)", rows)
    lines.extend([
        "p_market is retained with decision-row semantics only: WAIT-row values may differ from smoothed C7 entry p_market and are not silently substituted.",
        "",
        "INCREMENTAL MODEL — descriptive only",
        f"headline p_base N={calibration(headline, 'p_base')['n']} vs p_real N={calibration(headline, 'p_real')['n']} | alpha_micro N={sum(number(row.get('alpha_micro')) is not None for row in headline)} mean={format_number(mean(number(row.get('alpha_micro')) for row in headline))}",
        f"headline Brier difference (p_real - p_base)={format_number((calibration(headline, 'p_real')['brier'] - calibration(headline, 'p_base')['brier']) if calibration(headline, 'p_real')['brier'] is not None and calibration(headline, 'p_base')['brier'] is not None else None)}",
        "alpha_micro contains lag-derived inputs, including lag_signal and response_gap; it is not independent information attribution.",
        "",
        "POPULATION / SELECTIVITY — descriptive, not causal gate effects",
    ])
    for population, group in populations.items():
        lines.append(f"{population}: N={len(group)} YES outcome rate={format_number(mean(float(row['yes_settled']) for row in group))} mean p_real={format_number(mean(number(row.get('p_real')) for row in group))} mean p_base={format_number(mean(number(row.get('p_base')) for row in group))}")
    wait_reasons = Counter(str(row.get("reason") or "") for row in rows if row["population"] == "model_wait")
    lines.extend([
        "",
        "WAIT REASONS — selected snapshots only",
        ", ".join(f"{reason or 'missing'}={count}" for reason, count in wait_reasons.most_common()) or "none",
        "",
        "FIELD COVERAGE",
    ])
    for field in ("p_base", "p_real", "alpha_micro", "p_market", "yes_price_raw", "lag_confidence", "response_gap", "response_beta", "raw_features", "regime"):
        lines.append(f"{field}: {sum(row.get(field) is not None for row in rows)}/{len(rows)}")
    lines.extend([
        "",
        "DEPENDENCE WARNING",
        "Decision ticks were collapsed to one snapshot per asset×window. Statistical N is unique windows, not ticks.",
        "Older sessions may lack Phase 2 context; missing fields are reported as coverage gaps rather than invented.",
    ])
    return "\n".join(lines) + "\n"
