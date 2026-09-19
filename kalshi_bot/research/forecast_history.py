"""Offline, descriptive historical scoring for preferred decision opportunities."""

from __future__ import annotations

import math
import random
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from kalshi_bot.research.opportunities import build_opportunities, number, reliability


FORECAST_HISTORY_FIELDS = (
    "asset", "ticker", "window_id", "window_id_ts", "snapshot_ts", "time_remaining", "horizon_bin",
    "source_session", "p_base", "p_real", "alpha_micro", "yes_price_raw", "p_market",
    "p_market_semantics", "actual_outcome", "yes_settled", "reason", "strategy",
    "raw_features_present", "lag_signal", "executed",
)


def log_loss(probability: float, outcome: int) -> float:
    p = min(1.0 - 1e-12, max(1e-12, probability))
    return -(math.log(p) if outcome else math.log(1.0 - p))


def mean(values: Iterable[float]) -> Optional[float]:
    values = list(values)
    return sum(values) / len(values) if values else None


def quantiles(values: Iterable[float]) -> dict[str, Optional[float]]:
    data = sorted(values)
    if not data:
        return {label: None for label in ("min", "p10", "p25", "p50", "p75", "p90", "max")}

    def at(fraction: float) -> float:
        return data[round((len(data) - 1) * fraction)]

    return {"min": data[0], "p10": at(.10), "p25": at(.25), "p50": at(.50), "p75": at(.75), "p90": at(.90), "max": data[-1]}


def horizon_bin(value: Any) -> Optional[str]:
    seconds = number(value)
    if seconds is None or seconds < 60:
        return None
    return "60-119s" if seconds < 120 else ">=120s"


def _session_name(session: Any) -> str:
    return str(getattr(session, "name", session))


def build_forecast_history(sessions: Iterable[dict[str, Any]]) -> tuple[list[dict], dict]:
    """Build globally deduplicated preferred snapshots from pre-loaded session JSONL rows."""
    candidates: dict[tuple[str, int], dict] = {}
    conflicts: set[tuple[str, int]] = set()
    duplicate_keys = 0
    totals = Counter()
    sessions_read = []
    for session in sorted(sessions, key=lambda item: _session_name(item["name"])):
        session_name = _session_name(session["name"])
        sessions_read.append(session_name)
        opportunities, coverage = build_opportunities(session.get("decisions", []), session.get("windows", []), session.get("trades", []))
        totals["trades_missing_window_id_ts"] += coverage["trades_missing_window_id_ts"]
        for opportunity in opportunities:
            if opportunity["snapshot_selection"] != "preferred_ge_60s":
                continue
            key = (str(opportunity["asset"]), int(opportunity["window_id_ts"]))
            row = {field: opportunity.get(field) for field in FORECAST_HISTORY_FIELDS}
            row.update({
                "source_session": session_name,
                "horizon_bin": horizon_bin(opportunity.get("time_remaining")),
                "raw_features_present": opportunity.get("raw_features") is not None,
                "executed": opportunity.get("population") == "executed",
            })
            if key in conflicts:
                continue
            prior = candidates.get(key)
            if prior is None:
                candidates[key] = row
                continue
            same = (
                prior["actual_outcome"] == row["actual_outcome"]
                and _same_number(prior.get("p_real"), row.get("p_real"))
                and _same_number(prior.get("p_base"), row.get("p_base"))
            )
            if same:
                duplicate_keys += 1
            else:
                conflicts.add(key)
                candidates.pop(key, None)
    rows = list(candidates.values())
    rows.sort(key=lambda row: (row["window_id_ts"], row["asset"], row["source_session"]))
    return rows, {
        "sessions_read": sessions_read,
        "duplicate_keys": duplicate_keys,
        "conflicting_keys": len(conflicts),
        "trades_missing_window_id_ts": totals["trades_missing_window_id_ts"],
    }


def _same_number(left: Any, right: Any) -> bool:
    left_number, right_number = number(left), number(right)
    if left_number is None or right_number is None:
        return left_number is None and right_number is None
    return left_number is not None and right_number is not None and abs(left_number - right_number) <= 1e-9


def matched_rows(rows: Iterable[dict]) -> list[dict]:
    return [row for row in rows if number(row.get("p_base")) is not None and number(row.get("p_real")) is not None]


def paired_metrics(rows: Iterable[dict]) -> dict:
    paired = matched_rows(rows)
    base_briers, real_briers, base_loglosses, real_loglosses = [], [], [], []
    brier_delta, logloss_delta = [], []
    for row in paired:
        y, base, real = int(row["yes_settled"]), number(row["p_base"]), number(row["p_real"])
        base_brier, real_brier = (base - y) ** 2, (real - y) ** 2
        base_logloss, real_logloss = log_loss(base, y), log_loss(real, y)
        base_briers.append(base_brier)
        real_briers.append(real_brier)
        base_loglosses.append(base_logloss)
        real_loglosses.append(real_logloss)
        brier_delta.append(real_brier - base_brier)
        logloss_delta.append(real_logloss - base_logloss)
    return {
        "n_matched": len(paired),
        "brier_base": mean(base_briers), "brier_real": mean(real_briers),
        "logloss_base": mean(base_loglosses), "logloss_real": mean(real_loglosses),
        "mean_d_brier": mean(brier_delta), "mean_d_logloss": mean(logloss_delta),
        "fraction_d_brier_improved": mean(float(value < 0) for value in brier_delta),
        "fraction_d_logloss_improved": mean(float(value < 0) for value in logloss_delta),
    }


