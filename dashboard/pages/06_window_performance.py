"""
Window Performance — Per-15-min-window analysis.
"""

from __future__ import annotations

from collections import defaultdict

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard.components.sidebar import render_sidebar
from dashboard.data.state_store import StateStore

auto_refresh = render_sidebar()
store = StateStore()
rows = store.get_window_performance()

st.title("Window Performance")
st.caption("Per-15-minute window analysis by asset")

if not rows:
    st.info("No window data yet. Run the bot to populate.")
    if auto_refresh:
        import time
        time.sleep(5)
        st.rerun()
    st.stop()

# Table
df = pd.DataFrame(rows)
df = df[["window", "asset", "price_to_beat", "exit_price", "actual_outcome",
         "bot_action", "entry_price", "pnl", "correct"]]
df.columns = ["Window", "Asset", "Price to Beat", "Exit Price", "Actual Outcome",
              "Bot Action", "Entry Price", "P&L", "Correct"]
df["P&L"] = df["P&L"].apply(lambda x: f"${x:+.2f}" if pd.notna(x) else "")
df["Entry Price"] = df["Entry Price"].apply(
    lambda x: f"{x:.3f}" if pd.notna(x) and x is not None else "—"
)
df["Correct"] = df["Correct"].apply(
    lambda x: "✓" if x is True else "✗" if x is False else "—"
)
st.dataframe(df, use_container_width=True, hide_index=True)

# Win rate by asset
st.divider()
st.subheader("Win Rate by Asset")
traded_rows = [r for r in rows if r.get("bot_action") != "NO_TRADE"]
if traded_rows:
    by_asset = defaultdict(lambda: {"correct": 0, "total": 0})
    for r in traded_rows:
        if r.get("correct") is not None:
            by_asset[r["asset"]]["total"] += 1
            if r["correct"]:
                by_asset[r["asset"]]["correct"] += 1
    wr_data = [(a, c["correct"] / c["total"] if c["total"] else 0)
               for a, c in sorted(by_asset.items())]
    if wr_data:
        fig_wr = go.Figure(go.Bar(
            x=[a for a, _ in wr_data],
            y=[w * 100 for _, w in wr_data],
            marker_color="#4a90d9",
        ))
        fig_wr.update_layout(
            title="Win Rate %",
            yaxis_title="Win Rate %",
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            height=250,
        )
        st.plotly_chart(fig_wr, use_container_width=True, config={"displayModeBar": False})
else:
    st.info("No traded windows yet.")

# Win rate by time of day (hour)
st.subheader("Win Rate by Hour (UTC)")
if traded_rows:
    by_hour = defaultdict(lambda: {"correct": 0, "total": 0})
    for r in traded_rows:
        wid = r.get("window_id", "")
        if isinstance(wid, str) and len(wid) >= 13:
            try:
                hour = int(wid[11:13])
                if r.get("correct") is not None:
                    by_hour[hour]["total"] += 1
                    if r["correct"]:
                        by_hour[hour]["correct"] += 1
            except (ValueError, IndexError):
                pass
    if by_hour:
        hours = sorted(by_hour.keys())
        wr_hour = [(h, by_hour[h]["correct"] / by_hour[h]["total"]
                    if by_hour[h]["total"] else 0) for h in hours]
        fig_hr = go.Figure(go.Bar(
            x=[f"{h:02d}:00" for h, _ in wr_hour],
            y=[w * 100 for _, w in wr_hour],
            marker_color="#00c864",
        ))
        fig_hr.update_layout(
            title="Win Rate % by Hour",
            yaxis_title="Win Rate %",
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            height=250,
        )
        st.plotly_chart(fig_hr, use_container_width=True, config={"displayModeBar": False})
    else:
        st.info("No hourly data yet.")
else:
    st.info("No traded windows yet.")

# Chart: actual price movement vs bot prediction
st.subheader("Actual vs Bot Prediction")
traded_with_spot = [r for r in rows if r.get("bot_action") != "NO_TRADE"
                    and r.get("price_to_beat") and r.get("exit_price")]
if traded_with_spot:
    labels = [f"{r['window']} {r['asset']}" for r in traded_with_spot[-30:]]
    actual_move = [(r["exit_price"] - r["price_to_beat"]) / (r["price_to_beat"] or 1) * 100
    bot_pred = [1 if r["bot_action"] == "BUY_YES" else -1 for r in traded_with_spot[-30:]]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=labels, y=actual_move, name="Actual % move",
        marker_color=["#00c864" if v >= 0 else "#dc3232" for v in actual_move],
    ))
    fig.add_trace(go.Scatter(
        x=labels, y=bot_pred, name="Bot (1=YES -1=NO)",
        mode="markers", marker=dict(size=10, symbol="diamond"),
    ))
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=350,
        barmode="overlay",
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
else:
    st.info("No traded windows with spot data yet.")

if auto_refresh:
    import time
    time.sleep(5)
    st.rerun()
