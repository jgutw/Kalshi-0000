"""
Mission Control — What does the bot think right now?
"""

from __future__ import annotations

import time
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

from dashboard.components.probability_stack_chart import render_probability_stack_chart
from dashboard.components.reason_breakdown import render_reason_breakdown
from dashboard.components.sidebar import render_sidebar
from dashboard.components.strategy_router_panel import render_strategy_router_panel
from dashboard.data.state_store import StateStore

auto_refresh = render_sidebar()
store = StateStore()

# Log staleness indicator (top of page)
_dec_path = Path(__file__).resolve().parent.parent.parent / "logs" / "kalshi_decisions.jsonl"
if not _dec_path.exists():
    st.markdown(
        '<div style="padding:8px;background:rgba(100,100,100,0.2);border-radius:6px;margin-bottom:12px">'
        '<b>Last log update:</b> No log file yet</div>',
        unsafe_allow_html=True,
    )
else:
    mtime = _dec_path.stat().st_mtime
    age_secs = time.time() - mtime
    if age_secs > 30:
        st.markdown(
            '<div style="padding:8px;background:rgba(204,0,0,0.2);border:1px solid #cc0000;'
            'border-radius:6px;margin-bottom:12px;color:#cc0000">'
            f'<b>Last log update:</b> {int(age_secs)} seconds ago — '
            '<b>LOG STALE — bot may not be running</b></div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f'<div style="padding:8px;background:rgba(0,200,100,0.1);border-radius:6px;margin-bottom:12px">'
            f'<b>Last log update:</b> {int(age_secs)} seconds ago</div>',
            unsafe_allow_html=True,
        )

portfolio = store.get_portfolio()
snapshots = store.get_latest_snapshots()
decisions = store.get_decisions(last_n=500)

# Row 1: Portfolio metrics bar
c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
equity = portfolio.total_equity or (portfolio.balance + portfolio.vault_balance)
total_pnl = equity - portfolio.starting_balance
pnl_pct = (total_pnl / portfolio.starting_balance * 100) if portfolio.starting_balance else 0
with c1:
    st.metric("Trading", f"${portfolio.balance:,.2f}")
with c2:
    st.metric("Vault", f"${portfolio.vault_balance:,.2f}")
with c3:
    st.metric("Equity", f"${equity:,.2f}", f"{pnl_pct:+.1f}%")
with c4:
    st.metric("Win Rate", f"{portfolio.win_rate:.1%}")
with c5:
    st.metric("Sharpe", f"{portfolio.sharpe:.2f}")
with c6:
    st.metric("VaR 95%", f"{portfolio.var_95:.2%}")
with c7:
    if portfolio.halt_state:
        st.markdown(f'<span style="color:#cc0000">HALTED — {portfolio.halt_reason}</span>', unsafe_allow_html=True)
    else:
        st.success("OK")

