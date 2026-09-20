"""Offline descriptive analysis of lag state on the August 2026 development sample."""
from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from kalshi_bot.research.opportunities import build_opportunities, number


DEVELOPMENT_START = datetime(2026, 8, 9, tzinfo=timezone.utc).timestamp()
DEVELOPMENT_END = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()
LAG_FIELDS = (
    "asset", "ticker", "window_id", "window_id_ts", "source_session", "snapshot_ts", "time_remaining",
    "horizon_bin", "lag_signal", "lag_confidence", "response_gap", "response_beta", "p_base", "p_real",
    "alpha_micro", "yes_price_raw", "p_market", "p_market_semantics", "dp", "yes_settled",
    "actual_outcome", "reason", "strategy", "raw_features", "raw_features_present", "lag_signal_present",
    "lag_confidence_present", "response_gap_present", "lag_direction", "lag_magnitude", "base_residual",
    "lag_residual_help", "response_gap_source", "gap_direction", "gap_magnitude", "gap_residual_help", "structural_edge", "structural_direction", "dp_direction", "forecast_market_edge",
    "forecast_market_direction", "d_brier", "d_logloss", "large_dp", "cwm_available",
)


def sign(value: Any) -> Optional[int]:
    value = number(value)
    if value is None:
        return None
    return 1 if value > 0 else -1 if value < 0 else 0


def mean(values: Iterable[Optional[float]]) -> Optional[float]:
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def log_loss(probability: float, outcome: int) -> float:
    probability = min(1 - 1e-12, max(1e-12, probability))
    return -(math.log(probability) if outcome else math.log(1 - probability))


def _horizon(value: Any) -> Optional[str]:
    value = number(value)
    if value is None or value < 60:
        return None
    return "60-119s" if value < 120 else ">=120s"


def _same(left: Any, right: Any) -> bool:
    left, right = number(left), number(right)
    if left is None or right is None:
        return left is None and right is None
    return left is not None and right is not None and abs(left - right) <= 1e-9


def build_lag_dataset(sessions: Iterable[dict[str, Any]]) -> tuple[list[dict], dict]:
    """Reuse preferred opportunity selection, then globally deduplicate August rows."""
    kept: dict[tuple[str, int], dict] = {}
    conflicts: set[tuple[str, int]] = set()
    duplicates = 0
    sessions_read = []
    for session in sorted(sessions, key=lambda item: str(item["name"])):
        name = str(session["name"]); sessions_read.append(name)
        opportunities, _ = build_opportunities(session.get("decisions", []), session.get("windows", []), session.get("trades", []))
        for source in opportunities:
            window = number(source.get("window_id_ts"))
            if source.get("snapshot_selection") != "preferred_ge_60s" or window is None or not (DEVELOPMENT_START <= window < DEVELOPMENT_END):
                continue
            key = (str(source["asset"]), int(window))
            row = _row(source, name)
            if key in conflicts:
                continue
            previous = kept.get(key)
            if previous is None:
                kept[key] = row
            elif previous["actual_outcome"] == row["actual_outcome"] and _same(previous.get("p_base"), row.get("p_base")) and _same(previous.get("p_real"), row.get("p_real")):
                duplicates += 1
            else:
                kept.pop(key, None); conflicts.add(key)
    rows = sorted(kept.values(), key=lambda row: (row["window_id_ts"], row["asset"]))
    return rows, {"sessions_read": sessions_read, "duplicate_keys": duplicates, "conflicting_keys": len(conflicts)}


