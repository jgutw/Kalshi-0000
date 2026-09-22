"""P7B offline populations. No runtime imports, scoring, or reconstructed state."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

GROUPS = {
    "market": "spot_now spot_start price_to_beat realized_vol realized_vol_value raw_distance relative_distance log_distance dist_from_threshold yes_price_raw yes_bid yes_ask no_bid no_ask kalshi_spread spot_return_1s kalshi_prob_change_1s".split(),
    "measurement": "price_to_beat_source quote_age_secs per_venue_mids per_venue_staleness fresh_venue_count spot_confidence dislocation lead_source".split(),
    "model": "p_base p_real alpha_micro z_threshold sigma_used tau_used p_market p_market_semantics mispricing_base confidence_weighted_mispricing bias conviction belief_vol".split(),
    "estimated_response": "response_gap response_beta lag_signal lag_confidence".split(),
    "time": "time_remaining raw_tte actual_tte target_tte".split(),
    "microstructure_inputs": ["raw_features"],
}
FIELDS = sum(GROUPS.values(), [])
V1 = set("spot_now spot_start price_to_beat price_to_beat_source p_base p_real alpha_micro yes_price_raw p_market p_market_semantics lag_signal lag_confidence response_gap response_beta per_venue_mids per_venue_staleness time_remaining raw_features".split())
V2 = set(FIELDS) - set("spot_start realized_vol spot_return_1s response_gap response_beta lag_signal lag_confidence kalshi_prob_change_1s time_remaining mispricing_base confidence_weighted_mispricing bias conviction belief_vol dist_from_threshold raw_features".split())
# Fields defined by the decision logger, restricted to the research allowlist.
# This is a known schema contract, not a schema inferred from a sparse tick.
DECISION = set(FIELDS) - set("target_tte actual_tte raw_distance relative_distance log_distance realized_vol".split())
RULE_A = "last eligible supplied decision observation in manifest-file order then line order, with finite p_real and finite raw time_remaining >= 60"
RULE_V1 = "already-persisted v1 preferred row retained only after verifying finite p_real and finite raw time_remaining >= 60; does not prove last eligible live tick ever generated"
RULE_B = "schema_version == 2, record_kind == boundary_snapshot, target_tte == 60; duplicate asset/window rejected"
VARIABLE_SEMANTICS = {
    field: {"provenance": "ambiguous_fallback", "meaning": "a persisted value cannot distinguish measurement from historical fallback; preserve the observed value"}
    for field in ("realized_vol", "realized_vol_value")
}
REASONS = {"signal_warmup": "warmup", "venue_dislocation": "venue_dislocation", "spot_confidence_low": "low_confidence", "vol_too_high": "excessive_volatility", "no_price_to_beat": "missing_strike", "price_to_beat_unreliable": "unreliable_strike", "no_spot_for_structural": "missing_spot", "no_market": "missing_market", "stale_price": "stale_price", "telegram_paused": "operator_pause", "circuit_breaker": "circuit_breaker", "position_open": "position_open", "window_boundary": "window_timing", "macro_blackout": "macro_blackout", "early_exit_done": "early_exit"}
# Optional input fence. Not a population rule and not an August default.
COHORT_CLOCKS = {
    "decision": "timezone-aware ISO-8601 observation time: snapshot_ts when that key is present, otherwise ts; window_id_ts is not a substitute",
    "research_v1": "timezone-aware ISO-8601 observation time: snapshot_ts when that key is present, otherwise ts; outcome_ts and close_time_utc are not cohort clocks",
    "research_v2_boundary_snapshot": "timezone-aware ISO-8601 observation time: snapshot_ts when that key is present, otherwise ts; a non-null ts or capture_time_utc must be the same instant",
    "research_v2_window_outcome": "window_id_ts as UTC unix seconds; the outcome contract has no observation timestamp; outcome_ts and close_time_utc are not cohort clocks",
}


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def key(row):
    w = finite(row.get("window_id_ts"))
    if not isinstance(row.get("asset"), str) or not row["asset"] or w is None or not w.is_integer():
        raise ValueError("invalid asset/window identity")
    return row["asset"], int(w)


def _aware_utc(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("cohort timestamp must be a timezone-aware ISO-8601 string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("cohort timestamp must be a timezone-aware ISO-8601 string") from exc
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("naive timestamp cannot establish cohort membership")
    return stamp.astimezone(timezone.utc)


def _window_instant(value):
    parsed = finite(value)
    if parsed is None or not parsed.is_integer():
        raise ValueError("window_id_ts cannot establish cohort membership")
    try:
        return datetime.fromtimestamp(int(parsed), tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("window_id_ts cannot establish cohort membership") from exc


def cohort_bounds(manifest):
    """Return the requested half-open fence, or None when the build is unfenced."""
    start, end = manifest.get("start_utc"), manifest.get("end_utc")
    if start is None and end is None:
        return None
    if not isinstance(start, str) or not isinstance(end, str):
        raise ValueError("cohort fence requires both start_utc and end_utc")
    start_at, end_at = _aware_utc(start), _aware_utc(end)
    if start_at >= end_at:
        raise ValueError("cohort fence start must be before end")
    return start, end, start_at, end_at


def cohort_instant(kind, row):
    """Clock used only to decide cohort membership. Row timestamps are not rewritten."""
    if kind == "research_v2" and row.get("record_kind") == "window_outcome":
        if any(row.get(field) is not None for field in ("snapshot_ts", "ts", "capture_time_utc")):
            raise ValueError("window outcome cohort clock is window_id_ts; an observation timestamp makes membership ambiguous")
        return _window_instant(row.get("window_id_ts"))
    field = "snapshot_ts" if "snapshot_ts" in row else "ts"
    instant = _aware_utc(row.get(field))
    for other in ("snapshot_ts", "ts", "capture_time_utc"):
        if other == field or other not in row or row.get(other) is None:
            continue
        if _aware_utc(row.get(other)) != instant:
            raise ValueError("cohort clocks disagree")
    return instant


def safe_path(path):
    path = Path(path).resolve()
    if any(p.lower() in {"logs", "sessions", ".env"} for p in path.parts):
        raise ValueError("protected input/output path")
    return path


def build(manifest):
    """Build one explicitly declared mode/session/sample cohort; fail closed on conflicts."""
    cohort = {f: manifest[f] for f in ("mode", "session_tag", "sample_kind")}
    if cohort["mode"] not in {"paper", "live"} or cohort["sample_kind"] not in {"development", "historical", "prospective"} or not cohort["session_tag"]:
        raise ValueError("explicit valid cohort required")
    if cohort["session_tag"] == "p6c_d1_validation":
        raise ValueError("P6C-E is excluded")
    bounds = cohort_bounds(manifest)
    a, b, census, outcomes, sources = {}, {}, {}, {}, []
    fence = {"applied": bounds is not None, "start_utc": None if bounds is None else bounds[0], "end_utc": None if bounds is None else bounds[1],
             "interval": None if bounds is None else "start <= cohort_time < end", "records_before": 0, "records_after": 0, "records_excluded": 0,
             "by_source": [], "clocks": COHORT_CLOCKS}
    a_type = None
    seen_paths = set()
    for spec in manifest["sources"]:
        kind = spec["type"]
        if kind not in {"decision", "research_v1", "research_v2"}:
            raise ValueError("unknown source type")
        if kind != "research_v2":
            if a_type is not None and a_type != kind:
                raise ValueError("choose decision OR research_v1 for Population A")
            a_type = kind
        path = safe_path(spec["path"])
        if path in seen_paths:
            raise ValueError("duplicate source path")
        seen_paths.add(path)
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        sources.append({"path": str(path), "type": kind, "sha256": digest})
        source_count = {"path": str(path), "type": kind, "sha256": digest, "records_before": 0, "records_after": 0, "records_excluded": 0}
        for line_no, line in enumerate(content.decode("utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            row = json.loads(line)
            source_count["records_before"] += 1
            fence["records_before"] += 1
            if bounds is not None:
                if row.get("session_tag") == "p6c_d1_validation":
                    raise ValueError("P6C-E is excluded")
                try:
                    instant = cohort_instant(kind, row)
                except ValueError as exc:
                    raise ValueError(f"cohort timestamp cannot establish membership: {path}:{line_no}: {exc}") from exc
                if not (bounds[2] <= instant < bounds[3]):
                    source_count["records_excluded"] += 1
                    fence["records_excluded"] += 1
                    continue
            source_count["records_after"] += 1
            fence["records_after"] += 1
            k = key(row)
            if row.get("session_tag") == "p6c_d1_validation":
                raise ValueError("P6C-E is excluded")
            for f in ("mode", "session_tag", "sample_kind"):
                if row.get(f) is not None and row[f] != cohort[f]:
                    raise ValueError("mixed cohort")
            if "dry_run" in row and (not isinstance(row["dry_run"], bool) or row["dry_run"] != (cohort["mode"] == "paper")):
                raise ValueError("mode mismatch")
            if kind.startswith("research_") and row.get("schema_version") != (1 if kind == "research_v1" else 2):
                raise ValueError("schema mismatch")
            provenance = dict(cohort, source_type=kind, source_file=str(path), source_sha256=digest, source_line=line_no,
                              schema_version=row.get("schema_version"), asset=k[0], window_id_ts=k[1],
                              observation_timestamp=row.get("snapshot_ts", row.get("ts")),
                              observation_id=f"{digest}:{line_no}", decision_id=row.get("decision_id"),
                              git_sha=row.get("git_sha"), ticker=row.get("ticker"), window_id=row.get("window_id"),
                              capture_time_utc=row.get("capture_time_utc"), config_profile=row.get("regime"),
                              action=row.get("action"), strategy=row.get("strategy"), reason=row.get("reason"),
                              persisted_mode=row.get("mode"), persisted_dry_run=row.get("dry_run"),
                              persisted_session_tag=row.get("session_tag"),
                              cohort_basis="manifest declaration; persisted fields checked when present")
            if kind == "research_v1" or (kind == "research_v2" and row.get("record_kind") == "window_outcome"):
                if row.get("outcome_source") != "spot_vs_price_to_beat" or row.get("actual_outcome") not in {"YES", "NO"}:
                    raise ValueError("invalid research outcome semantics")
                outcome = {f: row.get(f) for f in ("outcome_source", "actual_outcome", "yes_settled", "exit_spot", "outcome_ts", "close_time_utc")}
                if outcome["yes_settled"] != (1 if outcome["actual_outcome"] == "YES" else 0):
                    raise ValueError("inconsistent outcome")
                ok = (kind, k)
                if ok in outcomes:
                    raise ValueError("duplicate outcome identity")
                outcomes[ok] = dict(outcome, provenance=provenance)
            if kind == "research_v2":
                if row.get("record_kind") == "window_outcome":
                    continue
                if row.get("record_kind") != "boundary_snapshot":
                    raise ValueError("unknown v2 record kind")
                if finite(row.get("target_tte")) != 60:
                    continue
                population, target, rule = "boundary_60", b, RULE_B
                if k in b:
                    raise ValueError("duplicate boundary identity")
            else:
                reason = row.get("reason")
                prefix = reason.split("(", 1)[0] if isinstance(reason, str) else None
                if kind == "decision" and finite(row.get("p_real")) is None and prefix in REASONS:
                    census[k] = dict(provenance, reason=reason, reason_category=REASONS[prefix], window_count=1)
                tte = finite(row.get("time_remaining"))
                if finite(row.get("p_real")) is None or tte is None or tte < 60:
                    if kind == "research_v1":
                        raise ValueError(f"ineligible research_v1 preferred row: {path}:{line_no}")
                    continue
                population, target, rule = "preferred_ge_60s", a, RULE_V1 if kind == "research_v1" else RULE_A
                if kind == "research_v1" and k in a:
                    raise ValueError("duplicate v1 identity")
            expected = V1 if kind == "research_v1" else V2 if kind == "research_v2" else DECISION
            features = {f: row.get(f) for f in FIELDS}
            # Preserve the known contemporaneous microstructure inputs in their
            # original namespace. Never copy arbitrary nested future/portfolio data.
            raw = row.get("raw_features")
            features["raw_features"] = ({f: raw[f] for f in ("obi", "ofi_hawkes", "microprice_dev", "trade_sign_autocorr", "lag_signal", "response_gap") if f in raw} if isinstance(raw, dict) else None)
            availability = {f: "observed" if row.get(f) is not None else "missing" if f in expected else "structurally_unavailable" for f in FIELDS}
            target[k] = dict(provenance, source_population=population, selection_rule=rule, features=features, availability=availability, outcome=None)
        fence["by_source"].append(source_count)
    for population in (a, b):
        for k, row in population.items():
            # No feature joins. A decision can use a v2 outcome only with
            # persisted session, mode, revision and ticker evidence on both sides.
            source_type = row["source_type"]
            outcome = outcomes.get(("research_v2" if source_type == "decision" else source_type, k))
            if source_type == "decision" and outcome:
                other = outcome["provenance"]
                required = ("persisted_session_tag", "git_sha", "ticker")
                mode_known = lambda p: p["persisted_mode"] is not None or p["persisted_dry_run"] is not None
                if not all(row[f] is not None and row[f] == other[f] for f in required) or not mode_known(row) or not mode_known(other):
                    outcome = None
            if outcome:
                if outcome and outcome["provenance"]["git_sha"] == row["git_sha"] and outcome["provenance"]["ticker"] == row["ticker"]:
                    row["outcome"] = outcome
    overlap = [dict(asset=k[0], window_id_ts=k[1], **cohort, membership="both" if k in a and k in b else "A_only" if k in a else "B_only") for k in sorted(a.keys() | b.keys())]
    populations = {"preferred_ge_60s": [a[k] for k in sorted(a)], "boundary_60": [b[k] for k in sorted(b)]}
    coverage = {name: {"rows": len(rows), "outcome_missing": sum(r["outcome"] is None for r in rows), "fields": {f: dict(Counter(r["availability"][f] for r in rows)) for f in FIELDS}} for name, rows in populations.items()}
    # Eligibility anywhere in the supplied stream excludes a window, including
    # early warmup and post-entry position_open records surrounding that tick.
    return dict(populations, population_overlap=overlap, abstention_census=[census[k] for k in sorted(census.keys() - a.keys())],
                schema_provenance={"builder_schema_version": 2, "cohort": cohort, "cohort_fence": fence, "sources": sources, "taxonomy": GROUPS, "selection_rules": {"A_decision": RULE_A, "A_research_v1": RULE_V1, "B": RULE_B}, "defined_fields": {"decision": sorted(DECISION), "research_v1": sorted(V1), "research_v2": sorted(V2)}, "variable_semantics": VARIABLE_SEMANTICS, "semantics": SEMANTICS}, coverage_missingness=coverage)


SEMANTICS = [
    "realized_vol and realized_vol_value: ambiguous historical fallback provenance; preserve values including 0.30; no row-level fallback flag",
    "dislocation zero with fewer than two fresh venues is not evidence of agreement",
    "missing spread stays null; no book depth, interpolation, or portfolio state",
    "raw market time, model tau_used, and target sampling time are distinct",
    "spot_return_1s is not the lag tracker input; response fields are estimates",
    "raw_features retains six named contemporaneous inputs; nested lag_signal and response_gap are estimates, not market prints; no other nested keys are exported",
    "research outcome spot_vs_price_to_beat is not official Kalshi settlement; outcome data lives outside features",
    "dist_from_threshold is price minus strike, not a model transform; kalshi_prob_change_1s is YES poll change, not a lag-tracker estimate or guaranteed one-second interval",
    "decision fields use the known logger contract; absent v2-only fields are structurally_unavailable; absent/null defined fields are missing; older stripped exports cannot establish exact schema history",
    "research_v1 preserves its stored preferred observation; cannot verify unpersisted decision history or its first-crossing freeze",
    "optional cohort fence is start <= cohort_time < end on the source clock before population construction; omitted bounds leave every supplied record eligible; row timestamps are not rewritten",
    "census includes only windows never eligible for Population A in supplied decisions; last recognized pre-model reason wins in manifest-file then line order, ignoring later unrecognized reasons",
    "no_market and stale_price returns before _log_decision are not recorded and cannot be represented by a decision-log-derived census unless independently supplied as recorded rows; no reasons are reconstructed",
]


def write_artifacts(result, out_dir):
    out = safe_path(out_dir)
    # Never overwrite source data or an earlier build.
    if out.exists():
        raise ValueError("output directory must be new")
    if "research_data" in [p.lower() for p in out.parts]:
        raise ValueError("research store is read-only")
    out.mkdir(parents=True)
    for name, value in result.items():
        if isinstance(value, list):
            (out / (name + ".jsonl")).write_text("".join(json.dumps(r, sort_keys=True, allow_nan=False) + "\n" for r in value), encoding="utf-8")
        else:
            (out / (name + ".json")).write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
