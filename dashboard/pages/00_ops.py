"""
Ops — Live multi-asset grid (default landing page).

Dense horizontal table for fast scanning. No Plotly — low latency.
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import streamlit as st

from dashboard.components.sidebar import render_sidebar
from dashboard.data.state_store import StateStore

auto_refresh = render_sidebar(refresh_secs=5)
store = StateStore()
portfolio = store.get_portfolio()
snapshots = store.get_latest_snapshots()

try:
    from kalshi_bot.config import all_asset_symbols, enabled_asset_symbols
    assets = all_asset_symbols()
    enabled = set(enabled_asset_symbols())
except Exception:
    assets = list(snapshots.keys())
    enabled = set(assets)


def _fmt_spot(x) -> str:
    if x is None:
        return "—"
    ax = abs(float(x))
    if ax >= 1000:
        return f"{float(x):,.1f}"
    if ax >= 10:
        return f"{float(x):,.2f}"
    if ax >= 1:
        return f"{float(x):,.4f}"
    return f"{float(x):,.6f}"


def _feed_label(conf: float, reason: str, has_spot: bool) -> str:
    if (reason or "").startswith("disabled"):
        return "OFF"
    # Already in a trade — feed was good enough to enter
    if (reason or "") == "position_open":
        return "OK"
    if conf >= 0.6:
        return "OK"
    if conf >= 0.3:
        return "WEAK"
    # Many WAIT stubs omit spot_confidence; spot updating still means live
    if has_spot and conf <= 0:
        return "—"
    return "BAD"


st.title("Ops")
st.caption("Live book — scan left→right. Detail lives on Asset / Windows.")

# Compact portfolio strip
equity = portfolio.total_equity or (portfolio.balance + portfolio.vault_balance)
pnl = equity - portfolio.starting_balance
pnl_pct = (pnl / portfolio.starting_balance * 100) if portfolio.starting_balance else 0.0
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Trading", f"${portfolio.balance:,.0f}")
c2.metric("Vault", f"${portfolio.vault_balance:,.0f}")
c3.metric("Equity", f"${equity:,.0f}", f"{pnl_pct:+.1f}%")
c4.metric("WR", f"{portfolio.win_rate:.0%}")
c5.metric("Trades", f"{portfolio.total_trades}")
if portfolio.halt_state:
    c6.error("HALT")
else:
    c6.success("OK")
if portfolio.halt_state and portfolio.halt_reason:
    st.error(portfolio.halt_reason)

# Log age
_dec = Path(__file__).resolve().parents[2] / "logs" / "kalshi_decisions.jsonl"
if _dec.exists():
    age = time.time() - _dec.stat().st_mtime
    if age > 30:
        st.warning(f"Logs stale ({age:.0f}s) — bot may be stopped")
    else:
        st.caption(f"Logs {age:.0f}s ago")
else:
    st.info("No live decision log yet (idle or mock).")

rows = []
for asset in assets:
    s = snapshots.get(asset)
    if not s:
        rows.append({
            "Asset": asset,
            "Feed": "—",
            "T left": "—",
            "Spot": "—",
            "vs open": "—",
            "Action": "—",
            "Reason": "no data",
            "Strategy": "—",
            "p_mkt": "—",
            "p_base": "—",
            "Lag": "—",
            "Spot conf": "—",
            "CWM": "—",
            "Spread": "—",
            "Pos": "—",
        })
        continue

    spot_now = s.spot_now
    spot_start = s.spot_start
    if spot_now is not None and spot_start is not None and spot_start > 0:
        vs = (spot_now - spot_start) / spot_start * 10_000
        vs_s = f"{vs:+.1f}bps"
    else:
        vs_s = "—"

    t_left = max(0, int(s.time_remaining_secs or 0))
    action = s.router_action or "—"
    reason = (s.wait_reason or "")[:48] if action == "WAIT" else ""
    if asset not in enabled:
        action = "OFF"
        reason = "disabled"

    cwm = s.confidence_weighted_mispricing
    pos = "—"
    if s.open_position_side:
        pos = f"{s.open_position_side.upper()}×{s.open_position_contracts or 0}"

    conf = float(s.spot_confidence or s.synthetic_confidence or 0)
    rows.append({
        "Asset": asset,
        "Feed": _feed_label(conf, s.wait_reason or "", spot_now is not None),
        "T left": f"{t_left // 60}:{t_left % 60:02d}",
        "Spot": _fmt_spot(spot_now),
        "vs open": vs_s,
        "Action": action,
        "Reason": reason,
        "Strategy": (s.active_strategy or "—")[:16],
        "p_mkt": f"{s.p_market * 100:.0f}¢" if s.p_market is not None else "—",
        "p_base": f"{s.p_base * 100:.0f}¢" if s.p_base is not None else "—",
        "Lag": f"{s.lag_confidence:.2f}",
        "Spot conf": f"{conf:.2f}" if conf > 0 else "—",
        "CWM": f"{cwm:+.3f}" if cwm is not None else "—",
        "Spread": f"{s.kalshi_spread:.2f}" if s.kalshi_spread else "—",
        "Pos": pos,
    })

df = pd.DataFrame(rows)
st.dataframe(
    df,
    use_container_width=True,
    hide_index=True,
    height=min(420, 48 + 36 * max(len(df), 1)),
    column_config={
        "Asset": st.column_config.TextColumn(width="small"),
        "Feed": st.column_config.TextColumn(width="small"),
        "Action": st.column_config.TextColumn(width="small"),
        "Reason": st.column_config.TextColumn(width="medium"),
        "Pos": st.column_config.TextColumn(width="small"),
    },
)

st.caption(
    "Feed: OK ≥0.6 venues · WEAK/BAD low synth confidence · "
    "p_mkt / p_base are Kalshi YES in cents (69¢ = 0.69) · "
    "CWM = confidence-weighted mispricing · Pos = open side×contracts"
)

if auto_refresh:
    time.sleep(5)
    st.rerun()