# Row 2: Per-asset cards
st.divider()
assets = ["BTC", "ETH", "SOL", "XRP"]
cols = st.columns(4)
for i, asset in enumerate(assets):
    with cols[i]:
        s = snapshots.get(asset)
        if not s:
            st.info(f"{asset} — No data")
            continue

        # Header
        ticker = f"KX{asset}15M"
        st.markdown(f"### {asset} — {ticker}")
        prog = s.time_remaining_secs / 900.0
        if s.time_remaining_secs > 300:
            color = "normal"
        elif s.time_remaining_secs > 60:
            color = "off"
        else:
            color = "inverse"
        st.progress(prog)

        # Warnings
        if s.synthetic_confidence < 0.3:
            st.markdown('<span class="feed-unreliable">FEED UNRELIABLE</span>', unsafe_allow_html=True)
        if s.dislocation > 0.003:
            st.markdown('<span class="venue-split">VENUE SPLIT</span>', unsafe_allow_html=True)
        if s.kalshi_quote_age_secs > 15:
            st.markdown('<span class="stale-quote">STALE QUOTE</span>', unsafe_allow_html=True)
        if s.halt_state:
            st.markdown('<div class="halt-banner">HALTED</div>', unsafe_allow_html=True)

        # Price section
        spot_now = s.spot_now or 0
        spot_start = s.spot_start or 0
        arrow = "↑" if spot_now >= spot_start else "↓"
        color_arrow = "#00c864" if spot_now >= spot_start else "#dc3232"
        st.markdown(f"Spot: {spot_now:,.0f} vs {spot_start:,.0f} <span style='color:{color_arrow}'>{arrow}</span>", unsafe_allow_html=True)
        st.caption(f"Dislocation: {s.dislocation:.4f} | Synth conf: {s.synthetic_confidence:.2f} | Quote age: {s.kalshi_quote_age_secs:.1f}s")

        # Structural model
        st.markdown("**STRUCTURAL MODEL**")
        z = s.z_threshold
        if z > 1.5:
            z_label = "STRONG YES"
            z_color = "#00c864"
        elif z > 0.5:
            z_label = "LEAN YES"
            z_color = "#00c864"
        elif z > -0.5:
            z_label = "COINFLIP"
            z_color = "#888888"
        elif z > -1.5:
            z_label = "LEAN NO"
            z_color = "#dc3232"
        else:
            z_label = "STRONG NO"
            z_color = "#dc3232"
        st.markdown(f"z: {z:.2f} <span style='color:{z_color}'>{z_label}</span>", unsafe_allow_html=True)
        st.caption(f"p_base: {(s.p_base or 0)*100:.1f}%")

        # Probability stack
        st.markdown("**PROBABILITY STACK**")
        p_m = s.p_market or 0
        p_b = s.p_base or 0
        p_r_str = f"{s.p_real:.3f}" if s.p_real is not None else "—"
        st.markdown(f"p_market: {p_m:.3f} | p_base: {p_b:.3f} | p_real: {p_r_str}")
        cwm = s.confidence_weighted_mispricing or 0
        if cwm > 0.04:
            st.markdown(f'<span style="color:#00c864">CWM: {cwm:+.4f}</span>', unsafe_allow_html=True)
        elif cwm < -0.04:
            st.markdown(f'<span style="color:#dc3232">CWM: {cwm:+.4f}</span>', unsafe_allow_html=True)
        else:
            st.caption(f"CWM: {cwm:+.4f}")

        # Strategy
        st.markdown("**Strategy**")
        action_style = "color:#00c864" if s.router_action == "BUY_YES" else ("color:#dc3232" if s.router_action == "BUY_NO" else "color:#888888")
        st.markdown(f"{s.active_strategy} — <span style='{action_style}'>{s.router_action}</span>", unsafe_allow_html=True)
        if s.wait_reason:
            st.caption(f"*{s.wait_reason}*")
        st.caption(f"lag_conf: {s.lag_confidence:.2f} | spot_conf: {s.spot_confidence:.2f}")

        # Position
        st.markdown("**Position**")
        if s.open_position_side:
            st.markdown(f"{s.open_position_side.upper()} @ {s.open_position_entry:.3f} × {s.open_position_contracts or 0}")
            if s.unrealized_pnl is not None:
                st.caption(f"PnL: ${s.unrealized_pnl:+.2f}")
        else:
            st.caption("*No open position*")

# Row 3: Two columns
st.divider()
col_left, col_right = st.columns(2)
with col_left:
    asset_sel = st.selectbox("Asset for chart", assets, key="mc_asset")
    asset_dec = [d for d in decisions if d.asset == asset_sel]
    render_probability_stack_chart(asset_dec[-50:] if asset_dec else [], None)
with col_right:
    render_reason_breakdown(decisions, 200)

# Row 4: Strategy router panel
st.divider()
render_strategy_router_panel(snapshots)

if auto_refresh:
    time.sleep(5)
    st.rerun()
