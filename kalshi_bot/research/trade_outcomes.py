"""Offline closed-trade outcome rows. Not imported by the trading loop."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable, Optional


EDGE_MODEL_FAILURE = 0.05
WIDE_SPREAD = 0.03


def side_probability(probability: Any, side: Any) -> Optional[float]:
    """YES keeps the YES probability. NO uses the complement. Unknown side stays blank."""
    value = _finite(probability)
    if value is None:
        return None
    label = str(side or "").strip().lower()
    if label in {"yes", "buy_yes"}:
        return value
    if label in {"no", "buy_no"}:
        return 1.0 - value
    return None


def outcome_from_pnl(pnl: Any) -> Optional[str]:
    value = _finite(pnl)
    if value is None:
        return None
    if value > 0:
        return "win"
    if value < 0:
        return "loss"
    return "scratch"


def build_trade_outcome(trade: dict[str, Any], *, mode: Optional[str] = None) -> dict[str, Any]:
    """One canonical row. Missing snapshot fields stay null and are listed in coverage."""
    decision = trade.get("decision") if isinstance(trade.get("decision"), dict) else {}
    side = trade.get("side") or decision.get("side")
    p_real = _first(decision, trade, "p_real")
    p_market = _first(decision, trade, "p_market")
    p_side = side_probability(p_real, side)
    q_side = side_probability(p_market, side)
    edge = None if p_side is None or q_side is None else p_side - q_side
    pnl = _finite(trade.get("pnl"))
    result = outcome_from_pnl(pnl)
    exit_price = _finite(trade.get("exit"))
    contract_won = None if exit_price is None else exit_price >= 0.5
    spread = _finite(_first(decision, trade, "kalshi_spread"))
    fees = _finite(trade.get("fees"))
    resolved_mode = _mode(trade, mode)
    labels = _labels(result, edge, contract_won, pnl, spread)
    present = {
        "decision_id": bool(str(trade.get("decision_id") or decision.get("decision_id") or "")),
        "p_real": p_real is not None,
        "p_market": p_market is not None,
        "side": p_side is not None,
        "time_remaining": _finite(_first(decision, trade, "time_remaining")) is not None,
        "realized_vol": _finite(_first(decision, trade, "realized_vol")) is not None,
        "kalshi_spread": spread is not None,
        "lag_confidence": _finite(_first(decision, trade, "lag_confidence")) is not None,
        "response_gap": _finite(_first(decision, trade, "response_gap")) is not None,
        "fees": fees is not None,
    }
    return {
        "decision_id": str(trade.get("decision_id") or decision.get("decision_id") or "") or None,
        "asset": trade.get("asset"),
        "strategy": trade.get("strategy") or decision.get("strategy"),
        "side": None if side is None else str(side).lower(),
        "mode": resolved_mode,
        "p_real": _finite(p_real),
        "p_market": _finite(p_market),
        "p_side": p_side,
        "q_side": q_side,
        "edge": edge,
        "time_remaining": _finite(_first(decision, trade, "time_remaining")),
        "realized_vol": _finite(_first(decision, trade, "realized_vol")),
        "kalshi_spread": spread,
        "lag_confidence": _finite(_first(decision, trade, "lag_confidence")),
        "response_gap": _finite(_first(decision, trade, "response_gap")),
        "entry": _finite(trade.get("entry")),
        "exit": exit_price,
        "pnl": pnl,
        "fees": fees,
        "fee_treatment": "net_of_logged_fees" if fees is not None else "unknown",
        "result": result,
        "contract_won": contract_won,
        "labels": labels,
        "coverage_missing": [name for name, ok in present.items() if not ok],
    }


def rows_from_trades(trades: Iterable[dict[str, Any]], *, mode: Optional[str] = None) -> list[dict[str, Any]]:
    return [build_trade_outcome(trade, mode=mode) for trade in trades if isinstance(trade, dict)]


def load_trade_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _labels(result: Optional[str], edge: Optional[float], contract_won: Optional[bool], pnl: Optional[float], spread: Optional[float]) -> list[str]:
    labels: list[str] = []
    if result == "loss" and contract_won is False and edge is not None and edge >= EDGE_MODEL_FAILURE:
        labels.append("model_failure")
    if contract_won is True and pnl is not None and pnl < 0:
        labels.append("execution_drag")
    if spread is not None and spread >= WIDE_SPREAD:
        labels.append("wide_spread")
    return labels


def _mode(trade: dict[str, Any], override: Optional[str]) -> str:
    if override in {"PAPER", "LIVE", "SHADOW", "UNKNOWN"}:
        return override
    live = trade.get("live")
    if live is True:
        return "LIVE"
    if live is False:
        return "PAPER"
    return "UNKNOWN"


def _first(decision: dict[str, Any], trade: dict[str, Any], key: str) -> Any:
    if key in decision and decision.get(key) is not None:
        return decision.get(key)
    return trade.get(key)


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number
