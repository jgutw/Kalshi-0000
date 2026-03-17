"""
dashboard_app.py — Kalshi Trading Control Room.

Multi-page Streamlit dashboard for the Kalshi 15-minute multi-asset trading bot.
Renders in mock mode when log files are missing or empty.
"""

from __future__ import annotations

import time

import streamlit as st

from dashboard.data.state_store import StateStore

st.set_page_config(
    layout="wide",
    page_title="Kalshi Trading Control Room",
    page_icon="📊",
    initial_sidebar_state="expanded",
)

# Global CSS
st.markdown("""
<style>
    .stApp { background-color: #0e1117; }
    .metric-card { padding: 0.5rem 0; }
    .action-buy-yes { background: rgba(0,200,100,0.15); color: #00c864; padding: 2px 8px; border-radius: 4px; }
    .action-buy-no { background: rgba(220,50,50,0.15); color: #dc3232; padding: 2px 8px; border-radius: 4px; }
    .action-wait { background: rgba(100,100,100,0.10); color: #888888; padding: 2px 8px; border-radius: 4px; }
    .halt-banner { border: 2px solid #cc0000; background: rgba(204,0,0,0.15); padding: 8px; border-radius: 6px; }
    .mock-banner { background: rgba(240,165,0,0.2); border: 1px solid #f0a500; padding: 8px; border-radius: 6px; }
    .feed-unreliable { color: #dc3232; font-weight: bold; }
    .venue-split { color: #f0a500; font-weight: bold; }
    .stale-quote { color: #f0a500; font-weight: bold; }
</style>
""", unsafe_allow_html=True)

store = StateStore()

# Sidebar
with st.sidebar:
    if store.is_mock_mode():
        st.markdown(
            '<div class="mock-banner">⚠️ <b>MOCK MODE</b> — no live logs detected</div>',
            unsafe_allow_html=True,
        )
        st.divider()

    portfolio = store.get_portfolio()
    pnl = portfolio.balance - portfolio.starting_balance
    pnl_pct = (pnl / portfolio.starting_balance * 100) if portfolio.starting_balance else 0

    st.metric("Balance", f"${portfolio.balance:,.2f}", f"{pnl_pct:+.1f}%")
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

    st.divider()
    auto_refresh = st.toggle("Auto-refresh", value=True)
    if auto_refresh:
        st.caption("Refreshing every 5s")

# Home page content
st.markdown("# Kalshi Trading Control Room")
st.markdown("Select a page from the sidebar to get started.")
st.markdown("- **Mission Control** — What does the bot think right now?")
st.markdown("- **Trade Journal** — Daily review of closed trades")

if auto_refresh:
    time.sleep(5)
    st.rerun()
