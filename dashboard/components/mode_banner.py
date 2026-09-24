"""Production-page banner. Reads ownership; does not start a trader."""

from __future__ import annotations

import streamlit as st

from dashboard.operator_view import production_banner, production_mode


def render_production_banner() -> str:
    try:
        from dashboard.operator_view import observe_ownership, production_banner, production_mode
        state, mode_name, _session = observe_ownership()
        mode = production_mode(state, mode_name)
    except Exception:
        mode = "UNKNOWN"
    text = production_banner(mode)
    if mode == "LIVE":
        st.error(text)
    elif mode == "PAPER":
        st.warning(text)
    else:
        st.info(text)
    st.caption("Rows below are BOT / LOCAL. They are not Kalshi exchange truth.")
    return mode
