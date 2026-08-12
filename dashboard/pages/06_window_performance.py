"""
Windows — Per 15-min window grid with decision context (Ops-aligned).
"""

from __future__ import annotations

import time

import pandas as pd
import streamlit as st

from dashboard.components.sidebar import render_sidebar
from dashboard.data.state_store import StateStore

auto_refresh = render_sidebar(refresh_secs=8)
store = StateStore()
rows = store.get_window_performance()

st.title("Windows")
st.caption(
    "One row per asset×15m window, written at each rollover (including NO_TRADE). "
    "Thinking columns mirror Ops. Live file: logs/kalshi_windows.jsonl."
)

if not rows:
    st.warning(
        "No window rows yet. The **trading bot** must be running — Streamlit alone does not "
        "close 15m windows. After the next UTC :00/:15/:30/:45 rollover, rows appear here."
    )
    if auto_refresh:
        time.sleep(8)
        st.rerun()
    st.stop()

show_no_trade = st.toggle("Include NO_TRADE windows", value=True)
traded_only = [r for r in rows if r.get("bot_action") != "NO_TRADE"]
view = rows if show_no_trade else traded_only

if not view:
    st.info("No traded windows yet — toggle NO_TRADE to see waits.")
else:
    table = []
    for r in view:
        cwm = r.get("cwm")
        pm = r.get("p_market")
        pb = r.get("p_base")
        entry = r.get("entry_price")
        correct = r.get("correct")
        if correct is True:
            ok = "✓"
        elif correct is False:
            ok = "✗"
        else:
            ok = "—"
        table.append({
            "Window": r.get("window") or "?",
            "Asset": r.get("asset") or "?",
            "Action": r.get("bot_action") or "—",
            "Reason": (r.get("reason") or "")[:48],
            "Strategy": (r.get("strategy") or "")[:14],
            "p_mkt": round(float(pm), 2) if pm is not None else None,
            "p_base": round(float(pb), 2) if pb is not None else None,
            "Lag": round(float(r.get("lag") or 0), 2),
            "Spot conf": round(float(r.get("spot_conf") or 0), 2),
            "CWM": round(float(cwm), 3) if cwm is not None else None,
            "Entry": round(float(entry), 3) if entry is not None else None,
            "Risked $": round(float(r.get("risked_$") or 0), 2),
            "P&L $": round(float(r.get("pnl") or 0), 2),
            "Outcome": r.get("actual_outcome") or "—",
            "OK": ok,
        })

    df = pd.DataFrame(table)
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        height=min(520, 48 + 34 * min(len(df), 14)),
        column_config={
            "p_mkt": st.column_config.NumberColumn(format="%.2f"),
            "p_base": st.column_config.NumberColumn(format="%.2f"),
            "Lag": st.column_config.NumberColumn(format="%.2f"),
            "Spot conf": st.column_config.NumberColumn(format="%.2f"),
            "CWM": st.column_config.NumberColumn(format="%+.3f"),
            "Entry": st.column_config.NumberColumn(format="%.3f"),
            "Risked $": st.column_config.NumberColumn(format="$%.2f"),
            "P&L $": st.column_config.NumberColumn(format="$%+.2f"),
            "Reason": st.column_config.TextColumn(width="medium"),
        },
    )

# Compact WR by asset (table, not a cramped bar chart)
st.divider()
st.subheader("Win rate by asset (traded windows)")
if traded_only:
    from collections import defaultdict
    by = defaultdict(lambda: {"ok": 0, "n": 0, "pnl": 0.0, "risked": 0.0})
    for r in traded_only:
        if r.get("correct") is None:
            continue
        a = r["asset"]
        by[a]["n"] += 1
        by[a]["ok"] += int(bool(r["correct"]))
        by[a]["pnl"] += float(r.get("pnl") or 0)
        by[a]["risked"] += float(r.get("risked_$") or 0)
    summary = []
    for a, v in sorted(by.items()):
        summary.append({
            "Asset": a,
            "Windows": v["n"],
            "WR": v["ok"] / v["n"] if v["n"] else 0,
            "P&L $": round(v["pnl"], 2),
            "Risked $": round(v["risked"], 2),
        })
    st.dataframe(
        pd.DataFrame(summary),
        use_container_width=True,
        hide_index=True,
        column_config={
            "WR": st.column_config.NumberColumn(format="%.0%"),
            "P&L $": st.column_config.NumberColumn(format="$%+.2f"),
            "Risked $": st.column_config.NumberColumn(format="$%.2f"),
        },
    )
else:
    st.caption("No traded windows yet.")

if auto_refresh:
    time.sleep(8)
    st.rerun()
