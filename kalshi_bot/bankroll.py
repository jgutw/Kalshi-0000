"""Operator trading-bankroll cap.

Live sizing cash is Kalshi available minus vault. If the operator sets a
trading bankroll, that amount is a ceiling on the sizing base. Unset means
the current behavior: all tradeable cash. Shadow Era 1C does not read this.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PATH = PROJECT_ROOT / "logs" / "trading_bankroll.json"

def canonical_profile(name: str) -> str:
    """One alias table: kalshi_bot.telegram.rounds.resolve_profile."""
    from kalshi_bot.telegram.rounds import resolve_profile
    return resolve_profile(name)


def trading_bankroll_cap() -> Optional[float]:
    if not PATH.exists():
        return None
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
        amount = data.get("amount")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    if amount is None:
        return None
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")) or value <= 0:
        return None
    return value


def set_trading_bankroll(amount: Optional[float]) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    if amount is None:
        PATH.write_text(json.dumps({"amount": None}), encoding="utf-8")
        return
    PATH.write_text(json.dumps({"amount": float(amount)}), encoding="utf-8")


def sizing_cash(available: float, vault: float) -> tuple[float, str]:
    """Cash the live engine may size from. Does not change Kelly."""
    tradeable = max(0.0, float(available) - max(0.0, float(vault)))
    cap = trading_bankroll_cap()
    if cap is None:
        return tradeable, "full tradeable cash"
    return min(tradeable, cap), f"capped at trading bankroll ${cap:.2f}"


