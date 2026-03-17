"""
Polymarket Bot Monitor — Professional Quant Workspace

Reads simulation + trades from polymarket_bot OR btc5m bot.
Bitchat-inspired dark theme: black background, neon green accent, monospace.
All formatting is adjustable via the sidebar.
"""

import json
import math
import time
import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Polymarket Monitor",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ═══════════════════════════════════════════════════════════════════════════════
# DATA SOURCES & CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

DATA_SOURCES = {
    "Polymarket Bot (multi-engine)": {
        "sim": "simulation.json",
        "log": "paper_trades.jsonl",
    },
    "BTC 5m Bot": {
        "sim": "btc5m_sim.json",
        "log": "btc5m_trades.jsonl",
    },
}

MAX_CONSECUTIVE_LOSSES = 3


def load_simulation(sim_path: str):
    """Load simulation state. Returns dict or None."""
    try:
        with open(sim_path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def load_trades(log_path: str, is_btc5m: bool = False):
    """Load trades from JSONL. Normalizes field names for dashboard compatibility."""
    trades = []
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                t = json.loads(line)
                if is_btc5m:
                    t = {
                        "ts": t.get("ts", ""),
                        "market_id": t.get("market", t.get("market_id", "")),
                        "entry_price": t.get("entry", 0),
                        "exit_price": t.get("exit", 0),
                        "pnl": t.get("pnl", 0),
                        "strategy": t.get("strategy", "btc5m"),
                    }
                trades.append(t)
    except FileNotFoundError:
        pass
    return trades


def normalize_sim(sim: dict, is_btc5m: bool) -> dict:
    """Unify field names across polymarket_bot and btc5m."""
    if not sim:
        return {}
    out = dict(sim)
    if is_btc5m:
        out["consecutive_losses"] = out.get("consec_losses", 0)
        out["volatility_regime"] = out.get("vol_regime", "normal")
    return out


def compute_sharpe(returns_history: list) -> float:
    if not returns_history or len(returns_history) < 2:
        return 0.0
    mean_ret = sum(returns_history) / len(returns_history)
    var = sum((r - mean_ret) ** 2 for r in returns_history) / (len(returns_history) - 1)
    std = math.sqrt(var) if var > 0 else 1e-9
    return (mean_ret / std) * math.sqrt(365 * 288) if std > 0 else 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# THEME & SIDEBAR
# ═══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown("### DATA SOURCE")
    data_source = st.radio(
        "Bot",
        options=list(DATA_SOURCES.keys()),
        index=1,
        label_visibility="collapsed",
    )
    cfg = DATA_SOURCES[data_source]
    is_btc5m = "btc5m" in cfg["sim"].lower()

    st.markdown("---")
    st.markdown("### THEME")
    bg_color = st.color_picker("Background", "#0e1117")
    accent_color = st.color_picker("Accent", "#39FF14")
    font_scale = st.slider("Font scale", 0.8, 1.4, 1.0, 0.05)
    font_family = st.selectbox(
        "Font",
        ["JetBrains Mono", "Fira Code", "IBM Plex Mono", "Source Code Pro"],
        index=0,
        key="font_family",
    )

    st.markdown("---")
    auto_refresh = st.checkbox("Auto-refresh every 10s", value=True, key="auto_refresh")

    if not is_btc5m:
        st.markdown("---")
        st.markdown("### STRATEGY FILTER")
        active_strategies = st.multiselect(
            "Active engines",
            options=["BTC Oracle-Lag", "Arbitrage", "Weather", "Events", "Market Making"],
            default=["BTC Oracle-Lag", "Arbitrage", "Weather", "Events", "Market Making"],
        )
        if st.button("Apply filter"):
            with open("config_override.json", "w") as f:
                json.dump({"active_strategies": active_strategies}, f)
            st.success("Applied")

# Custom CSS — Bitchat-style professional quant workspace
st.markdown(
    f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Fira+Code:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&family=Source+Code+Pro:wght@400;500;600&display=swap');

:root {{
    --bg: {bg_color};
    --accent: {accent_color};
    --font-scale: {font_scale};
    --font-mono: '{font_family}', 'Consolas', monospace;
}}

.stApp {{
    background: var(--bg) !important;
}}

/* Main text */
p, span, label, .stMarkdown {{
    color: var(--accent) !important;
    font-family: var(--font-mono) !important;
    font-size: calc(1rem * var(--font-scale)) !important;
}}

/* Headers */
h1, h2, h3 {{
    color: var(--accent) !important;
    font-family: var(--font-mono) !important;
    font-weight: 600 !important;
    letter-spacing: 0.05em !important;
    text-transform: uppercase !important;
}}

h1 {{ font-size: calc(1.75rem * var(--font-scale)) !important; }}
h2 {{ font-size: calc(1.25rem * var(--font-scale)) !important; }}
h3 {{ font-size: calc(1rem * var(--font-scale)) !important; }}

/* Metrics */
[data-testid="stMetricValue"],
[data-testid="stMetricDelta"] {{
    color: var(--accent) !important;
    font-family: var(--font-mono) !important;
    font-weight: 500 !important;
    font-size: calc(1.5rem * var(--font-scale)) !important;
}}

[data-testid="stMetricLabel"] {{
    color: var(--accent) !important;
    opacity: 0.85;
    font-family: var(--font-mono) !important;
}}

/* Dataframes */
[data-testid="stDataFrame"] {{
    background: rgba(0,0,0,0.3) !important;
    border: 1px solid var(--accent) !important;
    border-radius: 4px !important;
}}

[data-testid="stDataFrame"] th {{
    color: var(--accent) !important;
    font-family: var(--font-mono) !important;
    border-color: var(--accent) !important;
}}

[data-testid="stDataFrame"] td {{
    color: var(--accent) !important;
    font-family: var(--font-mono) !important;
}}

/* Progress bar */
.stProgress > div > div {{
    background: var(--accent) !important;
}}

/* Sidebar */
[data-testid="stSidebar"] {{
    background: rgba(0,0,0,0.5) !important;
    border-right: 1px solid var(--accent) !important;
}}

[data-testid="stSidebar"] .stMarkdown {{
    color: var(--accent) !important;
}}

/* Expanders, selects */
.stSelectbox, .stMultiSelect {{
    color: var(--accent) !important;
}}

/* Dividers */
hr {{
    border-color: var(--accent) !important;
    opacity: 0.5;
}}

/* Info / warning / success */
.stAlert {{
    background: rgba(0,0,0,0.4) !important;
    border: 1px solid var(--accent) !important;
    color: var(--accent) !important;
}}
</style>
""",
    unsafe_allow_html=True,
)

# ═══════════════════════════════════════════════════════════════════════════════
# LOAD DATA
# ═══════════════════════════════════════════════════════════════════════════════

sim_raw = load_simulation(cfg["sim"])
sim = normalize_sim(sim_raw, is_btc5m)
trades_log = load_trades(cfg["log"], is_btc5m)

if not sim:
    balance = 0.0
    starting_balance = 1000.0
    pnl = 0.0
    win_rate = 0.0
    sharpe = 0.0
    wins = 0
    losses = 0
else:
    balance = float(sim.get("balance", 0))
    starting_balance = float(sim.get("starting_balance", 1000))
    pnl = balance - starting_balance
    wins = int(sim.get("wins", 0))
    losses = int(sim.get("losses", 0))
    total = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0
    trades_list = sim.get("trades", [])
    returns_history = []
    for t in trades_list:
        entry = t.get("entry", t.get("entry_price", 0)) or 0.001
        exit_p = t.get("exit", t.get("exit_price", 0))
        shares = t.get("shares", 0)
        if entry * shares > 0:
            returns_history.append((exit_p - entry) * shares / (entry * shares))
    sharpe = compute_sharpe(returns_history)
    if not returns_history and trades_list:
        sharpe = float(sim.get("sharpe", 0)) or sharpe

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN CONTENT
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("# POLYMARKET BOT MONITOR")
st.markdown(f"*{data_source}*")
st.markdown("---")

if not sim:
    st.warning("Waiting for bot data. Run the bot to populate.")
    st.stop()

# ─── Section 1: Top metrics ───────────────────────────────────────────────────
st.markdown("## PERFORMANCE")

col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric("Balance", f"${balance:,.2f}")
with col2:
    st.metric("P&L", f"${pnl:+,.2f}", f"{(pnl/starting_balance*100):+.1f}%" if starting_balance > 0 else None)
with col3:
    st.metric("Win Rate", f"{win_rate:.1f}%")
with col4:
    st.metric("Sharpe", f"{sharpe:.2f}")

st.markdown("---")

# ─── Section 2: Risk Panel ───────────────────────────────────────────────────
st.markdown("## RISK PANEL")

peak = float(sim.get("peak_balance", balance))
drawdown_pct = ((peak - balance) / peak) if peak > 0 else 0.0
var_95 = sim.get("var_95")
var_95_str = f"{var_95:.1%}" if isinstance(var_95, (int, float)) else "—"
cvar_95 = sim.get("cvar_95")
cvar_95_str = f"{cvar_95:.1%}" if isinstance(cvar_95, (int, float)) else "—"
vol_regime = sim.get("volatility_regime", sim.get("vol_regime", "—"))
consec_losses = int(sim.get("consecutive_losses", sim.get("consec_losses", 0)))
circuit_breaker = "ACTIVE" if consec_losses >= MAX_CONSECUTIVE_LOSSES else "OK"

# Progress bar: full = healthy (1 - drawdown)
st.progress(max(0, 1.0 - min(1.0, drawdown_pct)), text=f"Drawdown: {drawdown_pct:.1%}")

r1, r2, r3, r4, r5 = st.columns(5)
with r1:
    st.write("**VaR 95%:**", var_95_str)
with r2:
    st.write("**CVaR 95%:**", cvar_95_str)
with r3:
    st.write("**Vol regime:**", vol_regime)
with r4:
    st.write("**Consec losses:**", consec_losses)
with r5:
    st.write("**Circuit breaker:**", circuit_breaker)

st.markdown("---")

# ─── Section 3: Equity & Trades ──────────────────────────────────────────────
st.markdown("## EQUITY & TRADES")

left_col, right_col = st.columns(2)

with left_col:
    st.write("**Equity curve**")
    if not trades_log:
        df_eq = pd.DataFrame({"balance": [starting_balance], "idx": [0]})
        st.line_chart(df_eq.set_index("idx"))
    else:
        cum_pnl = 0.0
        balances = [starting_balance]
        for t in trades_log:
            cum_pnl += float(t.get("pnl", 0))
            balances.append(starting_balance + cum_pnl)
        df_eq = pd.DataFrame({"balance": balances})
        st.line_chart(df_eq)

with right_col:
    st.write("**Recent trades**")
    if not trades_log:
        st.info("No trades yet")
    else:
        recent = trades_log[-20:][::-1]
        rows = []
        for t in recent:
            ts = (t.get("ts") or "")[:19]
            mid = str(t.get("market_id", ""))[:24]
            entry = float(t.get("entry_price", 0))
            exit_p = float(t.get("exit_price", 0))
            pnl_val = float(t.get("pnl", 0))
            strat = t.get("strategy", "")
            rows.append({"Time": ts, "Market": mid, "Entry": entry, "Exit": exit_p, "P&L": pnl_val, "Strategy": strat})
        df_trades = pd.DataFrame(rows)
        st.dataframe(df_trades, use_container_width=True)

st.markdown("---")

# ─── Section 4: Engine status (Polymarket Bot only) ────────────────────────────
if not is_btc5m:
    st.markdown("## ENGINE STATUS")
    last_scan = sim.get("last_scan", {})
    btc_bias = sim.get("btc_last_bias") or sim.get("last_bias_score") or "—"
    if isinstance(btc_bias, (int, float)):
        btc_bias = f"{btc_bias:.3f}"
    arb = last_scan.get("arb_signals", "—")
    weather = last_scan.get("weather_signals", "—")
    events = last_scan.get("event_signals", "—")
    e1, e2, e3, e4 = st.columns(4)
    with e1:
        st.metric("BTC Engine", "Bias", btc_bias)
    with e2:
        st.metric("Arbitrage", "Signals", str(arb))
    with e3:
        st.metric("Weather", "Signals", str(weather))
    with e4:
        st.metric("Events", "Signals", str(events))

    st.markdown("---")
    st.markdown("## ACTIVE MAKER QUOTES")
    active_quotes = sim.get("active_quotes", {})
    if not active_quotes:
        st.info("No active quotes")
    else:
        rows = []
        for mid, q in active_quotes.items():
            rows.append({
                "Question": str(q.get("question", ""))[:50],
                "Bid": q.get("bid", 0), "Ask": q.get("ask", 0), "Size": q.get("size", 0),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True)

# ─── Auto-refresh (every 10 seconds) ────────────────────────────────────────
if auto_refresh:
    time.sleep(10)
    st.rerun()