def directional_accuracy(rows: Iterable[dict]) -> Optional[float]:
    rows = list(rows)
    if not rows:
        return None
    return mean(
        float((number(row["p_real"]) - number(row["p_base"]) > 0) if row["yes_settled"] else (number(row["p_real"]) - number(row["p_base"]) < 0))
        for row in rows
    )


def clustered_bootstrap(rows: Iterable[dict], iterations: int = 1000, seed: int = 0) -> dict:
    rows = matched_rows(rows)
    clusters: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        clusters[int(row["window_id_ts"])].append(row)
    cluster_rows = list(clusters.values())
    if not cluster_rows:
        return {"n_rows": 0, "n_clusters": 0, "mean_d_brier_ci": (None, None), "mean_d_logloss_ci": (None, None), "directional_help_ci": (None, None)}
    rng = random.Random(seed)
    briers, losses, directions = [], [], []
    for _ in range(iterations):
        sample = [row for _ in range(len(cluster_rows)) for row in rng.choice(cluster_rows)]
        metrics = paired_metrics(sample)
        briers.append(metrics["mean_d_brier"])
        losses.append(metrics["mean_d_logloss"])
        directions.append(directional_accuracy(sample))

    def interval(values: list[float]) -> tuple[float, float]:
        values.sort()
        return values[int(.025 * (len(values) - 1))], values[int(.975 * (len(values) - 1))]

    return {"n_rows": len(rows), "n_clusters": len(cluster_rows), "mean_d_brier_ci": interval(briers), "mean_d_logloss_ci": interval(losses), "directional_help_ci": interval(directions)}


def calibration_diagnostics(rows: Iterable[dict], field: str) -> dict:
    valid = [row for row in rows if number(row.get(field)) is not None]
    probabilities = [number(row[field]) for row in valid]
    outcomes = [float(row["yes_settled"]) for row in valid]
    buckets = reliability(valid, field)
    for bucket in buckets:
        bucket["calibration_error"] = bucket["mean_p"] - bucket["observed_yes"]
    return {
        "n": len(valid), "mean_predicted_yes": mean(probabilities), "observed_yes_frequency": mean(outcomes),
        "calibration_in_the_large": (mean(probabilities) - mean(outcomes)) if valid else None,
        "quantiles": quantiles(probabilities), "buckets": buckets,
    }


def alpha_diagnostics(rows: Iterable[dict]) -> dict:
    selected = [row for row in matched_rows(rows) if number(row.get("alpha_micro")) is not None]
    directional = []
    for row in selected:
        dp = number(row["p_real"]) - number(row["p_base"])
        directional.append(float(dp > 0 if row["yes_settled"] else dp < 0))

    def buckets(metric: str, values: list[float]) -> list[dict]:
        limits = ((0, .02), (.02, .05), (.05, .10), (.10, math.inf))
        result = []
        for low, high in limits:
            group = [row for row, value in zip(selected, values) if low <= value < high]
            result.append({"bucket": f"[{low:.2f}, {'inf' if math.isinf(high) else f'{high:.2f}'})", "n": len(group), "mean_d_brier": paired_metrics(group)["mean_d_brier"]})
        return result

    alpha_values = [abs(number(row["alpha_micro"])) for row in selected]
    dp_values = [abs(number(row["p_real"]) - number(row["p_base"])) for row in selected]
    return {"n": len(selected), "directional_accuracy": mean(directional), "abs_alpha_micro": buckets("alpha_micro", alpha_values), "abs_dp": buckets("dp", dp_values), "raw_features_present": sum(bool(row.get("raw_features_present")) for row in selected), "lag_signal_present": sum(row.get("lag_signal") is not None for row in selected)}