def _row(source: dict, session: str) -> dict:
    base, real, market, lag = number(source.get("p_base")), number(source.get("p_real")), number(source.get("p_market")), number(source.get("lag_signal"))
    top_level_gap = number(source.get("response_gap"))
    nested_gap = number(source.get("raw_features", {}).get("response_gap")) if isinstance(source.get("raw_features"), dict) else None
    gap, gap_source = (top_level_gap, "top_level") if top_level_gap is not None else (nested_gap, "raw_features") if nested_gap is not None else (None, None)
    outcome = int(source["yes_settled"])
    dp = real - base if real is not None and base is not None else None
    structural = base - market if base is not None and market is not None else None
    row = {field: source.get(field) for field in LAG_FIELDS}
    row.update({
        "source_session": session, "window_id_ts": int(source["window_id_ts"]), "horizon_bin": _horizon(source.get("time_remaining")),
        "p_base": base, "p_real": real, "p_market": market, "lag_signal": lag,
        "lag_signal_present": lag is not None, "lag_confidence_present": number(source.get("lag_confidence")) is not None,
        "response_gap": gap, "response_gap_source": gap_source, "response_gap_present": gap is not None, "raw_features_present": source.get("raw_features") is not None,
        "lag_direction": sign(lag), "lag_magnitude": abs(lag) if lag is not None else None,
        "base_residual": outcome - base if base is not None else None,
        "lag_residual_help": sign(lag) * (outcome - base) if lag is not None and base is not None else None,
        "gap_direction": sign(gap), "gap_magnitude": abs(gap) if gap is not None else None,
        "gap_residual_help": sign(gap) * (outcome - base) if gap is not None and base is not None else None,
        "dp": dp, "dp_direction": sign(dp), "structural_edge": structural, "structural_direction": sign(structural),
        "forecast_market_edge": real - market if real is not None and market is not None else None,
        "forecast_market_direction": sign(real - market) if real is not None and market is not None else None,
        "d_brier": ((real - outcome) ** 2 - (base - outcome) ** 2) if dp is not None else None,
        "d_logloss": (log_loss(real, outcome) - log_loss(base, outcome)) if dp is not None else None,
        "large_dp": abs(dp) >= .10 if dp is not None else None,
        "cwm_available": False,
    })
    return row


def clustered_ci(rows: Iterable[dict], metric: str, iterations: int = 500, seed: int = 0) -> tuple[Optional[float], Optional[float]]:
    clusters: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        value = number(row.get(metric))
        if value is not None:
            clusters[int(row["window_id_ts"])].append(value)
    groups = list(clusters.values())
    if not groups:
        return None, None
    rng, samples = random.Random(seed), []
    for _ in range(iterations):
        values = [value for _ in groups for value in rng.choice(groups)]
        samples.append(sum(values) / len(values))
    samples.sort()
    return samples[int(.025 * (len(samples) - 1))], samples[int(.975 * (len(samples) - 1))]


def summary(rows: Iterable[dict], metric: str) -> dict:
    values = [number(row.get(metric)) for row in rows if number(row.get(metric)) is not None]
    return {"n": len(values), "mean": mean(values), "fraction_positive": mean(float(value > 0) for value in values), "ci": clustered_ci(rows, metric)}


def directional_help_summary(rows: Iterable[dict], direction: str, metric: str) -> dict:
    complete = [row for row in rows if row.get(metric) is not None and row.get(direction) is not None]
    nonzero = [row for row in complete if row[direction] != 0]
    values = [row[metric] for row in complete]
    nonzero_values = [row[metric] for row in nonzero]
    return {
        "n": len(complete), "n_nonzero": len(nonzero), "n_zero": len(complete) - len(nonzero),
        "mean": mean(values), "fraction_positive_all": mean(float(value > 0) for value in values),
        "fraction_positive_nonzero": mean(float(value > 0) for value in nonzero_values),
        "ci": clustered_ci(complete, metric),
    }


