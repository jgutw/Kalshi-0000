"""
dashboard_app.py — Kalshi Trading Control Room.

Pages: Ops (default) · Asset · Journal · Windows
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

PAGES_DIR = Path(__file__).resolve().parent / "dashboard" / "pages"

st.set_page_config(
    layout="wide",
    page_title="Kalshi Trading Control Room",
    page_icon="📊",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .stApp { background-color: #0e1117; }
    .halt-banner { border: 2px solid #cc0000; background: rgba(204,0,0,0.15); padding: 8px; border-radius: 6px; }
    .mock-banner { background: rgba(240,165,0,0.2); border: 1px solid #f0a500; padding: 8px; border-radius: 6px; }
    /* Keep dataframes readable */
    div[data-testid="stDataFrame"] { font-size: 0.92rem; }
</style>
""", unsafe_allow_html=True)

pg = st.navigation(
    [
        st.Page(str(PAGES_DIR / "00_ops.py"), title="Ops", default=True),
        st.Page(str(PAGES_DIR / "01_mission_control.py"), title="Asset"),
        st.Page(str(PAGES_DIR / "02_trade_journal.py"), title="Journal"),
        st.Page(str(PAGES_DIR / "06_window_performance.py"), title="Windows"),
    ]
)
pg.run()
