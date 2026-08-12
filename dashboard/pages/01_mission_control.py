"""
Asset — Deep dive for one symbol (optional detail behind Ops).
"""

from __future__ import annotations

import time

import streamlit as st

from dashboard.components.probability_stack_chart import render_probability_stack_chart
from dashboard.components.reason_breakdown import render_reason_breakdown
from dashboard.components.sidebar import render_sidebar
from dashboard.data.state_store import StateStore

auto_refresh = render_sidebar(refresh_secs=8)
store = StateStore()
snapshots = store.get_latest_snapshots()
decisions = store.get_decisions(last_n=300)

try:
    from kalshi_bot.config import all_asset_symbols
    assets = all_asset_symbols()
except Exception:
    assets = list(snapshots.keys()) or ["BTC"]

st.title("Asset")
st.caption("Single-asset detail — use Ops for the full book.")

# Green outline on the selected asset button (primary)
st.markdown(
    """
<style>
/* Selected asset pill — green outline */
div[data-testid="stHorizontalBlock"] button[kind="primary"],
div[data-testid="stHorizontalBlock"] button[data-testid="baseButton-primary"] {
    border: 2px solid #00c864 !important;
    background-color: rgba(0, 200, 100, 0.12) !important;
    color: #f0f0f0 !important;
    box-shadow: none !important;
}
/* Unselected — quiet rectangles */
div[data-testid="stHorizontalBlock"] button[kind="secondary"],
div[data-testid="stHorizontalBlock"] button[data-testid="baseButton-secondary"] {
    border: 1px solid rgba(255, 255, 255, 0.18) !important;
    background-color: rgba(255, 255, 255, 0.04) !important;
    color: #c8c8c8 !important;
}
div[data-testid="stHorizontalBlock"] button {
    border-radius: 6px !important;
    min-height: 2.4rem !important;
    font-weight: 600 !important;
}
</style>
""",
    unsafe_allow_html=True,
)

if "asset_detail_symbol" not in st.session_state or st.session_state.asset_detail_symbol not in assets:
    st.session_state.asset_detail_symbol = assets[0]

cols = st.columns(len(assets))
for i, sym in enumerate(assets):
    selected = st.session_state.asset_detail_symbol == sym
    with cols[i]:
        if st.button(
            sym,
            key=f"asset_btn_{sym}",
            use_container_width=True,
            type="primary" if selected else "secondary",
        ):
            st.session_state.asset_detail_symbol = sym
            st.rerun()

asset = st.session_state.asset_detail_symbol
s = snapshots.get(asset)
asset_dec = [d for d in decisions if d.asset == asset]

if not s:
    st.info(f"No snapshot for {asset}.")
else:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Action", s.router_action or "—")
    c2.metric("Spot conf", f"{(s.spot_confidence or s.synthetic_confidence):.2f}")
    c3.metric("Lag", f"{s.lag_confidence:.2f}")
    cwm = s.confidence_weighted_mispricing
    c4.metric("CWM", f"{cwm:+.3f}" if cwm is not None else "—")

    if s.wait_reason:
        st.caption(f"Reason: {s.wait_reason}")
    st.caption(
        f"Strategy {s.active_strategy} · "
        f"p_mkt={s.p_market if s.p_market is not None else '—'} · "
        f"p_base={s.p_base if s.p_base is not None else '—'} · "
        f"p_real={s.p_real if s.p_real is not None else '—'} · "
        f"z={s.z_threshold:.2f} · "
        f"t_left={s.time_remaining_secs:.0f}s · "
        f"spread={s.kalshi_spread:.3f}"
    )

    col_l, col_r = st.columns(2)
    with col_l:
        st.subheader("Probability path")
        render_probability_stack_chart(asset_dec[-40:] if asset_dec else [], None)
    with col_r:
        st.subheader("WAIT reasons")
        render_reason_breakdown(asset_dec, 200)

if auto_refresh:
    time.sleep(8)
    st.rerun()
