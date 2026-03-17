"""
dashboard/components/strategy_router_panel.py — Strategy router table by asset.
"""

from __future__ import annotations

from typing import Dict

import pandas as pd
import streamlit as st

from ..data.schemas import StateSnapshot


def _action_style(action: str) -> str:
    if action == "BUY_YES":
        return "background: rgba(0,200,100,0.15); color: #00c864;"
    if action == "BUY_NO":
        return "background: rgba(220,50,50,0.15); color: #dc3232;"
    return "background: rgba(100,100,100,0.10); color: #888888;"


def render_strategy_router_panel(snapshots: Dict[str, StateSnapshot]) -> None:
    """Table: asset | active_strategy | score | router_action | lag_confidence | spot_confidence | veto_active."""
    if not snapshots:
        st.info("No snapshot data.")
        return

    rows = []
    for asset in ["BTC", "ETH", "SOL", "XRP"]:
        s = snapshots.get(asset)
        if not s:
            continue
        cwm = s.confidence_weighted_mispricing or 0.0
        rows.append({
            "asset": asset,
            "active_strategy": s.active_strategy,
            "score": f"{cwm:+.4f}",
            "router_action": s.router_action,
            "lag_confidence": f"{s.lag_confidence:.2f}",
            "spot_confidence": f"{s.spot_confidence:.2f}",
            "veto_active": "—",
        })

    df = pd.DataFrame(rows)
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "router_action": st.column_config.TextColumn(
                "Action",
                help="BUY_YES / BUY_NO / WAIT",
            ),
        },
    )
