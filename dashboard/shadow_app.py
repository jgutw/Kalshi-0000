"""Read-only Shadow dashboard. It parses shadow_data and does not trade."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
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
        return "—"
    return f"${value:,.2f}"


def _pct(value):
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def _rate(numerator, denominator):
    if not denominator:
        return None
    return numerator / denominator


def _stamp(value):
    if not value:
        return "—"
    text = str(value).replace("+00:00", "Z").replace("T", " ")
    return text[:19] + "Z" if text.endswith("Z") or "+" not in text[19:] else text[:19] + "Z"


def _parse(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed


def _age(then, now):
    start = _parse(then)
    end = _parse(now)
    if start is None or end is None:
        return "—"
    seconds = max(0, int((end - start).total_seconds()))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _page() -> None:
    args = _args()
    st.set_page_config(page_title="Shadow dashboard", layout="wide")
    st.title("Shadow")
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
    _health(report)
    _funnel(report)
    if report["performance_withheld"]:
        st.error(INTEGRITY_FAILED)
        if report["lifecycle_chain_note"]:
            st.caption(report["lifecycle_chain_note"])
        st.caption("Lifecycle funnel counts, positions, gross P&L, and equity are withheld. Advisory counts above are not lifecycle evidence.")
    else:
        _portfolio(report)
    _research(report)


def _health(report) -> None:
    st.subheader("System health")
    st.caption("SHADOW / NO CAPITAL")
    st.caption("RESEARCH SETTLEMENT — NOT OFFICIAL KALSHI SETTLEMENT")
    st.caption("GROSS P&L — FEES NOT MODELED")
    session_id = report["session_id"] or "—"
    sha = report["code_sha"] or ""
    equity = report.get("equity") or {}
    perf = report.get("performance") or {}
    realized = equity.get("realized_equity") if equity and equity.get("available") else None
    gross = perf.get("gross_pnl") if perf else None
    opens = report["funnel"]["open"]
    cols = st.columns(4)
    cols[0].metric("Lifecycle", "OK" if report["lifecycle_chain_ok"] else "FAILED")
    cols[1].metric("Starting balance", _money(report["starting_balance"]))
    cols[2].metric("Realized gross equity", _money(realized) if not report["performance_withheld"] else "withheld")
    cols[3].metric("Gross P&L", _money(gross) if not report["performance_withheld"] else "withheld")
    cols = st.columns(4)
    cols[0].metric("Open positions", opens if opens is not None else "withheld")
    cols[1].metric("Heartbeat age", _age(report["last_heartbeat_utc"], report["now_utc"]))
    cols[2].metric("Runtime", _age(report["startup_utc"], report["shutdown_utc"] or report["now_utc"]))
    cols[3].metric("Loaded SHA", sha[:12] or "—")
    st.markdown(f"Session `{session_id}`")
    st.caption(
        f"Full SHA {sha or '—'} · startup {_stamp(report['startup_utc'])} · "
        f"now {_stamp(report['now_utc'])} · last heartbeat {_stamp(report['last_heartbeat_utc'])}"
    )
    st.caption(report["window_secs_basis"])
    if report["lifecycle_torn_tail"]:
        st.info(report["lifecycle_chain_note"])
    hearts = report["heartbeats"]
    if hearts:
        st.dataframe(
            [{"asset": asset, "ready": fields.get("is_ready"), "heartbeat": _stamp(fields.get("timestamp_utc")),
              "prices": fields.get("prices"), "trades": fields.get("trades"),
              "spot_sources": fields.get("spot_sources"), "spot_age_s": fields.get("spot_staleness_secs")}
             for asset, fields in sorted(hearts.items())],
            width="stretch", hide_index=True,
        )
    else:
        st.info("No heartbeats yet.")


def _funnel(report) -> None:
    st.subheader("Trading funnel")
    decisions = report["decisions"]
    funnel = report["funnel"]
    total = decisions["total"]
    buys = funnel["action_signals"]
    st.caption("Advisory decision counts stay visible when lifecycle performance is withheld.")
    top = st.columns(4)
    top[0].metric("Decisions", f"{total:,}")
    top[1].metric("WAIT", f"{decisions['WAIT']:,}")
    top[2].metric("BUY_YES", f"{decisions['BUY_YES']:,}")
    top[3].metric("BUY_NO", f"{decisions['BUY_NO']:,}")
    if report["performance_withheld"]:
        st.caption(f"BUY signals {buys:,} ({_pct(_rate(buys, total))} of decisions). Lifecycle stages are withheld.")
        return
    intended = funnel["intended"] or 0
    filled = funnel["filled"] or 0
    not_filled = funnel["not_filled"] or 0
    opens = funnel["open"] or 0
    closed = funnel["closed"] or 0
    stages = st.columns(6)
    for column, label, count, base in (
        (stages[0], "Decisions", total, None),
        (stages[1], "BUY signals", buys, total),
        (stages[2], "INTENDED", intended, buys),
        (stages[3], "FILLED", filled, intended),
        (stages[4], "OPEN", opens, filled),
        (stages[5], "CLOSED", closed, filled),
    ):
        column.metric(label, f"{count:,}", None if base is None else _pct(_rate(count, base)))
    st.caption(
        f"NOT FILLED {not_filled:,}. NO_FILL_NOT_MARKETABLE is a subset of NOT FILLED: "
        f"{funnel['NO_FILL_NOT_MARKETABLE']:,}, not an extra category."
    )
    st.caption("Fills per BUY signal " + _pct(funnel["fills_per_action_signal"]) + " · fills per intention " + _pct(funnel["fills_per_intention"]))
    reject_rows = [
        {"stage": "Before the journal (advisory)", "reason": reason, "count": count}
        for reason, count in sorted(funnel["advisory_rejections"].items())
    ]
    reject_rows.extend(
        {"stage": "Execution model", "reason": f"{row['disposition']} · {row['reason']}", "count": row["count"]}
        for row in (funnel["execution_reasons"] or [])
    )
    if reject_rows:
        st.dataframe(reject_rows, width="stretch", hide_index=True)
    else:
        st.info("No intention rejections or execution dispositions recorded.")


def _portfolio(report) -> None:
    st.subheader("Portfolio")
    perf = report["performance"]
    pcols = st.columns(4)
    pcols[0].metric("Gross P&L", _money(perf["gross_pnl"]))
    pcols[1].metric("Wins / losses", f"{perf['wins']} / {perf['losses']}")
    pcols[2].metric("Win rate", _pct(perf["win_rate"]))
    pcols[3].metric("Profit factor", "—" if perf["profit_factor"] is None else f"{perf['profit_factor']:.2f}")
    st.caption("Fees unknown. Net P&L is unavailable. This is not official Kalshi settlement.")
    equity = report["equity"]
    if not equity or not equity["available"]:
        st.info("Gross equity unavailable. It is not invented from a missing starting balance or an incomplete close.")
    else:
        st.caption(equity["basis"])
        ecols = st.columns(4)
        ecols[0].metric("Realized gross equity", _money(equity["realized_equity"]))
        ecols[1].metric("Cash gross", _money(equity["cash_gross"]))
        ecols[2].metric("Peak realized equity", _money(equity["peak_realized_equity"]))
        ecols[3].metric("Gross exposure", _money(equity["gross_exposure"]))
        dcols = st.columns(4)
        dcols[0].metric("Drawdown $", _money(equity["drawdown_usd"]))
        dcols[1].metric("Drawdown %", _pct(equity["drawdown_pct"]))
        dcols[2].metric("Cumulative gross return", _pct(equity["cumulative_return"]))
        dcols[3].metric("Concurrent open", equity["concurrent_open"])
        st.caption("equity_feeds_sizing = false. Sizing still uses the frozen starting balance.")
    st.markdown("**Open positions**")
    if report["open_positions"]:
        st.dataframe(report["open_positions"], width="stretch", hide_index=True)
    else:
        st.info("No open Shadow positions")
    st.markdown("**Closed positions**")
    if report["closed_positions"]:
        st.caption("Gross P&L only.")
        st.dataframe(report["closed_positions"], width="stretch", hide_index=True)
    else:
        st.info("No closed Shadow positions yet")


def _research(report) -> None:
    st.subheader("Research")
    total = report["decisions"]["total"] or 1
    table = [
        {"reason": row["reason"], "count": row["count"], "% decisions": _pct(row["pct_decisions"])}
        for row in report["decisions"]["reason_table"]
    ]
    st.dataframe(table, width="stretch", hide_index=True)
    with st.expander("Exact decision reasons"):
        exact = [
            {"reason": reason, "count": count, "% decisions": _pct(count / total)}
            for reason, count in sorted(report["decisions"]["reasons"].items(), key=lambda item: (-item[1], str(item[0])))
        ]
        st.dataframe(exact, width="stretch", hide_index=True)
    st.write("Decisions by asset", report["decisions"]["by_asset"])
    known = [row for row in report["incidents"] if row["class"] == "known_feed_exit"]
    other = [row for row in report["incidents"] if row["class"] != "known_feed_exit"]
    st.markdown("**Incidents**")
    st.caption(f"Known Binance feed stops: {len(known)}. Other incidents: {len(other)}. Nothing is hidden.")
    if other:
        st.dataframe(other, width="stretch", hide_index=True)
    else:
        st.info("No unexpected operational incidents.")
    with st.expander(f"Known Binance feed stops ({len(known)})"):
        st.dataframe(known, width="stretch", hide_index=True)


def main() -> None:
    page = st.navigation([st.Page(_page, title="Shadow", default=True)])
    page.run()


main()
