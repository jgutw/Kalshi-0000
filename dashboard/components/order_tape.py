"""
dashboard/components/order_tape.py — Chronological list of BUY_YES/BUY_NO actions.
"""

from __future__ import annotations

from typing import List

import pandas as pd
import streamlit as st

from ..data.schemas import DecisionEvent, TradeEvent


def render_order_tape_from_decisions(decisions: List[DecisionEvent]) -> None:
    """Order tape built from decision log (BUY_YES/BUY_NO only)."""
    actions = [d for d in decisions if d.action in ("BUY_YES", "BUY_NO")]
    if not actions:
        st.info("No BUY_YES/BUY_NO actions in decision log.")
        return

    rows = []
    for d in actions:
        rows.append({
            "ts": d.ts,
            "asset": d.asset,
            "action": d.action,
            "entry_price": f"{d.p_market:.3f}" if d.p_market is not None else "—",
            "contracts": "—",
            "kalshi_spread": f"{d.kalshi_spread:.4f}",
            "quote_age": f"{d.kalshi_quote_age_secs:.1f}s",
            "strategy": d.strategy,
        })

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)


def render_order_tape_from_trades(trades: List[TradeEvent]) -> None:
    """Order tape from closed trades."""
    if not trades:
        st.info("No trades yet.")
        return

    rows = []
    for t in trades:
        side = "yes" if t.entry > 0.5 else "no"
        action = "BUY_YES" if side == "yes" else "BUY_NO"
        rows.append({
            "ts": t.ts,
            "asset": t.asset,
            "action": action,
            "entry_price": f"{t.entry:.3f}",
            "contracts": t.contracts,
            "kalshi_spread": f"{t.kalshi_spread_at_entry:.4f}",
            "quote_age": f"{t.kalshi_quote_age_at_entry:.1f}s",
            "strategy": t.strategy,
        })

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)