def quantile_groups(rows: Iterable[dict], field: str, labels=("low", "mid", "high")) -> dict[str, list[dict]]:
    valid = [row for row in rows if number(row.get(field)) is not None]
    if not valid:
        return {}
    cuts = sorted(number(row[field]) for row in valid)
    low, high = cuts[(len(cuts) - 1) // 3], cuts[2 * (len(cuts) - 1) // 3]
    groups = {label: [] for label in labels}
    for row in valid:
        value = number(row[field])
        groups[labels[0] if value <= low else labels[1] if value <= high else labels[2]].append(row)
    return groups


def concordance(rows: Iterable[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        directions = (row.get("lag_direction"), row.get("structural_direction"), row.get("dp_direction"))
        if any(value is None for value in directions):
            continue
        label = f"lag={directions[0]} structural={directions[1]} dp={directions[2]}"
        groups[label].append(row)
    return {label: group for label, group in sorted(groups.items())}


def _fmt(value: Optional[float], digits: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def render_report(rows: list[dict], coverage: dict, source: str) -> str:
    matched = [row for row in rows if row["d_brier"] is not None]
    residual_lag = [row for row in rows if row["lag_residual_help"] is not None]
    residual_gap = [row for row in rows if row["gap_residual_help"] is not None]
    dates = [datetime.fromtimestamp(row["window_id_ts"], tz=timezone.utc).date().isoformat() for row in rows]
    lines = [
        "LAG MECHANISM — AUGUST 9–31 2026 DEVELOPMENT SAMPLE", f"Source: {source}",
        "Descriptive/exploratory only: no causal or tradability claim, no threshold recommendation, and no hypothetical profitability.", "",
        "1. DATASET / COVERAGE",
        f"sessions read={len(coverage['sessions_read'])} preferred unique N={len(rows)} duplicates={coverage['duplicate_keys']} conflicts excluded={coverage['conflicting_keys']}",
        f"UTC range={min(dates) if dates else 'n/a'} to {max(dates) if dates else 'n/a'} | YES rate={_fmt(mean(float(row['yes_settled']) for row in rows))}",
        "assets: " + ", ".join(f"{asset}={count}" for asset, count in sorted(Counter(row['asset'] for row in rows).items())),
        f"horizons: 60-119s={sum(row['horizon_bin'] == '60-119s' for row in rows)} >=120s={sum(row['horizon_bin'] == '>=120s' for row in rows)}",
        f"coverage: p_base={sum(row['p_base'] is not None for row in rows)} p_real={sum(row['p_real'] is not None for row in rows)} matched={len(matched)} lag_signal={sum(row['lag_signal_present'] for row in rows)} lag_confidence={sum(row['lag_confidence_present'] for row in rows)} response_gap={sum(row['response_gap_present'] for row in rows)} raw_features={sum(row['raw_features_present'] for row in rows)}", "",
        "2. INFORMATION BEYOND p_base",
        "base_residual = yes_settled - p_base. lag_signal is neither lag seconds nor a z-score.",
    ]
    for label, group, direction, metric in (("lag direction residual help", residual_lag, "lag_direction", "lag_residual_help"), ("response_gap directional residual help", residual_gap, "gap_direction", "gap_residual_help")):
        stats = directional_help_summary(group, direction, metric)
        lines.append(f"{label}: N={stats['n']} directional_nonzero={stats['n_nonzero']} zero_direction={stats['n_zero']} mean={_fmt(stats['mean'])} fraction>0 all={_fmt(stats['fraction_positive_all'])} nonzero={_fmt(stats['fraction_positive_nonzero'])} clustered 95% CI={stats['ci']}")
    zero = sum(row.get("lag_direction") == 0 for row in rows)
    lines.append(f"lag direction states: positive={sum(row.get('lag_direction') == 1 for row in rows)} negative={sum(row.get('lag_direction') == -1 for row in rows)} zero={zero} missing={sum(row.get('lag_direction') is None for row in rows)}")
    lines.append(f"response_gap direction states: positive={sum(row.get('gap_direction') == 1 for row in rows)} negative={sum(row.get('gap_direction') == -1 for row in rows)} zero={sum(row.get('gap_direction') == 0 for row in rows)} missing={sum(row.get('gap_direction') is None for row in rows)}")
    for label, group in quantile_groups(residual_lag, "lag_magnitude").items():
        stats = summary(group, "lag_residual_help"); lines.append(f"lag magnitude {label}: N={stats['n']} residual-help mean={_fmt(stats['mean'])} fraction>0={_fmt(stats['fraction_positive'])}")
    for field in ("response_gap", "lag_confidence"):
        for label, group in quantile_groups([row for row in rows if row["base_residual"] is not None], field).items():
            lines.append(f"{field} {label}: N={len(group)} base_residual mean={_fmt(mean(row['base_residual'] for row in group))} absolute residual mean={_fmt(mean(abs(row['base_residual']) for row in group))}")
    lines.extend(["", "3. p_real OVERLAY / SCALING", "Matched p_base/p_real rows only; negative dBrier favors p_real. Mixed evidence is not forced into H_INFO or H_SCALE."])
    for field in ("lag_direction", "lag_magnitude", "lag_confidence", "response_gap"):
        groups = ({str(key): [row for row in matched if row.get(field) == key] for key in (-1, 0, 1)} if field == "lag_direction" else quantile_groups(matched, field))
        for label, group in groups.items():
            stats = summary(group, "d_brier"); lines.append(f"dBrier by {field}={label}: N={stats['n']} mean={_fmt(stats['mean'])} CI={stats['ci']}")
    for label, group in quantile_groups(matched, "lag_magnitude").items():
        dp_help = [row["dp_direction"] * row["base_residual"] for row in group if row["dp_direction"] is not None and row["base_residual"] is not None]
        lines.append(f"dp directional residual help by lag magnitude {label}: N={len(dp_help)} mean={_fmt(mean(dp_help))} fraction>0={_fmt(mean(float(value > 0) for value in dp_help))}")
    lines.extend(["", "4. DIRECTIONAL CONCORDANCE", "structural_edge = p_base - p_market when both are present; this is not fabricated CWM. p_market semantics remain cautious."])
    for label, group in concordance(matched).items():
        lines.append(f"{label}: N={len(group)} base_residual={_fmt(mean(row['base_residual'] for row in group))} dBrier={_fmt(mean(row['d_brier'] for row in group))} YES={_fmt(mean(float(row['yes_settled']) for row in group))}")
    lines.extend(["", "5. LARGE-ADJUSTMENT DIAGNOSTICS", "large dp means |p_real-p_base| >= 0.10; no coefficient or threshold recommendation follows."])
    for label, group in (("large", [row for row in matched if row["large_dp"]]), ("small", [row for row in matched if not row["large_dp"]])):
        lines.append(f"{label} dp: N={len(group)} |lag_signal|={_fmt(mean(row['lag_magnitude'] for row in group))} |response_gap|={_fmt(mean(abs(number(row['response_gap'])) if number(row.get('response_gap')) is not None else None for row in group))} lag_confidence={_fmt(mean(number(row.get('lag_confidence')) for row in group))} dBrier={_fmt(mean(row['d_brier'] for row in group))}")
    strong = quantile_groups(matched, "lag_magnitude").get("high", [])
    lines.append(f"strongest lag-magnitude quantile: N={len(strong)} large-dp fraction={_fmt(mean(float(row['large_dp']) for row in strong))}")
    lag_groups = quantile_groups(matched, "lag_magnitude")
    for lag_label in ("low", "high"):
        for dp_label, group in (("small_dp", [row for row in lag_groups.get(lag_label, []) if not row["large_dp"]]), ("large_dp", [row for row in lag_groups.get(lag_label, []) if row["large_dp"]])):
            lines.append(f"dBrier lag-magnitude={lag_label} {dp_label}: N={len(group)} mean={_fmt(mean(row['d_brier'] for row in group))}")
    lines.extend(["", "6. ASSET / HORIZON CONDITIONAL DESCRIPTIVES"])
    for label, group in list((f"asset {asset}", [row for row in matched if row['asset'] == asset]) for asset in sorted({row['asset'] for row in matched})) + [(f"horizon {h}", [row for row in matched if row['horizon_bin'] == h]) for h in ("60-119s", ">=120s")]:
        lines.append(f"{label}: N={len(group)} dBrier={_fmt(mean(row['d_brier'] for row in group))} residual={_fmt(mean(row['base_residual'] for row in group))}")
    lines.extend(["", "7. OPTIONAL REPRICING APPENDIX", "Skipped: rotated archived ticks are not a reliable complete post-snapshot path.", "8. OPTIONAL ECONOMIC-CAPTURE APPENDIX", "Skipped: August execution tagging is unreliable and no separate valid C7 dataset was supplied.", "9. LIMITATIONS", "No fitted nested M0/M1 CV diagnostic was implemented: residual-based, cluster-aware diagnostics answer the mandatory incremental-information question without adding a development-sample fit claim.", "Outcome is existing spot_vs_price_to_beat, not official Kalshi settlement. Current Phase 2, research_store, and prospective C7 observations are excluded from this headline sample."])
    return "\n".join(lines) + "\n"
