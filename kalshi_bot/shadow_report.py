"""Read-only Shadow session report. Parses artifacts. Does not open the writer lock."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# Era 1 markets are 15 minutes. Session artifacts do not record this duration.
WINDOW_SECS = 900
WINDOW_SECS_BASIS = "Era 1 invariant of 900 seconds. Not read from session artifacts."
FULL = "SIMULATED_FULL_FILL"
GENESIS = "0" * 64
LIFECYCLE_ENVELOPE = {
    "schema_version", "mode", "shadow_session_id", "code_sha", "seq", "event_id",
    "timestamp_utc", "kind", "payload", "previous_hash", "sha256",
}
INTEGRITY_FAILED = "LIFECYCLE INTEGRITY FAILED — PERFORMANCE WITHHELD"


class SessionPathError(ValueError):
    """The requested session is not a directory inside the Shadow root."""


def resolve_session(root, session: str) -> Path:
    """Canonical session directory. Rejects paths that leave the Shadow root."""
    if not isinstance(session, str) or not session or session != session.strip():
        raise SessionPathError("Session name required")
    if session in {".", ".."} or any(part in {"", ".", ".."} for part in Path(session).parts):
        raise SessionPathError("Session escapes the Shadow root")
    if len(Path(session).parts) != 1:
        raise SessionPathError("Session must be a single directory under the Shadow root")
    root_path = Path(root).expanduser().resolve()
    session_dir = (root_path / session).resolve()
    if session_dir.parent != root_path:
        raise SessionPathError("Session escapes the Shadow root")
    return session_dir


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _read_lines(path: Path) -> tuple[list[str], int]:
    if not path.is_file():
        return [], 0
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    skipped = 0
    kept = []
    for line in lines:
        if not line.strip():
            continue
        try:
            json.loads(line)
        except (json.JSONDecodeError, ValueError):
            skipped += 1
            continue
        kept.append(line)
    return kept, skipped


def _loads(line: str) -> dict:
    value = json.loads(line)
    if type(value) is not dict:
        raise ValueError("JSON object required")
    return value


def _verify_lifecycle_record(event, previous: str) -> str:
    """Return the record hash, or raise ValueError if a complete record is untrustworthy."""
    if type(event) is not dict or set(event) != LIFECYCLE_ENVELOPE:
        raise ValueError("lifecycle envelope invalid")
    digest = event.pop("sha256")
    if type(digest) is not str or len(digest) != 64:
        raise ValueError("record hash mismatch")
    if event.get("previous_hash") != previous:
        raise ValueError("previous hash mismatch")
    if hashlib.sha256(canonical(event).encode("utf-8")).hexdigest() != digest:
        raise ValueError("record hash mismatch")
    event["sha256"] = digest
    return digest


def _chain(lines: list[str]) -> tuple[list[dict], bool, bool, str | None]:
    """Verify the hash chain.

    An incomplete JSON line at EOF is a torn tail: earlier records stay valid.
    A complete record that fails integrity, anywhere, fails the whole chain.
    The returned events are empty when integrity fails, so partial P&L cannot leak.
    """
    events = []
    previous = GENESIS
    for index, line in enumerate(lines):
        last = index == len(lines) - 1
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            if last:
                note = "chain verified through last complete record; trailing incomplete record observed"
                return events, True, True, f"{note}: {exc}"
            return [], False, False, f"record {index + 1}: incomplete JSON"
        try:
            previous = _verify_lifecycle_record(parsed, previous)
        except (ValueError, TypeError) as exc:
            return [], False, False, f"record {index + 1}: {exc}"
        events.append(parsed)
    return events, True, False, None


def _num(value):
    if type(value) in (int, float) and not isinstance(value, bool):
        return float(value)
    return None


def _replay(events: list[dict]) -> dict:
    orders = {}
    positions = {}
    executions = []
    for event in events:
        kind = event.get("kind")
        payload = event.get("payload")
        if type(payload) is not dict:
            continue
        if kind == "INTENDED":
            request = payload.get("request")
            if type(request) is dict:
                orders[payload.get("intended_order_id")] = request
        elif kind == "SIMULATED_EXECUTION":
            result = payload.get("result") if type(payload.get("result")) is dict else {}
            request = orders.get(payload.get("intended_order_id"), {})
            disposition = result.get("disposition")
            row = {
                "timestamp_utc": event.get("timestamp_utc"),
                "asset": request.get("asset"),
                "side": request.get("side"),
                "strategy": request.get("strategy"),
                "window_id_ts": request.get("window_id_ts"),
                "disposition": disposition,
                "reason": result.get("reason"),
                "quantity": result.get("filled_quantity") or 0,
                "price": result.get("simulated_price"),
                "price_to_beat": payload.get("price_to_beat"),
                "position_id": payload.get("position_id"),
            }
            executions.append(row)
            if disposition == FULL and payload.get("position_id"):
                positions[payload["position_id"]] = {
                    "position_id": payload["position_id"],
                    "asset": request.get("asset"),
                    "side": request.get("side"),
                    "strategy": request.get("strategy"),
                    "window_id_ts": request.get("window_id_ts"),
                    "ticker": request.get("ticker"),
                    "quantity": result.get("filled_quantity"),
                    "entry": result.get("simulated_price"),
                    "price_to_beat": payload.get("price_to_beat"),
                    "opened_utc": event.get("timestamp_utc"),
                    "status": "OPEN",
                    "close": None,
                }
        elif kind == "CLOSED" and payload.get("position_id") in positions:
            position = positions[payload["position_id"]]
            position["status"] = "CLOSED"
            position["close"] = payload
            position["closed_utc"] = payload.get("timestamp_utc") or event.get("timestamp_utc")
    return {"intended": len(orders), "executions": executions, "positions": list(positions.values())}


def _advisory(lines: list[str]) -> list[dict]:
    rows = []
    for line in lines:
        try:
            rows.append(_loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    return rows


def _performance(closed: list[dict]) -> dict:
    gross = [row for row in closed if _num(row.get("gross_pnl")) is not None]
    pnls = [_num(row["gross_pnl"]) for row in gross]
    winners = [value for value in pnls if value > 0]
    losers = [value for value in pnls if value < 0]
    decided = len(winners) + len(losers)
    by_asset = {}
    by_strategy = {}
    for row in gross:
        value = _num(row["gross_pnl"])
        by_asset[row.get("asset")] = by_asset.get(row.get("asset"), 0.0) + value
        by_strategy[row.get("strategy")] = by_strategy.get(row.get("strategy"), 0.0) + value
    return {
        "gross_pnl": sum(pnls),
        "closes": len(gross),
        "wins": len(winners),
        "losses": len(losers),
        "win_rate": (len(winners) / decided) if decided else None,
        "average_winner": (sum(winners) / len(winners)) if winners else None,
        "average_loser": (sum(losers) / len(losers)) if losers else None,
        "profit_factor": (sum(winners) / abs(sum(losers))) if losers else None,
        "by_asset": by_asset,
        "by_strategy": by_strategy,
    }


def _equity(starting, closed_rows, open_rows) -> dict:
    """Gross equity from the lifecycle. Fees stay unknown. Open positions are not marked.

    Realized equity is starting balance plus closed gross P&L. An open premium
    is still inside that number at cost. Cash is realized equity minus open premium.
    This ledger does not change the sizer. The engine keeps using the frozen
    starting balance for Kelly and the position cap.
    """
    blank = {
        "available": False,
        "starting_gross_equity": None,
        "realized_pnl": None,
        "realized_equity": None,
        "cash_gross": None,
        "gross_exposure": None,
        "exposure_pct_of_starting": None,
        "concurrent_open": None,
        "peak_realized_equity": None,
        "drawdown_usd": None,
        "drawdown_pct": None,
        "cumulative_return": None,
        "fees": None,
        "net_equity": None,
        "equity_feeds_sizing": False,
        "basis": "unavailable",
    }
    if type(starting) not in (int, float) or isinstance(starting, bool) or starting <= 0:
        blank["basis"] = "starting balance missing; equity not invented"
        return blank
    start = float(starting)
    ordered = []
    for row in closed_rows:
        pnl = _num(row.get("gross_pnl"))
        if pnl is None:
            blank["basis"] = "closed position missing gross P&L; equity withheld"
            return blank
        ordered.append((str(row.get("closed_utc") or ""), pnl))
    running = start
    peak = start
    for _when, pnl in sorted(ordered, key=lambda item: item[0]):
        running += pnl
        if running > peak:
            peak = running
    realized_pnl = running - start
    premium = 0.0
    for row in open_rows:
        qty = _num(row.get("quantity"))
        entry = _num(row.get("entry"))
        if qty is None or entry is None:
            blank["basis"] = "open position missing premium; equity withheld"
            return blank
        premium += qty * entry
    drawdown = peak - running
    return {
        "available": True,
        "starting_gross_equity": start,
        "realized_pnl": realized_pnl,
        "realized_equity": running,
        "cash_gross": running - premium,
        "gross_exposure": premium,
        "exposure_pct_of_starting": premium / start,
        "concurrent_open": len(open_rows),
        "peak_realized_equity": peak,
        "drawdown_usd": drawdown,
        "drawdown_pct": drawdown / peak if peak > 0 else None,
        "cumulative_return": (running / start) - 1.0,
        "fees": None,
        "net_equity": None,
        "equity_feeds_sizing": False,
        "basis": (
            "Gross realized equity. Open positions carried at cost, not marked. "
            "Fees unknown, so net equity is unavailable. "
            "Sizing still uses the frozen starting balance."
        ),
    }


def _seconds_to_boundary(window_id, now: datetime) -> float | None:
    if type(window_id) is not int:
        return None
    return window_id + WINDOW_SECS - now.timestamp()


def analyze(session_dir: Path, now: datetime | None = None) -> dict:
    """Build a report from one session directory. Reads files and returns."""
    session_dir = Path(session_dir)
    now = now or datetime.now(timezone.utc)
    metadata = {}
    meta_path = session_dir / "session.json"
    if meta_path.is_file():
        try:
            metadata = _loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError, OSError):
            metadata = {}
    decision_lines, bad_decisions = _read_lines(session_dir / "decisions.jsonl")
    operational_lines, bad_operational = _read_lines(session_dir / "operational_events.jsonl")
    life_path = session_dir / "lifecycle.jsonl"
    life_lines = []
    if life_path.is_file():
        life_lines = [line for line in life_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    events, chain_ok, torn_tail, chain_note = _chain(life_lines)
    replay = _replay(events) if chain_ok else {"intended": 0, "executions": [], "positions": []}
    decisions = _advisory(decision_lines)
    operational = _advisory(operational_lines)
    actions = Counter(row.get("action") for row in decisions)
    reasons = Counter(row.get("reason") for row in decisions)
    grouped = Counter()
    for reason, count in reasons.items():
        if isinstance(reason, str) and (reason.startswith("edge(") or reason.startswith("p_base_near_50")):
            key = reason.split("(", 1)[0]
        else:
            key = reason
        grouped[key] += count
    by_asset_decisions = Counter(row.get("asset") for row in decisions)
    hearts = [row for row in operational if row.get("event") == "heartbeat"]
    latest = {}
    for row in hearts:
        latest[row.get("asset")] = row
    incidents = [
        {"event": row.get("event"), "timestamp_utc": row.get("timestamp_utc"),
         "asset": row.get("asset"), "feed": row.get("feed"), "error": row.get("error")}
        for row in operational
        if row.get("event") in {
            "feed_stopped", "feed_failure", "price_feed_error", "uncaught_loop_error",
            "settlement_error", "settlement_waiting", "simulation_rejected", "journal_intend_failed",
        }
    ]
    closed_rows = []
    open_rows = []
    for position in replay["positions"]:
        close = position.get("close") or {}
        view = {
            "asset": position.get("asset"),
            "side": position.get("side"),
            "strategy": position.get("strategy"),
            "window_id_ts": position.get("window_id_ts"),
            "quantity": position.get("quantity"),
            "entry": position.get("entry"),
            "price_to_beat": position.get("price_to_beat"),
            "opened_utc": position.get("opened_utc"),
            "seconds_to_boundary": _seconds_to_boundary(position.get("window_id_ts"), now),
            "status": position.get("status"),
        }
        if position.get("status") == "OPEN":
            open_rows.append(view)
        else:
            view.update({
                "side_payoff": close.get("side_payoff"),
                "gross_pnl": close.get("gross_pnl"),
                "fees": close.get("fees"),
                "net_pnl": close.get("net_pnl"),
                "observed_spot": close.get("observed_spot"),
                "settlement_method": close.get("settlement_method"),
                "yes_settled": close.get("yes_settled"),
                "closed_utc": position.get("closed_utc"),
            })
            closed_rows.append(view)
    dispositions = Counter(row.get("disposition") for row in replay["executions"])
    starting = next((row.get("starting_balance") for row in operational if row.get("event") == "starting_balance"), None)
    startup = metadata.get("startup_utc")
    shutdown = next((row.get("timestamp_utc") for row in reversed(operational) if row.get("event") == "shutdown"), None)
    last_heartbeat = hearts[-1]["timestamp_utc"] if hearts else None
    actionable = actions.get("BUY_YES", 0) + actions.get("BUY_NO", 0)
    filled = dispositions.get(FULL, 0) if chain_ok else None
    intended = replay["intended"] if chain_ok else None
    not_filled = sum(count for name, count in dispositions.items() if name != FULL) if chain_ok else None
    perf = _performance(closed_rows) if chain_ok else None
    equity = _equity(starting, closed_rows, open_rows) if chain_ok else None
    if not chain_ok:
        open_rows = None
        closed_rows = None
    return {
        "session_id": metadata.get("shadow_session_id"),
        "session_tag": metadata.get("session_tag"),
        "code_sha": metadata.get("code_sha"),
        "config_sha256": metadata.get("config_sha256"),
        "mode": metadata.get("mode"),
        "startup_utc": startup,
        "shutdown_utc": shutdown,
        "starting_balance": starting,
        "now_utc": now.isoformat(),
        "last_heartbeat_utc": last_heartbeat,
        "lifecycle_chain_ok": chain_ok,
        "lifecycle_torn_tail": torn_tail,
        "lifecycle_chain_note": chain_note,
        "performance_withheld": not chain_ok,
        "window_secs": WINDOW_SECS,
        "window_secs_basis": WINDOW_SECS_BASIS,
        "malformed_advisory_lines": bad_decisions + bad_operational,
        "decisions": {
            "total": len(decisions),
            "WAIT": actions.get("WAIT", 0),
            "BUY_YES": actions.get("BUY_YES", 0),
            "BUY_NO": actions.get("BUY_NO", 0),
            "reasons": dict(reasons),
            "reason_groups": dict(grouped),
            "by_asset": dict(by_asset_decisions),
        },
        "funnel": {
            "action_signals": actionable,
            "intended": intended,
            "filled": filled,
            "not_filled": not_filled,
            "NO_FILL_NOT_MARKETABLE": dispositions.get("NO_FILL_NOT_MARKETABLE", 0) if chain_ok else None,
            "not_marketable_subset_of_not_filled": True,
            "dispositions": dict(dispositions) if chain_ok else None,
            "open": len(open_rows) if chain_ok else None,
            "closed": len(closed_rows) if chain_ok else None,
            "fills_per_action_signal": (filled / actionable) if chain_ok and actionable and filled is not None else None,
            "fills_per_intention": (filled / intended) if chain_ok and intended else None,
        },
        "heartbeats": {
            asset: {
                "timestamp_utc": row.get("timestamp_utc"),
                "is_ready": row.get("is_ready"),
                "prices": row.get("prices"),
                "trades": row.get("trades"),
                "trade_age_secs": row.get("trade_age_secs"),
                "spot_sources": row.get("spot_sources"),
                "spot_staleness_secs": row.get("spot_staleness_secs"),
                "spot_mid": row.get("spot_mid"),
                "window_id_ts": row.get("window_id_ts"),
                "coinbase_trades": row.get("coinbase_trades"),
                "coinbase_books": row.get("coinbase_books"),
                "coinbase_mids": row.get("coinbase_mids"),
                "binance_trades": row.get("binance_trades"),
                "binance_books": row.get("binance_books"),
                "binance_mids": row.get("binance_mids"),
                "okx_trades": row.get("okx_trades"),
                "okx_books": row.get("okx_books"),
                "okx_mids": row.get("okx_mids"),
                "kraken_mids": row.get("kraken_mids"),
                "gemini_mids": row.get("gemini_mids"),
            }
            for asset, row in latest.items()
        },
        "incidents": incidents,
        "open_positions": open_rows,
        "closed_positions": closed_rows,
        "performance": perf,
        "equity": equity,
        "labels": [
            "SHADOW / NO CAPITAL",
            "RESEARCH SETTLEMENT — NOT OFFICIAL KALSHI SETTLEMENT",
            "GROSS P&L — FEES NOT MODELED",
        ],
    }


def render(report: dict) -> str:
    decisions = report["decisions"]
    funnel = report["funnel"]
    lines = [
        *report["labels"],
        f"session={report['session_id']} tag={report['session_tag']} sha={report['code_sha']}",
        f"startup={report['startup_utc']} shutdown={report['shutdown_utc']} last_heartbeat={report['last_heartbeat_utc']}",
        (
            f"starting_balance_metadata={report['starting_balance']} "
            f"chain_ok={report['lifecycle_chain_ok']} torn_tail={report['lifecycle_torn_tail']} "
            f"malformed_advisory={report['malformed_advisory_lines']}"
        ),
        f"window_secs={report['window_secs']} ({report['window_secs_basis']})",
        f"advisory decisions total={decisions['total']} WAIT={decisions['WAIT']} BUY_YES={decisions['BUY_YES']} BUY_NO={decisions['BUY_NO']}",
        "reasons: " + ", ".join(f"{key}={value}" for key, value in sorted(decisions["reason_groups"].items())),
        f"incidents={len(report['incidents'])}",
    ]
    if report["performance_withheld"]:
        lines.append(INTEGRITY_FAILED)
    else:
        perf = report["performance"]
        lines.append(
            "funnel "
            f"signals={funnel['action_signals']} intended={funnel['intended']} "
            f"filled={funnel['filled']} not_filled={funnel['not_filled']} "
            f"not_marketable_subset={funnel['NO_FILL_NOT_MARKETABLE']} "
            f"open={funnel['open']} closed={funnel['closed']}"
        )
        lines.append("NO_FILL_NOT_MARKETABLE is a subset of not_filled")
        lines.append(
            "gross_pnl="
            f"{perf['gross_pnl']} wins={perf['wins']} losses={perf['losses']} "
            f"win_rate={perf['win_rate']} profit_factor={perf['profit_factor']} "
            "fees=null net_pnl=null"
        )
        equity = report["equity"]
        if equity and equity["available"]:
            lines.append(
                "equity "
                f"start={equity['starting_gross_equity']} realized={equity['realized_equity']} "
                f"cash={equity['cash_gross']} exposure={equity['gross_exposure']} "
                f"concurrent={equity['concurrent_open']} peak={equity['peak_realized_equity']} "
                f"drawdown_usd={equity['drawdown_usd']} drawdown_pct={equity['drawdown_pct']} "
                f"return={equity['cumulative_return']} fees=null net_equity=null "
                "equity_feeds_sizing=false"
            )
        else:
            lines.append("equity unavailable")
    if report["lifecycle_chain_note"]:
        lines.append(f"chain_note={report['lifecycle_chain_note']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Shadow session report. Does not trade or write.")
    parser.add_argument("--root", default="shadow_data")
    parser.add_argument("--session", required=True)
    args = parser.parse_args(argv)
    try:
        session_dir = resolve_session(args.root, args.session)
    except SessionPathError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    report = analyze(session_dir)
    print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
