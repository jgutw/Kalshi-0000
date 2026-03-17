"""
dashboard.py — Streamlit dashboard for the Kalshi multi-asset bot.

Usage:
  streamlit run dashboard.py

Reads:
  kalshi_sim.json        — live balance, wins, per-asset stats
  kalshi_trades.jsonl    — closed trade history
  kalshi_decisions.jsonl — per-tick decision log (last 500 rows)
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

# ─── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Kalshi Multi-Asset Bot",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Resolve paths: dashboard may run from kalshi_bot/ or project root
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
SIM_FILE       = _PROJECT_ROOT / "logs" / "kalshi_sim.json"
TRADE_LOG      = _PROJECT_ROOT / "logs" / "kalshi_trades.jsonl"
DECISION_LOG   = _PROJECT_ROOT / "logs" / "kalshi_decisions.jsonl"
REFRESH_SECS   = 5


# ─── Helpers ──────────────────────────────────────────────────────────────────

@st.cache_data(ttl=REFRESH_SECS)
def load_sim() -> dict:
    try:
        with open(SIM_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


@st.cache_data(ttl=REFRESH_SECS)
def load_trades(max_rows: int = 500) -> pd.DataFrame:
    try:
        rows = []
        with open(TRADE_LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line.strip()))
                except Exception:
                    pass
        df = pd.DataFrame(rows[-max_rows:])
        if "ts" in df.columns:
            df["ts"] = pd.to_datetime(df["ts"])
        return df
    except FileNotFoundError:
        return pd.DataFrame()


@st.cache_data(ttl=REFRESH_SECS)
def load_decisions(max_rows: int = 500) -> pd.DataFrame:
    try:
        rows = []
        with open(DECISION_LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line.strip()))
                except Exception:
                    pass
        df = pd.DataFrame(rows[-max_rows:])
        if "ts" in df.columns:
            df["ts"] = pd.to_datetime(df["ts"])
        return df
    except FileNotFoundError:
        return pd.DataFrame()


def fmt_pct(v) -> str:
    try:
        return f"{float(v):.1%}"
    except Exception:
        return "—"


def fmt_dollar(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return "—"


def _compute_win_rate(sim: dict) -> float:
    w, l = sim.get("wins", 0), sim.get("losses", 0)
    return w / (w + l) if (w + l) > 0 else 0.0


def _compute_sharpe(sim: dict) -> float:
    r = sim.get("returns_hist", [])
    if len(r) < 2:
        return 0.0
    arr = np.array(r)
    if np.std(arr) == 0:
        return 0.0
    return float(np.mean(arr) / np.std(arr))


# ─── Main layout ──────────────────────────────────────────────────────────────

st.title("📈 Kalshi Multi-Asset 15-Min Bot")
st.caption(f"Auto-refreshes every {REFRESH_SECS}s · {datetime.now().strftime('%H:%M:%S')}")

sim    = load_sim()
trades = load_trades()
decs   = load_decisions()

# ─── Top metrics row ──────────────────────────────────────────────────────────
c1, c2, c3, c4, c5, c6 = st.columns(6)
bal   = sim.get("balance",          0)
start = sim.get("starting_balance", 1)
pnl   = bal - start
wr    = _compute_win_rate(sim)
sharpe = _compute_sharpe(sim)

c1.metric("Balance",      fmt_dollar(bal))
c2.metric("P&L",          fmt_dollar(pnl),      delta=f"{(bal/start - 1):+.1%}" if start else "—")
c3.metric("Win Rate",     fmt_pct(wr))
c4.metric("Sharpe",       f"{sharpe:.2f}")
c5.metric("VaR 95%",      fmt_pct(sim.get("var_95", 0)))
c6.metric("Trades",       sim.get("total_trades", 0))

# Halt status
halted_at = sim.get("_halted_at")
consec    = sim.get("consec_losses", 0)
if halted_at:
    elapsed = time.time() - halted_at
    cooldown = 3600  # default 60 min
    remain   = max(0, cooldown - elapsed)
    st.warning(f"⛔ Circuit breaker active — {consec} consecutive losses. "
               f"Cooldown: {remain/60:.0f}m remaining.")
elif consec >= 2:
    st.info(f"⚠ {consec} consecutive losses (breaker fires at 3).")

st.divider()

# ─── Per-asset stats ──────────────────────────────────────────────────────────
asset_stats = sim.get("asset_stats", {})
if asset_stats:
    st.subheader("Per-Asset Performance")
    stat_rows = []
    for sym, s in asset_stats.items():
        w = s.get("wins",      0) if isinstance(s, dict) else s.wins
        l = s.get("losses",    0) if isinstance(s, dict) else s.losses
        p = s.get("total_pnl", 0) if isinstance(s, dict) else s.total_pnl
        wr_a = w / (w + l) if (w + l) > 0 else 0
        stat_rows.append({"Asset": sym, "Wins": w, "Losses": l,
                           "WR": f"{wr_a:.0%}", "P&L": f"${p:+.2f}"})
    st.dataframe(pd.DataFrame(stat_rows), use_container_width=True, hide_index=True)

st.divider()

# ─── Equity curve ─────────────────────────────────────────────────────────────
if not trades.empty and "balance" in trades.columns:
    st.subheader("Equity Curve")
    st.line_chart(trades.set_index("ts")["balance"] if "ts" in trades.columns
                  else trades["balance"])

# ─── Recent trades ────────────────────────────────────────────────────────────
st.subheader("Recent Trades")
if not trades.empty:
    show_cols = [c for c in ["ts", "asset", "side", "entry", "exit",
                              "contracts", "pnl", "balance"] if c in trades.columns]
    df_show = trades[show_cols].tail(30).sort_values("ts", ascending=False) \
              if "ts" in trades.columns else trades[show_cols].tail(30)
    # Color P&L
    def color_pnl(val):
        try:
            v = float(val)
            return "color: green" if v > 0 else ("color: red" if v < 0 else "")
        except Exception:
            return ""
    if "pnl" in df_show.columns:
        st.dataframe(df_show.style.map(color_pnl, subset=["pnl"]),
                     use_container_width=True, hide_index=True)
    else:
        st.dataframe(df_show, use_container_width=True, hide_index=True)
else:
    st.info("No trades yet.")

st.divider()

# ─── Decision log ─────────────────────────────────────────────────────────────
st.subheader("Decision Feed (last 50)")
if not decs.empty:
    show_d = [c for c in ["ts", "asset", "action", "reason", "p_real",
                           "p_market", "ev", "bias", "conviction",
                           "time_remaining"] if c in decs.columns]
    df_d = decs[show_d].tail(50).sort_values("ts", ascending=False) \
           if "ts" in decs.columns else decs[show_d].tail(50)
    st.dataframe(df_d, use_container_width=True, hide_index=True)

# ─── WAIT reason breakdown ────────────────────────────────────────────────────
if not decs.empty and "reason" in decs.columns and "action" in decs.columns:
    st.subheader("WAIT Reason Breakdown")
    wait_df = decs[decs["action"] == "WAIT"]
    if not wait_df.empty:
        reason_counts = (
            wait_df["reason"]
            .str.split("(").str[0]   # strip parameters
            .value_counts()
            .reset_index()
        )
        reason_counts.columns = ["Reason", "Count"]
        cols = st.columns([1, 2])
        cols[0].dataframe(reason_counts, use_container_width=True, hide_index=True)
        cols[1].bar_chart(reason_counts.set_index("Reason")["Count"])

# ─── Auto-refresh ─────────────────────────────────────────────────────────────
st.markdown(
    f"<meta http-equiv='refresh' content='{REFRESH_SECS}'>",
    unsafe_allow_html=True,
)
