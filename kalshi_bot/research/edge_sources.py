"""Pure helpers for offline attribution of closed Kalshi trades."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any, Iterable, Optional

from kalshi_bot.signal_engine import kelly_binary


INFO_FIELDS = (
    "lag_signal", "lag_confidence", "response_gap", "response_beta",
    "spot_return_1s", "kalshi_prob_change_1s", "dislocation",
)


def as_float(value: Any) -> Optional[float]:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def attribution_generation(decision: Any) -> str:
    if not isinstance(decision, dict):
        return "pre_c7"
    if decision.get("decision_id") and isinstance(decision.get("per_venue_mids"), dict):
        return "phase2_gold"
    return "c7_thin"


def settle_kind(trade: dict, decision: dict) -> str:
    exit_price = as_float(trade.get("exit"))
    if trade.get("strategy") == "early_exit" or decision.get("strategy") == "early_exit":
        return "mtm"
    return "binary" if exit_price in (0.0, 1.0) else "mtm"


def side_orientation(side: str, exit_price: float) -> dict:
    """Return the mandatory YES/NO orientation from a closed contract payoff."""
    side_won = exit_price >= 0.5
    yes_settled = side_won if side == "yes" else not side_won
    return {"side_won": side_won, "yes_settled": yes_settled}


def brier(probability: Optional[float], outcome: Optional[bool]) -> Optional[float]:
    if probability is None or outcome is None:
        return None
    return (probability - float(outcome)) ** 2


def log_loss(probability: Optional[float], outcome: Optional[bool]) -> Optional[float]:
    if probability is None or outcome is None:
        return None
    p = min(1.0 - 1e-12, max(1e-12, probability))
    return -(math.log(p) if outcome else math.log(1.0 - p))


def flatten_trade(trade: dict, fill: Optional[dict] = None) -> Optional[dict]:
    """Flatten a closed trade with its C7 decision; no later-decision join occurs."""
    side = str(trade.get("side") or "").lower()
    entry = as_float(trade.get("entry"))
    exit_price = as_float(trade.get("exit"))
    if side not in {"yes", "no"} or entry is None or exit_price is None:
        return None

    decision = trade.get("decision") if isinstance(trade.get("decision"), dict) else {}
    row = dict(trade)
    row["trade_strategy"] = trade.get("strategy")
    row.update(decision)  # decision.strategy is authoritative for attribution.
    row["decision"] = decision
    row["decision_id"] = trade.get("decision_id") or decision.get("decision_id") or ""
    row["attribution_generation"] = attribution_generation(decision)
    row["side"] = side
    row["entry"] = entry
    row["exit"] = exit_price
    row["fees"] = as_float(trade.get("fees")) or 0.0
    row["contracts"] = int(as_float(trade.get("contracts")) or 0)
    row["amount_usdc"] = as_float(trade.get("amount_usdc")) or 0.0
    row["pnl"] = as_float(trade.get("pnl")) or 0.0
    row["settle_kind"] = settle_kind(trade, decision)
    row.update(side_orientation(side, exit_price))

    p_real = as_float(row.get("p_real"))
    p_market = as_float(row.get("p_market"))
    row["p_side"] = p_real if side == "yes" else (1.0 - p_real if p_real is not None else None)
    row["q_side"] = p_market if side == "yes" else (1.0 - p_market if p_market is not None else None)
    row["edge_yes"] = p_real - p_market if p_real is not None and p_market is not None else None
    row["edge_traded"] = row["p_side"] - row["q_side"] if row["p_side"] is not None and row["q_side"] is not None else None
    row["R"] = row["pnl"] / row["amount_usdc"] if row["amount_usdc"] > 0 else None
    row["brier_yes"] = brier(p_real, row["yes_settled"]) if row["settle_kind"] == "binary" else None
    row["brier_traded"] = brier(row["p_side"], row["side_won"]) if row["settle_kind"] == "binary" else None
    row["log_loss_yes"] = log_loss(p_real, row["yes_settled"]) if row["settle_kind"] == "binary" else None
    row["log_loss_traded"] = log_loss(row["p_side"], row["side_won"]) if row["settle_kind"] == "binary" else None

    side_ask = as_float(row.get("yes_ask" if side == "yes" else "no_ask"))
    row["side_ask"] = side_ask
    row["slippage_vs_ask"] = entry - side_ask if side_ask is not None else None
    yes_bid, yes_ask = as_float(row.get("yes_bid")), as_float(row.get("yes_ask"))
    yes_mid = (yes_bid + yes_ask) / 2 if yes_bid is not None and yes_ask is not None else None
    side_mid = yes_mid if side == "yes" else (1.0 - yes_mid if yes_mid is not None else None)
    row["yes_mid"] = yes_mid
    row["side_mid"] = side_mid
    contracts = row["contracts"]
    fee_per_contract = row["fees"] / contracts if contracts > 0 else None
    row["actual_unit_pnl"] = (exit_price - entry) - fee_per_contract if fee_per_contract is not None else None
    row["ask_unit_pnl"] = (exit_price - side_ask) - fee_per_contract if side_ask is not None and fee_per_contract is not None else None
    row["mid_unit_pnl"] = (exit_price - side_mid) - fee_per_contract if side_mid is not None and fee_per_contract is not None else None
    row["requested_vs_filled"] = (
        row["amount_usdc"] / as_float(row.get("size_usd"))
        if as_float(row.get("size_usd")) and as_float(row.get("size_usd")) > 0 else None
    )
    row["kelly_fraction"] = (
        kelly_binary(row["p_side"], row["q_side"])
        if row["p_side"] is not None and row["q_side"] is not None else None
    )
    if fill:
        row["fill_joined"] = True
        row["fill_entry"] = as_float(fill.get("entry"))
        row["fill_amount_usdc"] = as_float(fill.get("amount_usdc"))
    else:
        row["fill_joined"] = False
    return row


def join_closed_trades(trades: Iterable[dict], fills: Iterable[dict]) -> list[dict]:
    fills_by_decision = {
        str(row.get("decision_id")): row
        for row in fills if row.get("decision_id")
    }
    rows = []
    for trade in trades:
        decision = trade.get("decision") if isinstance(trade.get("decision"), dict) else {}
        decision_id = str(trade.get("decision_id") or decision.get("decision_id") or "")
        row = flatten_trade(trade, fills_by_decision.get(decision_id))
        if row is not None:
            rows.append(row)
    return rows


def mean(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def correlation(rows: Iterable[dict], field: str, outcome_field: str) -> Optional[float]:
    pairs = [(as_float(row.get(field)), float(bool(row[outcome_field]))) for row in rows if as_float(row.get(field)) is not None]
    if len(pairs) < 2:
        return None
    xs, ys = zip(*pairs)
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    denom = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return sum((x - mx) * (y - my) for x, y in pairs) / denom if denom else None


def bucket_label(value: Optional[float], cuts: tuple[float, ...]) -> str:
    if value is None:
        return "missing"
    for cut in cuts:
        if value < cut:
            return f"<{cut:g}"
    return f">={cuts[-1]:g}"


def grouped_summary(rows: Iterable[dict], key_fn) -> dict[str, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[str(key_fn(row))].append(row)
    return {
        key: {
            "n": len(group), "mean_R": mean(r.get("R") for r in group),
            "mean_pnl": mean(r.get("pnl") for r in group),
        }
        for key, group in sorted(groups.items())
    }


def reliability(rows: Iterable[dict], probability_field: str, buckets: int = 5) -> list[dict]:
    grouped: dict[int, list[tuple[float, bool]]] = defaultdict(list)
    for row in rows:
        p = as_float(row.get(probability_field))
        if row.get("settle_kind") != "binary" or p is None:
            continue
        grouped[min(buckets - 1, int(p * buckets))].append((p, bool(row["yes_settled"])))
    return [
        {"bucket": f"{idx / buckets:.1f}-{(idx + 1) / buckets:.1f}", "n": len(items),
         "mean_p": mean(p for p, _ in items), "observed_yes": mean(float(y) for _, y in items)}
        for idx, items in sorted(grouped.items())
    ]


def format_number(value: Optional[float], digits: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def render_report(rows: list[dict], source: str) -> str:
    generation = Counter(row["attribution_generation"] for row in rows)
    binary = [row for row in rows if row["settle_kind"] == "binary"]
    mtm = [row for row in rows if row["settle_kind"] == "mtm"]
    segments: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        segments[f"{row['attribution_generation']} | {'live' if row.get('live') else 'paper'}"].append(row)
    lines = [
        "FOUR SOURCES OF EDGE — OFFLINE DESCRIPTIVE REPORT",
        f"Source: {source}",
        "No post-entry decision rows were joined. C7 decision.strategy is used, not trade.strategy.",
        "All inference is descriptive only; N may be insufficient for inference.",
        "",
        "COVERAGE",
        f"closed trades with side: {len(rows)} | binary: {len(binary)} | mtm excluded from calibration: {len(mtm)}",
        "attribution_generation: " + ", ".join(f"{tag}={generation.get(tag, 0)}" for tag in ("pre_c7", "c7_thin", "phase2_gold")),
        f"mode coverage: paper={sum(not bool(r.get('live')) for r in rows)} live={sum(bool(r.get('live')) for r in rows)}",
        f"fill joins by decision_id: {sum(bool(r['fill_joined']) for r in rows)}/{len(rows)}",
    ]
    for segment_name, segment in sorted(segments.items()):
        segment_binary = [row for row in segment if row["settle_kind"] == "binary"]
        lines.extend(["", f"SEGMENT: {segment_name} (N={len(segment)}, binary={len(segment_binary)})", "INFORMATION — venues/lag only (no p_real or ev)"])
        for field in INFO_FIELDS:
            present = [r for r in segment_binary if as_float(r.get(field)) is not None]
            lines.append(f"{field}: N={len(present)} corr_yes={format_number(correlation(present, field, 'yes_settled'))} corr_side={format_number(correlation(present, field, 'side_won'))}")
        lines.append("PRICING / MODEL — binary settles only")
        for field in ("p_market", "p_base", "p_real"):
            present = [r for r in segment_binary if as_float(r.get(field)) is not None]
            lines.append(f"{field}: N={len(present)} brier={format_number(mean(r.get('brier_yes') if field == 'p_real' else brier(as_float(r.get(field)), r['yes_settled']) for r in present))} log_loss={format_number(mean(r.get('log_loss_yes') if field == 'p_real' else log_loss(as_float(r.get(field)), r['yes_settled']) for r in present))}")
            for point in reliability(present, field):
                lines.append(f"  {point['bucket']} N={point['n']} mean_p={format_number(point['mean_p'])} observed_yes={format_number(point['observed_yes'])}")
        lines.append("p_real vs p_base is not independent of information: alpha_micro includes lag_signal and response_gap.")
        executable = [r for r in segment if r.get("side_ask") is not None]
        lines.append("EXECUTION — unit contract, after decision")
        lines.append(f"quote coverage: {len(executable)}/{len(segment)} | mean slippage_vs_ask={format_number(mean(r.get('slippage_vs_ask') for r in executable))} | mean half_spread={format_number(mean((as_float(r.get('kalshi_spread')) / 2) if as_float(r.get('kalshi_spread')) is not None else None for r in executable))}")
        lines.append(f"mean unit pnl: actual={format_number(mean(r.get('actual_unit_pnl') for r in executable))} ask={format_number(mean(r.get('ask_unit_pnl') for r in executable))} mid={format_number(mean(r.get('mid_unit_pnl') for r in executable))}")
        lines.append("SIZING — realized post-execution results, not ev")
        lines.append(f"N={len(segment)} mean actual R={format_number(mean(r.get('R') for r in segment))} mean actual pnl={format_number(mean(r.get('pnl') for r in segment))} mean 1-contract pnl={format_number(mean(r.get('actual_unit_pnl') for r in segment))} mean raw Kelly fraction={format_number(mean(r.get('kelly_fraction') for r in segment))} mean requested/filled={format_number(mean(r.get('requested_vs_filled') for r in segment))}")
        lines.append("Kelly uses current-process kelly_binary settings; it is not a reconstruction of size_usd or bankroll. Production sizing also applies scalar/cap/probe controls.")
        lines.append("edge_traded buckets: " + str(grouped_summary(segment, lambda r: bucket_label(r.get("edge_traded"), (-0.05, 0.0, 0.05)))))
        lines.append("entry_for_size / lottery: " + str(grouped_summary(segment, lambda r: f"lottery={r.get('is_lottery')} entry={bucket_label(as_float(r.get('entry_for_size')), (0.15, 0.5, 0.85))}")))
    lines.extend([
        "", "OVERLAP / DOUBLE-COUNTING BOUNDARIES",
        "Information tables use lag/venue/1s fields only; they do not use p_real or ev.",
        "Pricing discloses lag inside alpha_micro. Execution is unit-contract, not size_usd.",
        "Sizing uses realized unit results, not ev. strategy=lag_arb is a selection label, not information magnitude.",
        "Residual caveats: p_market is smoothed while yes_price_raw is not; trade ts may be naive local while entry_ts is UTC; decision logs rotate; split live/paper before inference.",
    ])
    return "\n".join(lines) + "\n"
