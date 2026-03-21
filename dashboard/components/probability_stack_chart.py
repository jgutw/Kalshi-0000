"""
dashboard/components/probability_stack_chart.py — p_market, p_base, p_real line chart.
"""

from __future__ import annotations

from typing import List, Optional

import plotly.graph_objects as go
import streamlit as st

from ..data.schemas import DecisionEvent

PLOTLY_LAYOUT = dict(
    template="plotly_dark",
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    margin=dict(l=40, r=20, t=30, b=40),
    height=280,
    xaxis=dict(showgrid=True, gridcolor="rgba(100,100,100,0.2)"),
    yaxis=dict(range=[0, 1], showgrid=True, gridcolor="rgba(100,100,100,0.2)"),
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    showlegend=True,
)


def render_probability_stack_chart(
    events: List[DecisionEvent],
    selected_index: Optional[int] = None,
) -> None:
    """
    Plotly line chart: p_market (blue), p_base (orange), p_real (green).
    Reference lines at 0.47 and 0.53. Optional vertical marker at selected_index.
    """
    if not events:
        st.info("No decision data for selected asset.")
        return

    ts_vals = [e.ts for e in events]
    p_market = [e.p_market if e.p_market is not None else 0.5 for e in events]
    p_base = [e.p_base if e.p_base is not None else 0.5 for e in events]
    p_real = [e.p_real if e.p_real is not None else None for e in events]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=ts_vals, y=p_market, name="p_market", line=dict(color="#4a90d9", width=2)))
    fig.add_trace(go.Scatter(x=ts_vals, y=p_base, name="p_base", line=dict(color="#f5a623", width=2)))
    fig.add_trace(go.Scatter(x=ts_vals, y=p_real, name="p_real", line=dict(color="#00c864", width=2)))

    fig.add_hline(y=0.47, line_dash="dash", line_color="rgba(150,150,150,0.6)")
    fig.add_hline(y=0.53, line_dash="dash", line_color="rgba(150,150,150,0.6)")

    if selected_index is not None and 0 <= selected_index < len(ts_vals):
        fig.add_vline(x=ts_vals[selected_index], line_dash="dot", line_color="#f0a500", line_width=2)

    fig.update_layout(**PLOTLY_LAYOUT)
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
