"""
dashboard/components/sidebar.py — Shared sidebar for all pages.
"""

from __future__ import annotations

import time

import streamlit as st

from ..data.state_store import StateStore
from .vault_panel import render_vault_panel


def render_sidebar() -> bool:
    """
    Render sidebar. Returns auto_refresh setting.
    Call at top of each page.
    """
    store = StateStore()

    with st.sidebar:
        if store.is_mock_mode():
            st.markdown(
                '<div class="mock-banner">⚠️ <b>MOCK MODE</b> — no live logs detected</div>',
                unsafe_allow_html=True,
            )
            st.divider()

        portfolio = store.get_portfolio()
        equity = portfolio.total_equity or (portfolio.balance + portfolio.vault_balance)
        total_pnl = equity - portfolio.starting_balance
        pnl_pct = (total_pnl / portfolio.starting_balance * 100) if portfolio.starting_balance else 0

        st.metric("Trading", f"${portfolio.balance:,.2f}")
        st.metric("Vault", f"${portfolio.vault_balance:,.2f}")
        st.metric("Equity", f"${equity:,.2f}", f"{pnl_pct:+.1f}%")
        st.metric("Win Rate", f"{portfolio.win_rate:.1%}", None)
        st.metric("Sharpe", f"{portfolio.sharpe:.2f}", None)

        if portfolio.halt_state:
            st.markdown(
                f'<div class="halt-banner">🛑 HALTED — {portfolio.halt_reason}</div>',
                unsafe_allow_html=True,
            )
        else:
            st.success("OK")

        st.divider()
        st.markdown("**Per-asset status**")
        snapshots = store.get_latest_snapshots()
        for asset in ["BTC", "ETH", "SOL", "XRP"]:
            s = snapshots.get(asset)
            if s:
                conf = s.synthetic_confidence
                halted = s.halt_state
                consec = portfolio.consec_losses
                if halted or conf < 0.3:
                    st.markdown(f"🔴 {asset}")
                elif conf < 0.6 or consec >= 2:
                    st.markdown(f"🟡 {asset}")
                else:
                    st.markdown(f"🟢 {asset}")
            else:
                st.markdown(f"⚪ {asset}")

        render_vault_panel()

        st.divider()
        auto_refresh = st.toggle("Auto-refresh", value=True)
        if auto_refresh:
            st.caption("Refreshing every 5s")

    return auto_refresh
