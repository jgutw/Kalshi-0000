"""Environment choice for the Kalshi Control Room.

Navigation only. Shadow capital safety is the Shadow app's missing live
imports and its read-only report path, not a flag that hides buttons.
"""
from __future__ import annotations

SHADOW = "shadow"
LIVE = "live"
LIVE_ACK_KEY = "live_capital_acknowledged"
LIVE_ACK_TEXT = "LIVE MODE — REAL CAPITAL CAN BE AFFECTED"

# Live-control widget state. Cleared when the operator enters Shadow.
_LIVE_KEYS = {LIVE_ACK_KEY, "asset_detail_symbol"}
_LIVE_PREFIXES = ("vault_",)


def sizing_basis_label(feeds_sizing) -> str | None:
    """Label from the durable equity flag. None when the flag was withheld."""
    if feeds_sizing is True:
        return "REALIZED-GROSS-EQUITY SIZING"
    if feeds_sizing is False:
        return "FROZEN-BALANCE SIZING"
    return None


def live_controls_open(state: dict) -> bool:
    return state.get(LIVE_ACK_KEY) is True


def acknowledge_live(state: dict) -> dict:
    updated = dict(state)
    updated[LIVE_ACK_KEY] = True
    return updated


def enter_shadow(state: dict) -> dict:
    """Drop live-control session state. Shadow must not inherit it."""
    return {
        key: value
        for key, value in state.items()
        if key not in _LIVE_KEYS and not key.startswith(_LIVE_PREFIXES)
    }
