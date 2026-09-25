"""
Trade Journal — Closed trades with risked $ and P&L.
"""

from __future__ import annotations

import time
from collections import defaultdict

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard.components.mode_banner import render_production_banner
from dashboard.components.sidebar import render_sidebar
from dashboard.data.state_store import StateStore

render_production_banner()
from dashboard.data.loaders import load_working_fills

auto_refresh = render_sidebar(refresh_secs=8)
store = StateStore()
trades = store.get_trades()
portfolio = store.get_portfolio()

st.title("Production journal")
st.caption("LOCAL PRODUCTION LOG. P&L is the logged pnl field. Fee treatment is unknown unless a fee field is present.")
st.caption("Closed trades — risked premium vs realized P&L. Working fills appear above the table.")

working = load_working_fills()
if working:
    st.subheader("Working fills (this session, not settled)")
    wrows = []
    for p in working:
        wrows.append({
            "Asset": p.get("asset") or "",
            "Side": str(p.get("side") or "").upper(),
            "Entry": p.get("entry"),
            "Contracts": p.get("contracts"),
            "Risked $": p.get("amount_usdc"),
            "Ticker": p.get("ticker") or "",
        })
    st.dataframe(pd.DataFrame(wrows), use_container_width=True, hide_index=True)


if not trades:
    st.info("No closed trades yet.")
    if auto_refresh:
        time.sleep(8)
        st.rerun()
    st.stop()

wins = [t for t in trades if t.pnl > 0]
losses = [t for t in trades if t.pnl < 0]
scratches = [t for t in trades if t.pnl == 0]
total_pnl = sum(t.pnl for t in trades)
total_risked = sum(t.amount_usdc for t in trades)
best = max(trades, key=lambda x: x.pnl)
worst = min(trades, key=lambda x: x.pnl)

c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
c1.metric("Trades", len(trades))
c2.metric("Wins", len(wins))
c3.metric("Losses", len(losses))
c4.metric("Scratches", len(scratches))
c5.metric("Logged P&L", f"${total_pnl:+,.2f}")
c6.metric("Best", f"${best.pnl:+,.2f}")
c7.metric("Worst", f"${worst.pnl:+,.2f}")
st.caption("Logged P&L uses the local pnl field. Fee treatment: unknown.")
st.caption(f"Risked (sum, local): ${total_risked:,.0f}")

# Main table first (practical)
rows = []
for t in sorted(trades, key=lambda x: x.ts, reverse=True):
    roi = (t.pnl / t.amount_usdc) if t.amount_usdc else None
    rows.append({
        "Time": t.ts[:19].replace("T", " ") if t.ts else "",
        "Asset": t.asset,
        "Side": (t.side or "").upper(),
        "Entry": round(t.entry, 3),
        "Exit": round(t.exit, 3),
        "Contracts": t.contracts,
        "Risked $": round(t.amount_usdc, 2),
        "P&L $": round(t.pnl, 2),
        "ROI": round(roi, 3) if roi is not None else None,
        "Strategy": t.strategy or "—",
        "Reason": (t.reason or "")[:40],
    })

df = pd.DataFrame(rows)
st.dataframe(
    df,
    use_container_width=True,
    hide_index=True,
    height=min(480, 48 + 35 * min(len(df), 12)),
    column_config={
        "Entry": st.column_config.NumberColumn(format="%.3f"),
        "Exit": st.column_config.NumberColumn(format="%.3f"),
        "Risked $": st.column_config.NumberColumn(format="$%.2f"),
        "P&L $": st.column_config.NumberColumn(format="$%+.2f"),
        "ROI": st.column_config.NumberColumn(format="%+.1%"),
    },
)

# One clean equity curve (fixed height, readable axes)
st.divider()
st.subheader("Equity curve")
balances = []
xs = []
cum = portfolio.starting_balance
for t in sorted(trades, key=lambda x: x.ts):
    cum += t.pnl
    balances.append(cum)
    xs.append(t.ts[:16].replace("T", " ") if t.ts else "")

ymin = min(min(balances), portfolio.starting_balance) * 0.98
ymax = max(max(balances), portfolio.starting_balance) * 1.02
fig = go.Figure()
fig.add_trace(go.Scatter(
    x=xs,
    y=balances,
    mode="lines+markers",
    name="Equity",
    line=dict(color="#4a90d9", width=2),
    marker=dict(size=5),
    hovertemplate="%{x}<br>$%{y:,.2f}<extra></extra>",
))
fig.add_hline(
    y=portfolio.starting_balance,
    line_dash="dash",
    line_color="rgba(180,180,180,0.7)",
    annotation_text="start",
    annotation_position="top left",
)
fig.update_layout(
    template="plotly_dark",
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    height=280,
    margin=dict(l=48, r=16, t=24, b=48),
    yaxis=dict(title="Balance ($)", range=[ymin, ymax], tickformat="$,.0f", zeroline=False),
    xaxis=dict(title="", tickangle=-30, nticks=min(10, len(xs))),
    showlegend=False,
)
st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

# Compact P&L by asset (single chart, readable)
st.subheader("P&L by asset")
by_asset = defaultdict(float)
risk_asset = defaultdict(float)
for t in trades:
    by_asset[t.asset] += t.pnl
    risk_asset[t.asset] += t.amount_usdc
assets_sorted = sorted(by_asset.keys())
vals = [by_asset[a] for a in assets_sorted]
fig_a = go.Figure(go.Bar(
    x=assets_sorted,
    y=vals,
    marker_color=["#00c864" if v >= 0 else "#dc3232" for v in vals],
    text=[f"${v:+.0f}" for v in vals],
    textposition="outside",
    hovertemplate="%{x}<br>P&L $%{y:+.2f}<br>Risked $%{customdata:,.0f}<extra></extra>",
    customdata=[risk_asset[a] for a in assets_sorted],
))
pad = max(abs(v) for v in vals) * 0.25 if vals else 10
fig_a.update_layout(
    template="plotly_dark",
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    height=260,
    margin=dict(l=48, r=16, t=16, b=40),
    yaxis=dict(
        title="P&L ($)",
        range=[min(0, min(vals) - pad), max(0, max(vals) + pad)],
        tickformat="$,.0f",
        zeroline=True,
        zerolinecolor="rgba(150,150,150,0.5)",
    ),
    xaxis=dict(title=""),
    showlegend=False,
)
st.plotly_chart(fig_a, use_container_width=True, config={"displayModeBar": False})

if auto_refresh:
    time.sleep(8)
    st.rerun()
