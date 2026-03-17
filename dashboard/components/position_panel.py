"""
dashboard/components/position_panel.py — Open position display.
"""

from __future__ import annotations

import streamlit as st

from ..data.schemas import StateSnapshot


def render_position_panel(snapshot: StateSnapshot) -> None:
    """Display open position or 'No open position'."""
    if snapshot.open_position_side and snapshot.open_position_entry is not None:
        side = snapshot.open_position_side.upper()
        entry = snapshot.open_position_entry
        contracts = snapshot.open_position_contracts or 0
        pnl = snapshot.unrealized_pnl
        pnl_str = f"${pnl:+.2f}" if pnl is not None else "—"
        st.markdown(f"**{side}** @ {entry:.3f} × {contracts} | PnL: {pnl_str}")
    else:
        st.markdown("*No open position*")
