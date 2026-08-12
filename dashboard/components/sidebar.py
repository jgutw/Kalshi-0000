"""
dashboard/components/sidebar.py — Shared sidebar for all pages.

Kept light for latency: portfolio strip + refresh only (no per-asset list).
"""

from __future__ import annotations

import streamlit as st

from ..data.state_store import StateStore
from .vault_panel import render_vault_panel


def render_sidebar(refresh_secs: int = 5) -> bool:
    """
    Render sidebar. Returns auto_refresh setting.
    """
    store = StateStore()

    with st.sidebar:
        if store.is_mock_mode():
            st.markdown(
                '<div class="mock-banner">⚠️ <b>MOCK MODE</b> — no live logs</div>',
                unsafe_allow_html=True,
            )
            st.divider()

        portfolio = store.get_portfolio()
        equity = portfolio.total_equity or (portfolio.balance + portfolio.vault_balance)
        total_pnl = equity - portfolio.starting_balance
        pnl_pct = (total_pnl / portfolio.starting_balance * 100) if portfolio.starting_balance else 0

        st.metric("Equity", f"${equity:,.2f}", f"{pnl_pct:+.1f}%")
        st.caption(f"Trading ${portfolio.balance:,.0f} · Vault ${portfolio.vault_balance:,.0f}")
        st.caption(f"WR {portfolio.win_rate:.0%} · Sharpe {portfolio.sharpe:.2f}")

        if portfolio.halt_state:
            st.markdown(
                f'<div class="halt-banner">🛑 HALTED — {portfolio.halt_reason}</div>',
                unsafe_allow_html=True,
            )
        else:
            st.success("Running OK" if not store.is_mock_mode() else "Mock OK")

        with st.expander("Vault / Take cash", expanded=False):
            render_vault_panel()

        st.divider()
        auto_refresh = st.toggle("Auto-refresh", value=True)
        if auto_refresh:
            st.caption(f"Every {refresh_secs}s")

    return auto_refresh
