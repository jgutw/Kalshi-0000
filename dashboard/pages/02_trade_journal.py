"""
Trade Journal — Daily review of closed trades and loss predictor analysis.
"""

from __future__ import annotations

import time
from collections import defaultdict

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard.components.sidebar import render_sidebar
from dashboard.data.schemas import TradeEvent
from dashboard.data.state_store import StateStore

auto_refresh = render_sidebar()
store = StateStore()
trades = store.get_trades()
portfolio = store.get_portfolio()

if not trades:
    st.info("No trades yet.")
    if auto_refresh:
        time.sleep(5)
        st.rerun()
    st.stop()

# Row 1: Summary metrics
wins = [t for t in trades if t.pnl > 0]
losses = [t for t in trades if t.pnl <= 0]
total_pnl = sum(t.pnl for t in trades)
best = max(trades, key=lambda x: x.pnl)
worst = min(trades, key=lambda x: x.pnl)

c1, c2, c3, c4, c5 = st.columns(5)
with c1:
    st.metric("Total Trades", len(trades))
with c2:
    wr = len(wins) / len(trades) if trades else 0
    st.metric("Win Rate", f"{wr:.1%}")
with c3:
    st.metric("Total P&L", f"${total_pnl:+,.2f}")
with c4:
    st.metric("Best Trade", f"${best.pnl:+,.2f}")
with c5:
    st.metric("Worst Trade", f"${worst.pnl:+,.2f}")

# Row 2: P&L by asset and by strategy
st.divider()
col_a, col_b = st.columns(2)
with col_a:
    by_asset = defaultdict(float)
    for t in trades:
        by_asset[t.asset] += t.pnl
    fig_a = go.Figure(go.Bar(
        x=list(by_asset.keys()),
        y=list(by_asset.values()),
        marker_color=["#00c864" if v >= 0 else "#dc3232" for v in by_asset.values()],
    ))
    fig_a.update_layout(
        title="P&L by Asset",
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=250,
    )
    st.plotly_chart(fig_a, use_container_width=True, config={"displayModeBar": False})
with col_b:
    by_strat = defaultdict(float)
    for t in trades:
        by_strat[t.strategy] += t.pnl
    fig_b = go.Figure(go.Bar(
        x=list(by_strat.keys()),
        y=list(by_strat.values()),
        marker_color=["#00c864" if v >= 0 else "#dc3232" for v in by_strat.values()],
    ))
    fig_b.update_layout(
        title="P&L by Strategy",
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=250,
    )
    st.plotly_chart(fig_b, use_container_width=True, config={"displayModeBar": False})

# Row 3: Equity curve
st.divider()
balances = []
cum = portfolio.starting_balance
for t in sorted(trades, key=lambda x: x.ts):
    cum += t.pnl
    balances.append(cum)
fig_eq = go.Figure()
fig_eq.add_trace(go.Scatter(y=balances, mode="lines", name="Balance", line=dict(color="#4a90d9")))
fig_eq.add_hline(y=portfolio.starting_balance, line_dash="dash", line_color="rgba(150,150,150,0.6)")
fig_eq.update_layout(
    title="Equity Curve",
    template="plotly_dark",
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    height=250,
)
st.plotly_chart(fig_eq, use_container_width=True, config={"displayModeBar": False})

# Row 4: Full trade table
st.divider()
rows = []
for t in trades:
    rows.append({
        "ts": t.ts,
        "asset": t.asset,
        "side": t.side,
        "entry": f"{t.entry:.3f}",
        "exit": f"{t.exit:.3f}",
        "contracts": t.contracts,
        "pnl": t.pnl,
        "strategy": t.strategy,
        "reason": t.reason,
    })
df = pd.DataFrame(rows)
df = df.sort_values("ts", ascending=False)
st.dataframe(df, use_container_width=True, hide_index=True)

# Row 5: Loss predictor analysis
st.divider()
col_l, col_r = st.columns(2)
with col_l:
    st.markdown("**Win rate by strategy**")
    strat_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    for t in trades:
        strat_stats[t.strategy]["wins" if t.pnl > 0 else "losses"] += 1
        strat_stats[t.strategy]["pnl"] += t.pnl
    strat_rows = []
    for s, v in strat_stats.items():
        total = v["wins"] + v["losses"]
        wr = v["wins"] / total if total else 0
        strat_rows.append({"strategy": s, "wins": v["wins"], "losses": v["losses"], "win_rate": f"{wr:.1%}", "avg_pnl": f"${v['pnl']/total:+.2f}" if total else "—"})
    st.dataframe(pd.DataFrame(strat_rows), use_container_width=True, hide_index=True)
with col_r:
    st.markdown("**P&L by time-to-expiry at entry**")
    buckets = {"0-60s": [], "60-180s": [], "180-450s": [], "450-900s": []}
    for t in trades:
        tr = t.time_remaining_at_entry
        if tr <= 60:
            buckets["0-60s"].append(t.pnl)
        elif tr <= 180:
            buckets["60-180s"].append(t.pnl)
        elif tr <= 450:
            buckets["180-450s"].append(t.pnl)
        else:
            buckets["450-900s"].append(t.pnl)
    bucket_sums = {k: sum(v) if v else 0 for k, v in buckets.items()}
    fig_bucket = go.Figure(go.Bar(
        x=list(bucket_sums.keys()),
        y=list(bucket_sums.values()),
        marker_color=["#00c864" if v >= 0 else "#dc3232" for v in bucket_sums.values()],
    ))
    fig_bucket.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=220,
    )
    st.plotly_chart(fig_bucket, use_container_width=True, config={"displayModeBar": False})

# Row 6: Loss predictor breakdown
st.divider()
st.markdown("**Conditions at Entry — Win vs Loss Comparison**")
win_trades = [t for t in trades if t.pnl > 0]
loss_trades = [t for t in trades if t.pnl <= 0]

def avg(attr, lst):
    vals = [getattr(t, attr) for t in lst if getattr(t, attr) is not None]
    return sum(vals) / len(vals) if vals else 0

fields = [
    ("kalshi_quote_age_at_entry", "Quote Age (s)"),
    ("spot_confidence_at_entry", "Spot Confidence"),
    ("lag_confidence_at_entry", "Lag Confidence"),
    ("dislocation_at_entry", "Dislocation"),
    ("confidence_weighted_mispricing_at_entry", "CWM"),
]
win_avgs = []
loss_avgs = []
for fname, flabel in fields:
    wv = [getattr(t, fname) for t in win_trades if getattr(t, fname) is not None]
    lv = [getattr(t, fname) for t in loss_trades if getattr(t, fname) is not None]
    win_avgs.append(sum(wv) / len(wv) if wv else 0)
    loss_avgs.append(sum(lv) / len(lv) if lv else 0)

fig_comp = go.Figure()
labels = [f[1] for f in fields]
fig_comp.add_trace(go.Bar(name="Winners", x=labels, y=win_avgs, marker_color="#00c864"))
fig_comp.add_trace(go.Bar(name="Losers", x=labels, y=loss_avgs, marker_color="#dc3232"))
fig_comp.update_layout(
    barmode="group",
    template="plotly_dark",
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    height=300,
)
st.plotly_chart(fig_comp, use_container_width=True, config={"displayModeBar": False})
st.caption("Higher quote age, lower confidence, and higher dislocation at entry correlate with losing trades.")

if auto_refresh:
    time.sleep(5)
    st.rerun()
