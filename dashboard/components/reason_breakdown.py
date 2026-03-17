"""
dashboard/components/reason_breakdown.py — WAIT reason counts horizontal bar chart.
"""

from __future__ import annotations

from collections import Counter
from typing import List

import plotly.graph_objects as go
import streamlit as st

from ..data.schemas import DecisionEvent

REASON_COLORS = {
    "window_boundary": "rgba(150,150,150,0.8)",
    "uncertain_near_50": "rgba(240,165,0,0.8)",
    "lag_absent": "rgba(255,140,0,0.8)",
    "venue_dislocation": "rgba(220,50,50,0.8)",
    "circuit_breaker_cooldown": "rgba(180,0,0,0.8)",
    "edge": "rgba(150,150,150,0.8)",
    "conviction": "rgba(150,150,150,0.8)",
    "default": "rgba(150,150,150,0.6)",
}


def _reason_category(reason: str) -> str:
    r = reason.lower()
    if "window_boundary" in r:
        return "window_boundary"
    if "uncertain_near_50" in r:
        return "uncertain_near_50"
    if "lag_absent" in r:
        return "lag_absent"
    if "venue_dislocation" in r:
        return "venue_dislocation"
    if "circuit_breaker" in r or "cooldown" in r:
        return "circuit_breaker_cooldown"
    if "edge" in r:
        return "edge"
    if "conviction" in r:
        return "conviction"
    return "other"


def render_reason_breakdown(decisions: List[DecisionEvent], last_n: int = 200) -> None:
    """Horizontal bar chart of wait_reason counts from last N decisions."""
    wait_reasons = [
        _reason_category(d.reason)
        for d in decisions[-last_n:]
        if d.action == "WAIT" and d.reason
    ]
    if not wait_reasons:
        st.info("No WAIT decisions in recent data.")
        return

    counts = Counter(wait_reasons)
    reasons = list(counts.keys())
    vals = [counts[r] for r in reasons]
    colors = [REASON_COLORS.get(r, REASON_COLORS["default"]) for r in reasons]

    fig = go.Figure(go.Bar(x=vals, y=reasons, orientation="h", marker_color=colors))
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=80, r=20, t=20, b=40),
        height=min(250, 40 + len(reasons) * 28),
        xaxis=dict(title="Count", showgrid=True, gridcolor="rgba(100,100,100,0.2)"),
        yaxis=dict(autorange="reversed"),
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
