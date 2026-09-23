"""Read-only Shadow dashboard. It parses shadow_data and does not trade."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kalshi_bot.shadow_report import INTEGRITY_FAILED, SessionPathError, analyze, resolve_session


def _args():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", default="shadow_data")
    parser.add_argument("--session", default="shadow_era1b_001")
    known, _ = parser.parse_known_args()
    return known


def _money(value):
    if value is None:
        return "undefined"
    return f"{value:.4f}"


def main() -> None:
    args = _args()
    st.set_page_config(page_title="Shadow dashboard", layout="wide")
    st.title("Shadow dashboard")
    for label in (
        "SHADOW / NO CAPITAL",
        "RESEARCH SETTLEMENT — NOT OFFICIAL KALSHI SETTLEMENT",
        "GROSS P&L — FEES NOT MODELED",
    ):
        st.caption(label)
    root = st.sidebar.text_input("Root", args.root)
    session = st.sidebar.text_input("Session", args.session)
    if st.sidebar.button("Refresh"):
        st.rerun()
    try:
        path = resolve_session(root, session)
    except SessionPathError as exc:
        st.error(str(exc))
        return
    if not (path / "session.json").is_file():
        st.error(f"No session metadata at {path}")
        return
    report = analyze(path)
    session_cols = st.columns(4)
    session_cols[0].metric("Session", report["session_id"] or "—")
    session_cols[1].metric("SHA", (report["code_sha"] or "")[:12] or "—")
    session_cols[2].metric("Starting balance (metadata)", report["starting_balance"] if report["starting_balance"] is not None else "—")
    session_cols[3].metric("Last heartbeat", report["last_heartbeat_utc"] or "—")
    st.caption(
        f"Startup {report['startup_utc']} · now {report['now_utc']} · "
        f"chain ok {report['lifecycle_chain_ok']}"
    )
    st.caption(report["window_secs_basis"])
    if report["lifecycle_torn_tail"]:
        st.info(report["lifecycle_chain_note"])
    elif report["lifecycle_chain_note"] and report["lifecycle_chain_ok"]:
        st.caption(report["lifecycle_chain_note"])

    st.subheader("Data health")
    hearts = report["heartbeats"]
    if hearts:
        st.dataframe(
            [
                {"asset": asset, **fields}
                for asset, fields in sorted(hearts.items())
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No heartbeats yet.")

    st.subheader("Advisory decisions")
    st.caption("Decision counts come from the advisory log. They stay visible when lifecycle performance is withheld.")
    decisions = report["decisions"]
    dcols = st.columns(4)
    dcols[0].metric("Total", decisions["total"])
    dcols[1].metric("WAIT", decisions["WAIT"])
    dcols[2].metric("BUY_YES", decisions["BUY_YES"])
    dcols[3].metric("BUY_NO", decisions["BUY_NO"])
    st.write("Reason groups", decisions["reason_groups"])
    st.write("By asset", decisions["by_asset"])

    if report["performance_withheld"]:
        st.error(INTEGRITY_FAILED)
        if report["lifecycle_chain_note"]:
            st.caption(report["lifecycle_chain_note"])
        st.caption("Lifecycle funnel, positions, and gross P&L are withheld. Advisory decisions and feed health above are not lifecycle evidence.")
    else:
        st.subheader("Execution funnel")
        funnel = report["funnel"]
        st.caption("NO_FILL_NOT_MARKETABLE is a subset of not filled, not a separate additive category.")
        fcols = st.columns(6)
        for column, (label, key) in zip(fcols, (
            ("Signals", "action_signals"),
            ("Intended", "intended"),
            ("Filled", "filled"),
            ("Not filled", "not_filled"),
            ("Not marketable (subset)", "NO_FILL_NOT_MARKETABLE"),
            ("Open / closed", None),
        )):
            if key is None:
                column.metric("Open / closed", f"{funnel['open']} / {funnel['closed']}")
            else:
                column.metric(label, funnel[key])
        st.caption(
            f"Fills/signals {_money(funnel['fills_per_action_signal'])} · "
            f"fills/intentions {_money(funnel['fills_per_intention'])}"
        )
        st.write("Dispositions", funnel["dispositions"])

        st.subheader("Open positions")
        st.dataframe(report["open_positions"], width="stretch", hide_index=True)
        st.subheader("Closed positions")
        st.caption("Gross P&L only. Fees are not modeled. This is not net profit and not official Kalshi settlement.")
        st.dataframe(report["closed_positions"], width="stretch", hide_index=True)

        st.subheader("Gross performance")
        perf = report["performance"]
        pcols = st.columns(4)
        pcols[0].metric("Gross P&L", _money(perf["gross_pnl"]))
        pcols[1].metric("Wins / losses", f"{perf['wins']} / {perf['losses']}")
        pcols[2].metric("Win rate", _money(perf["win_rate"]))
        pcols[3].metric("Profit factor", _money(perf["profit_factor"]))
        st.write("Gross P&L by asset", perf["by_asset"])
        st.write("Gross P&L by strategy", perf["by_strategy"])
        st.caption(f"Average winner {_money(perf['average_winner'])} · average loser {_money(perf['average_loser'])}")

    st.subheader("Operational incidents")
    st.dataframe(report["incidents"], width="stretch", hide_index=True)


main()