def _format(value: Optional[float], digits: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _utc_date(value: Any) -> Optional[str]:
    seconds = number(value)
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()


def render_report(rows: list[dict], coverage: dict, source: str) -> str:
    matched = matched_rows(rows)
    metrics, bootstrap = paired_metrics(matched), clustered_bootstrap(matched)
    dates = sorted(date for date in (_utc_date(row["window_id_ts"]) for row in rows) if date)
    lines = [
        "FORECAST HISTORY — DEVELOPMENT SAMPLE (August 2026 archives; not prospective validation)",
        f"Source: {source}",
        "Descriptive, not causal: this is forecast research only and contains no hypothetical fills, P&L, fees, slippage, or sizing.", "",
        "DATASET INTEGRITY",
        f"sessions read: {len(coverage['sessions_read'])} | unique preferred opportunities: {len(rows)} | duplicate keys: {coverage['duplicate_keys']} | conflicting keys excluded: {coverage['conflicting_keys']}",
        f"horizons: 60-119s={sum(row['horizon_bin'] == '60-119s' for row in rows)} >=120s={sum(row['horizon_bin'] == '>=120s' for row in rows)} | YES rate={_format(mean(float(row['yes_settled']) for row in rows))}",
        "assets: " + ", ".join(f"{asset}={count}" for asset, count in sorted(Counter(row['asset'] for row in rows).items())),
        f"UTC date range: {dates[0] if dates else 'n/a'} to {dates[-1] if dates else 'n/a'}",
        f"trades missing window_id_ts: {coverage['trades_missing_window_id_ts']}; executed tagging is UNRELIABLE when this is nonzero and is not headlined.", "",
        "HEADLINE MATCHED NESTED COMPARISON — identical p_base/p_real observations only",
        f"p_base: N={metrics['n_matched']} Brier={_format(metrics['brier_base'])} log-loss={_format(metrics['logloss_base'])}",
        f"p_real: N={metrics['n_matched']} Brier={_format(metrics['brier_real'])} log-loss={_format(metrics['logloss_real'])}",
        f"N_matched={metrics['n_matched']} mean dBrier (real-base)={_format(metrics['mean_d_brier'])} mean dLogLoss (real-base)={_format(metrics['mean_d_logloss'])}",
        f"fraction dBrier<0={_format(metrics['fraction_d_brier_improved'])} | fraction dLogLoss<0={_format(metrics['fraction_d_logloss_improved'])}; negative delta favors p_real.",
        f"cluster bootstrap: n_rows={bootstrap['n_rows']} n_clusters={bootstrap['n_clusters']} dBrier 95% CI={bootstrap['mean_d_brier_ci']} dLogLoss 95% CI={bootstrap['mean_d_logloss_ci']} directional help (dp sign) 95% CI={bootstrap['directional_help_ci']}", "",
        "RELIABILITY / CALIBRATION DIAGNOSTICS — matched sample",
    ]
    for field in ("p_base", "p_real"):
        diagnostic = calibration_diagnostics(matched, field)
        lines.append(f"{field}: N={diagnostic['n']} mean predicted YES={_format(diagnostic['mean_predicted_yes'])} observed YES={_format(diagnostic['observed_yes_frequency'])} calibration-in-the-large={_format(diagnostic['calibration_in_the_large'])} quantiles={diagnostic['quantiles']}")
        lines.extend(f"  {bucket['bucket']} N={bucket['n']} calibration error={_format(bucket['calibration_error'])}" for bucket in diagnostic["buckets"])
    raw_market = calibration_diagnostics(matched, "yes_price_raw")
    lines.append(f"CAUTIOUS yes_price_raw (not 'the market'): N={raw_market['n']} diagnostics={raw_market}; WAIT values may be raw YES and historically polarized.")
    lines.extend(["Brier captures calibration and discrimination/resolution; lower Brier is not, by itself, proof of improved calibration.", "", "HORIZON / ASSET / UTC DATE SPLITS — descriptive; small-N cells must not be over-interpreted"])
    for label, group in _split_groups(matched).items():
        subgroup = paired_metrics(group)
        lines.append(f"{label}: N={subgroup['n_matched']} mean dBrier={_format(subgroup['mean_d_brier'])} mean dLogLoss={_format(subgroup['mean_d_logloss'])}")
    alpha = alpha_diagnostics(matched)
    lines.extend(["", "ALPHA — logit units, not probability delta", "p_real = sigmoid(logit(p_base) + alpha_micro); dp = p_real - p_base.", f"N={alpha['n']} directional help={_format(alpha['directional_accuracy'])} | raw_features present={alpha['raw_features_present']} | lag_signal present={alpha['lag_signal_present']}"])
    for label in ("abs_alpha_micro", "abs_dp"):
        lines.append(label + ": " + ", ".join(f"{bucket['bucket']} N={bucket['n']} dBrier={_format(bucket['mean_d_brier'])}" for bucket in alpha[label]))
    return "\n".join(lines) + "\n"


def _split_groups(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for bin_name in ("60-119s", ">=120s"):
        groups[f"horizon {bin_name}"] = [row for row in rows if row["horizon_bin"] == bin_name]
    for asset in sorted({row["asset"] for row in rows}):
        groups[f"asset {asset}"] = [row for row in rows if row["asset"] == asset]
    for date in sorted({_utc_date(row["window_id_ts"]) for row in rows if _utc_date(row["window_id_ts"])}):
        groups[f"UTC date {date}"] = [row for row in rows if _utc_date(row["window_id_ts"]) == date]
    for week in sorted({datetime.fromtimestamp(int(row["window_id_ts"]), tz=timezone.utc).strftime("%G-W%V") for row in rows}):
        groups[f"UTC week {week}"] = [row for row in rows if datetime.fromtimestamp(int(row["window_id_ts"]), tz=timezone.utc).strftime("%G-W%V") == week]
    return groups
