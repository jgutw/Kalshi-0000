"""
dashboard/components/metric_cards.py — Compact metric card helpers.
"""

from __future__ import annotations

import streamlit as st


def metric_card(label: str, value: str, delta: str | None = None, delta_color: str = "normal") -> None:
    """Render a compact metric with optional delta."""
    st.metric(label=label, value=value, delta=delta, delta_color=delta_color)


def action_badge(action: str) -> str:
    """Return CSS class for action color coding."""
    if action == "BUY_YES":
        return "action-buy-yes"
    if action == "BUY_NO":
        return "action-buy-no"
    return "action-wait"
