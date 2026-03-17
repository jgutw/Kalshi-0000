"""
dashboard/components/feature_contributions.py — Feature contribution breakdown for alpha_micro.
"""

from __future__ import annotations

from typing import Dict, Optional

import plotly.graph_objects as go
import streamlit as st

FEATURE_KEYS = ["obi", "ofi_hawkes", "microprice_dev", "trade_sign_autocorr", "lag_signal", "response_gap"]
COEFFICIENTS = {
    "obi": 0.15,
    "ofi_hawkes": 0.12,
    "microprice_dev": 0.10,
    "trade_sign_autocorr": 0.08,
    "lag_signal": 0.25,
    "response_gap": 0.20,
}


def render_feature_contributions(
    raw_features: Dict[str, float],
    alpha_micro: float,
) -> None:
    """
    Table: feature | raw_value | standardized_z | coefficient | contribution.
    Bar chart of contributions (green positive, red negative).
    """
    if not raw_features:
        st.info("No raw features available.")
        return

    rows = []
    contributions = []
    for key in FEATURE_KEYS:
        raw = raw_features.get(key, 0.0)
        coef = COEFFICIENTS.get(key, 0.1)
        z = raw * 2.0
        contrib = raw * coef
        contributions.append(contrib)
        rows.append({
            "feature": key,
            "raw_value": f"{raw:.4f}",
            "standardized_z": f"{z:.4f}",
            "coefficient": f"{coef:.4f}",
            "contribution": f"{contrib:+.4f}",
        })

    import pandas as pd
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    colors = ["#00c864" if c >= 0 else "#dc3232" for c in contributions]
    fig = go.Figure(go.Bar(x=FEATURE_KEYS, y=contributions, marker_color=colors))
    fig.add_hline(y=alpha_micro, line_dash="dash", line_color="#f0a500", annotation_text="alpha_micro")
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=50, r=20, t=30, b=80),
        height=220,
        xaxis=dict(tickangle=-30),
        yaxis=dict(title="Contribution"),
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
