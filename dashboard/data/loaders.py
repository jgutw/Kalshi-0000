"""
dashboard/data/loaders.py — Load from kalshi_sim.json, kalshi_trades.jsonl, kalshi_decisions.jsonl.

Maps existing field names into normalized schemas. Falls back to mock data if files missing/empty.
Does not modify any file on disk.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .mock_data import (
    generate_decisions,
    generate_portfolio,
    generate_state_snapshots,
    generate_trades,
)
from .schemas import DecisionEvent, PortfolioSnapshot, StateSnapshot, TradeEvent

ROOT = Path(__file__).resolve().parent.parent.parent
LOGS_DIR = ROOT / "logs"
SIM_PATH = LOGS_DIR / "kalshi_sim.json"
TRADES_PATH = LOGS_DIR / "kalshi_trades.jsonl"
DECISIONS_PATH = LOGS_DIR / "kalshi_decisions.jsonl"


def _safe_float(val, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_dict(val) -> dict:
    return val if isinstance(val, dict) else {}


def load_portfolio() -> PortfolioSnapshot:
    """Load from kalshi_sim.json or return mock."""
    try:
        if not SIM_PATH.exists() or SIM_PATH.stat().st_size == 0:
            return generate_portfolio()
        with open(SIM_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return generate_portfolio()

    balance = _safe_float(data.get("balance"), 1000.0)
    start = _safe_float(data.get("starting_balance"), 1000.0)
    peak = _safe_float(data.get("peak_balance"), balance)
    total = int(data.get("total_trades", 0))
    wins = int(data.get("wins", 0))
    losses = int(data.get("losses", 0))
    wr = wins / total if total > 0 else 0.0
    sharpe = _safe_float(data.get("sharpe"), 0.0)
    var_95 = _safe_float(data.get("var_95"), 0.0)
    consec = int(data.get("consec_losses", 0))
    halted = data.get("_halted_at") is not None or consec >= 3
    halt_reason = "consec_loss_cooldown" if halted else ""

    asset_stats = {}
    for k, v in _safe_dict(data.get("asset_stats")).items():
        if isinstance(v, dict):
            asset_stats[k] = {
                "wins": v.get("wins", 0),
                "losses": v.get("losses", 0),
                "total_pnl": _safe_float(v.get("total_pnl"), 0.0),
            }
        else:
            asset_stats[k] = {"wins": 0, "losses": 0, "total_pnl": 0.0}

    return PortfolioSnapshot(
        ts=datetime.now(timezone.utc).isoformat(),
        balance=balance,
        starting_balance=start,
        peak_balance=peak,
        total_trades=total,
        wins=wins,
        losses=losses,
        win_rate=wr,
        sharpe=sharpe,
        var_95=var_95,
        consec_losses=consec,
        halt_state=halted,
        halt_reason=halt_reason,
        asset_stats=asset_stats,
    )


def load_trades() -> List[TradeEvent]:
    """Load from kalshi_trades.jsonl or return mock."""
    events: List[TradeEvent] = []
    try:
        if not TRADES_PATH.exists() or TRADES_PATH.stat().st_size == 0:
            return generate_trades()
        with open(TRADES_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    t = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entry = _safe_float(t.get("entry"), 0.5)
                exit_val = _safe_float(t.get("exit"), 0.0)
                side = "yes" if entry > 0.5 else "no"
                events.append(TradeEvent(
                    ts=t.get("ts", ""),
                    asset=t.get("asset", "BTC"),
                    ticker=t.get("ticker", ""),
                    side=side,
                    entry=entry,
                    exit=exit_val,
                    contracts=int(t.get("contracts", 0)),
                    pnl=_safe_float(t.get("pnl"), 0.0),
                    balance=_safe_float(t.get("balance"), 1000.0),
                    win_rate=_safe_float(t.get("win_rate"), 0.0),
                    strategy=t.get("strategy", ""),
                    reason=t.get("reason", ""),
                    time_remaining_at_entry=_safe_float(t.get("time_remaining_at_entry"), 0.0),
                    kalshi_spread_at_entry=_safe_float(t.get("kalshi_spread_at_entry"), 0.0),
                    kalshi_quote_age_at_entry=_safe_float(t.get("kalshi_quote_age_at_entry"), 0.0),
                    spot_confidence_at_entry=_safe_float(t.get("spot_confidence_at_entry"), 0.0),
                    lag_confidence_at_entry=_safe_float(t.get("lag_confidence_at_entry"), 0.0),
                    dislocation_at_entry=_safe_float(t.get("dislocation_at_entry"), 0.0),
                    confidence_weighted_mispricing_at_entry=(
                        t.get("confidence_weighted_mispricing_at_entry")
                        if t.get("confidence_weighted_mispricing_at_entry") is not None
                        else None
                    ),
                ))
    except IOError:
        return generate_trades()

    if not events:
        return generate_trades()
    return events


def load_decisions(asset: Optional[str] = None, last_n: int = 500) -> List[DecisionEvent]:
    """Load from kalshi_decisions.jsonl or return mock."""
    events: List[DecisionEvent] = []
    try:
        if not DECISIONS_PATH.exists() or DECISIONS_PATH.stat().st_size == 0:
            all_events = generate_decisions()
        else:
            with open(DECISIONS_PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rf = _safe_dict(d.get("raw_features"))
                    for key in ["obi", "ofi_hawkes", "microprice_dev",
                               "trade_sign_autocorr", "lag_signal", "response_gap"]:
                        if key not in rf:
                            rf[key] = 0.0
                    wid = d.get("window_id_ts")
                    if wid is None:
                        wid = d.get("window_id", 0)
                    if isinstance(wid, str):
                        wid = 0
                    else:
                        wid = int(wid) if wid else 0
                    events.append(DecisionEvent(
                        ts=d.get("ts", ""),
                        asset=d.get("asset", "BTC"),
                        window_id=wid,
                        action=d.get("action", "WAIT"),
                        reason=d.get("reason", ""),
                        p_base=d.get("p_base"),
                        p_real=d.get("p_real"),
                        p_market=d.get("p_market"),
                        ev=d.get("ev"),
                        z_threshold=_safe_float(d.get("z_threshold"), 0.0),
                        lag_confidence=_safe_float(d.get("lag_confidence"), 0.0),
                        spot_confidence=_safe_float(d.get("spot_confidence"), 0.0),
                        confidence_weighted_mispricing=d.get("confidence_weighted_mispricing"),
                        alpha_micro=_safe_float(d.get("alpha_micro"), 0.0),
                        strategy=d.get("strategy", ""),
                        diagnostics=_safe_dict(d.get("diagnostics")),
                        raw_features=rf,
                        time_remaining_secs=_safe_float(d.get("time_remaining"), 0.0),
                        spot_now=d.get("spot_now"),
                        spot_start=d.get("spot_start"),
                        kalshi_quote_age_secs=_safe_float(d.get("kalshi_quote_age_secs"), 0.0),
                        kalshi_spread=_safe_float(d.get("kalshi_spread"), 0.0),
                    ))
            all_events = events if events else generate_decisions()
        if asset:
            all_events = [e for e in all_events if e.asset == asset]
        return all_events[-last_n:]
    except IOError:
        ev = generate_decisions()
        if asset:
            ev = [e for e in ev if e.asset == asset]
        return ev[-last_n:]


def load_latest_snapshots() -> dict:
    """Build StateSnapshot per asset from decisions + portfolio. Falls back to mock."""
    try:
        portfolio = load_portfolio()
        decisions = load_decisions(last_n=200)
        if not decisions:
            return generate_state_snapshots()

        snapshots = {}
        for asset in ["BTC", "ETH", "SOL", "XRP"]:
            asset_dec = [d for d in decisions if d.asset == asset]
            if not asset_dec:
                dec = None
            else:
                dec = asset_dec[-1]
            if dec:
                snapshots[asset] = StateSnapshot(
                    ts=dec.ts,
                    asset=asset,
                    window_id=dec.window_id,
                    time_remaining_secs=dec.time_remaining_secs,
                    spot_now=dec.spot_now,
                    spot_start=dec.spot_start,
                    synthetic_confidence=dec.spot_confidence,
                    dislocation=0.001,
                    z_threshold=dec.z_threshold,
                    p_base=dec.p_base,
                    alpha_micro=dec.alpha_micro,
                    p_real=dec.p_real,
                    p_market=dec.p_market,
                    mispricing_base=dec.p_base - dec.p_market if dec.p_base and dec.p_market else None,
                    confidence_weighted_mispricing=dec.confidence_weighted_mispricing,
                    lag_confidence=dec.lag_confidence,
                    spot_confidence=dec.spot_confidence,
                    active_strategy=dec.strategy,
                    router_action=dec.action,
                    wait_reason=dec.reason if dec.action == "WAIT" else "",
                    open_position_side=None,
                    open_position_entry=None,
                    open_position_contracts=None,
                    unrealized_pnl=None,
                    halt_state=portfolio.halt_state,
                    halt_reason=portfolio.halt_reason,
                    kalshi_quote_age_secs=dec.kalshi_quote_age_secs,
                    kalshi_spread=dec.kalshi_spread,
                )
            else:
                mock = generate_state_snapshots()
                snapshots[asset] = mock.get(asset, mock["BTC"])
        return snapshots
    except Exception:
        return generate_state_snapshots()


def load_window_performance() -> List[Dict[str, Any]]:
    """Load window-level performance from trades + decisions. Group by window_id."""
    rows: List[Dict[str, Any]] = []
    trades_by_key: Dict[str, dict] = {}
    decisions_by_key: Dict[str, dict] = {}

    def _window_display(wid: str) -> str:
        """Convert '2026-03-17 09:15' to '09:15-09:30'."""
        if not wid or len(wid) < 16:
            return wid or "?"
        try:
            dt = datetime.strptime(wid[:16], "%Y-%m-%d %H:%M")
            from datetime import timedelta
            end = dt + timedelta(minutes=15)
            return f"{dt.strftime('%H:%M')}-{end.strftime('%H:%M')}"
        except ValueError:
            return wid

    # Load trades (with window_id, price_to_beat, exit_spot)
    try:
        if TRADES_PATH.exists() and TRADES_PATH.stat().st_size > 0:
            with open(TRADES_PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        t = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    wid = t.get("window_id", "")
                    if not wid and t.get("ts"):
                        ts = t.get("ts", "")
                        if "T" in ts:
                            try:
                                dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
                                min_floor = (dt.minute // 15) * 15
                                dt = dt.replace(minute=min_floor, second=0, microsecond=0)
                                wid = dt.strftime("%Y-%m-%d %H:%M")
                            except (ValueError, TypeError):
                                pass
                    asset = t.get("asset", "BTC")
                    if wid:
                        key = f"{wid}|{asset}"
                        ptb = _safe_float(t.get("price_to_beat"), 0.0)
                        exit_spot = _safe_float(t.get("exit_spot"), 0.0)
                        entry = _safe_float(t.get("entry"), 0.5)
                        pnl = _safe_float(t.get("pnl"), 0.0)
                        side = "yes" if entry > 0.5 else "no"
                        bot_action = "BUY_YES" if side == "yes" else "BUY_NO"
                        actual = "YES" if exit_spot > ptb else "NO"
                        pred_yes = side == "yes"
                        actual_yes = exit_spot > ptb
                        correct = pred_yes == actual_yes
                        trades_by_key[key] = {
                            "window": _window_display(wid),
                            "window_id": wid,
                            "asset": asset,
                            "price_to_beat": ptb,
                            "exit_price": exit_spot,
                            "actual_outcome": actual,
                            "bot_action": bot_action,
                            "entry_price": entry,
                            "pnl": pnl,
                            "correct": correct,
                        }
    except IOError:
        pass

    # Load decisions to fill NO_TRADE rows
    try:
        if DECISIONS_PATH.exists() and DECISIONS_PATH.stat().st_size > 0:
            with open(DECISIONS_PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    wid = d.get("window_id", "")
                    wid_ts = d.get("window_id_ts")
                    if (not wid or isinstance(wid, int)) and isinstance(wid_ts, (int, float)) and wid_ts:
                        wid = datetime.fromtimestamp(int(wid_ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                    if not wid or not isinstance(wid, str):
                        continue
                    asset = d.get("asset", "BTC")
                    key = f"{wid}|{asset}"
                    if key in trades_by_key:
                        continue
                    decisions_by_key[key] = d
    except IOError:
        pass

    # Build rows: trades first, then NO_TRADE from decisions
    seen = set()
    for key, t in trades_by_key.items():
        rows.append(t)
        seen.add(key)
    for key, d in decisions_by_key.items():
        if key in seen:
            continue
        wid = d.get("window_id", "")
        wid_ts = d.get("window_id_ts")
        if (not wid or isinstance(wid, int)) and isinstance(wid_ts, (int, float)) and wid_ts:
            wid = datetime.fromtimestamp(int(wid_ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        ptb = _safe_float(d.get("spot_start"), 0.0)
        exit_spot = _safe_float(d.get("spot_now"), 0.0)
        action = d.get("action", "WAIT")
        bot_action = "NO_TRADE" if action == "WAIT" else action
        actual = "YES" if exit_spot > ptb else "NO" if ptb > 0 else "?"
        correct = None if bot_action == "NO_TRADE" else (bot_action == "BUY_YES" and actual == "YES") or (bot_action == "BUY_NO" and actual == "NO")
        rows.append({
            "window": _window_display(wid) if isinstance(wid, str) else "?",
            "window_id": wid,
            "asset": d.get("asset", "BTC"),
            "price_to_beat": ptb,
            "exit_price": exit_spot,
            "actual_outcome": actual,
            "bot_action": bot_action,
            "entry_price": None,
            "pnl": 0.0,
            "correct": correct,
        })

    # Sort by window_id desc (most recent first)
    rows.sort(key=lambda r: (r.get("window_id", ""), r.get("asset", "")), reverse=True)
    return rows
