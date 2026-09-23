"""Shadow research settlement. Not official Kalshi settlement.

YES settles at 1 only when the reference spot is strictly above price_to_beat.
Equality settles YES at 0, matching the paper spot-vs-threshold convention.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from .config import cfg

SETTLEMENT_METHOD = "SHADOW_RESEARCH_SETTLEMENT"
CLOSE_REASON = "window_resolution"
RESEARCH_CLOSE_KEYS = {
    "close_id",
    "position_id",
    "reason",
    "settlement_method",
    "observation_unix",
    "observation_utc",
    "window_close_ts",
    "observed_spot",
    "price_to_beat",
    "yes_settled",
    "side",
    "side_payoff",
    "quantity",
    "entry_price",
    "gross_pnl",
    "fees",
    "net_pnl",
}


def _spot(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("Positive finite spot required")
    return float(value)


def _contract_price(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Finite contract price in [0, 1] required")
    return float(value)


def yes_settled(observed_spot, price_to_beat) -> int:
    """1 only when spot is strictly above the stored strike."""
    return 1 if _spot(observed_spot) > _spot(price_to_beat) else 0


def side_payoff(side: str, settled: int) -> int:
    if settled not in (0, 1) or type(settled) is not int:
        raise ValueError("YES settlement must be 0 or 1")
    if side == "yes":
        return settled
    if side == "no":
        return 1 - settled
    raise ValueError("Explicit YES/NO side required")


def gross_pnl(quantity: int, entry_price, payoff: int) -> float:
    if type(quantity) is not int or quantity <= 0:
        raise ValueError("Positive integer quantity required")
    entry = _contract_price(entry_price)
    if payoff not in (0, 1) or type(payoff) is not int:
        raise ValueError("Bought-side payoff must be 0 or 1")
    return quantity * (payoff - entry)


def window_close_ts(window_id_ts: int) -> int:
    if type(window_id_ts) is not int or window_id_ts < 0:
        raise ValueError("Explicit nonnegative integer window_id_ts required")
    return window_id_ts + int(cfg.WINDOW_SECS)


def research_close_payload(position: dict, *, observed_spot, observation_unix) -> dict:
    """Canonical CLOSE body for one open Shadow position. Fees stay unknown."""
    request = position["request"]
    if "price_to_beat" not in position or position["price_to_beat"] is None:
        raise ValueError("Open position has no durable price_to_beat")
    if type(observation_unix) not in (int, float) or not math.isfinite(observation_unix):
        raise ValueError("Finite observation time required")
    close_ts = window_close_ts(request["window_id_ts"])
    if float(observation_unix) < close_ts:
        raise ValueError("Observation is before the window boundary")
    spot = _spot(observed_spot)
    strike = _spot(position["price_to_beat"])
    settled = yes_settled(spot, strike)
    side = request["side"]
    payoff = side_payoff(side, settled)
    quantity = position["quantity"]
    entry = position["entry_price"]
    observed = datetime.fromtimestamp(float(observation_unix), tz=timezone.utc)
    if observed.utcoffset().total_seconds() != 0:
        raise ValueError("UTC observation required")
    return {
        "close_id": "shadow-research-close-v1:" + position["position_id"],
        "position_id": position["position_id"],
        "reason": CLOSE_REASON,
        "settlement_method": SETTLEMENT_METHOD,
        "observation_unix": float(observation_unix),
        "observation_utc": observed.isoformat(),
        "window_close_ts": close_ts,
        "observed_spot": spot,
        "price_to_beat": strike,
        "yes_settled": settled,
        "side": side,
        "side_payoff": payoff,
        "quantity": quantity,
        "entry_price": _contract_price(entry),
        "gross_pnl": gross_pnl(quantity, entry, payoff),
        "fees": None,
        "net_pnl": None,
    }
