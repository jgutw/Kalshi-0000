"""Kalshi Control Room.

The landing page chooses an environment. Shadow and Live are separate page
graphs. Shadow renders dashboard.shadow_app, which never imports vault or
order paths. Live page scripts run only after the operator acknowledges
real capital. That acknowledgement is a UX gate, not an authorization boundary.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard.environments import LIVE_ACK_KEY, LIVE_ACK_TEXT, live_controls_open

PAGES = ROOT / "dashboard" / "pages"
LIVE_COPY = (
    "Production operator dashboard. It shows the live book, asset detail, "
    "trade journal, and window performance, and it can queue vault take-cash "
    "for the running trader. It does not place exchange orders from this screen."
)


def landing() -> None:
    st.header("Kalshi Control Room")
    st.caption("Choose an operating environment. This choice is navigation, not a permission switch.")
    shadow, live = st.columns(2)
    with shadow:
        st.subheader("SHADOW")
        st.markdown("### NO CAPITAL")
        st.markdown(
            "- Simulated execution\n"
            "- Research settlement\n"
            "- Shadow portfolio\n"
            "- Read-only operator dashboard"
        )
        if st.button("Enter Shadow", type="primary", key="enter-shadow"):
            st.switch_page(shadow_page)
    with live:
        st.subheader("PRODUCTION")
        st.markdown("### MODE FROM THE OWNED PROCESS")
        st.markdown(
            "Paper and live share this dashboard. The page banner states which one is running. "
            "It does not start a trader."
        )
        if st.button("Enter production", type="primary", key="enter-live"):
            st.switch_page(live_ops_page)


def shadow_room() -> None:
    if st.button("Environment selection", key="back-shadow"):
        st.switch_page(landing_page)
    from dashboard.shadow_app import _page
    _page(embedded=True)


def _live(script: Path, key: str):
    def run() -> None:
        from dashboard.operator_view import observe_ownership, production_banner, production_mode
        state, mode_name, _session = observe_ownership()
        banner = production_banner(production_mode(state, mode_name))
        st.markdown(f"**{banner}**")
        if st.button("Environment selection", key=f"back-{key}"):
            st.session_state[LIVE_ACK_KEY] = False
            st.switch_page(landing_page)
        if not live_controls_open(dict(st.session_state)):
            st.header(banner)
            if "LIVE —" in banner:
                st.error(LIVE_ACK_TEXT)
            st.markdown(LIVE_COPY)
            if st.button("Enter production view", key=f"ack-{key}"):
                st.session_state[LIVE_ACK_KEY] = True
                st.rerun()
            return
        runpy.run_path(str(script), run_name="__control_room_live__")
    return run


landing_page = st.Page(landing, title="Control Room", default=True)
shadow_page = st.Page(shadow_room, title="Shadow", url_path="shadow")
live_ops_page = st.Page(_live(PAGES / "00_ops.py", "ops"), title="Ops", url_path="live-ops")
live_asset_page = st.Page(_live(PAGES / "01_mission_control.py", "asset"), title="Asset", url_path="live-asset")
live_journal_page = st.Page(_live(PAGES / "02_trade_journal.py", "journal"), title="Journal", url_path="live-journal")
live_windows_page = st.Page(_live(PAGES / "06_window_performance.py", "windows"), title="Windows", url_path="live-windows")


def main() -> None:
    st.set_page_config(page_title="Kalshi Control Room", layout="wide")
    st.navigation(
        [landing_page, shadow_page, live_ops_page, live_asset_page, live_journal_page, live_windows_page]
    ).run()


if __name__ == "__main__":
    main()
