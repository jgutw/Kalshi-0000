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
SESSIONS_DIR = ROOT / "sessions"
SIM_PATH = LOGS_DIR / "kalshi_sim.json"
TRADES_PATH = LOGS_DIR / "kalshi_trades.jsonl"
DECISIONS_PATH = LOGS_DIR / "kalshi_decisions.jsonl"
WINDOWS_PATH = LOGS_DIR / "kalshi_windows.jsonl"
META_PATH = LOGS_DIR / "session_meta.json"


def _safe_float(val, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_dict(val) -> dict:
    return val if isinstance(val, dict) else {}


def _live_logs_present() -> bool:
    """True when sim + decisions exist (same live criteria as StateStore)."""
    if not SIM_PATH.exists() or SIM_PATH.stat().st_size == 0:
        return False
    if not DECISIONS_PATH.exists() or DECISIONS_PATH.stat().st_size == 0:
        return False
    return True


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
    # Prefer persisted sharpe if present; else leave 0 (sim uses returns_hist)
    sharpe = _safe_float(data.get("sharpe"), 0.0)
    if sharpe == 0.0:
        rh = data.get("returns_hist") or []
        if isinstance(rh, list) and len(rh) >= 2:
            try:
                import numpy as np
                r = np.array(rh, dtype=float)
                if np.std(r) > 0:
                    sharpe = float(np.mean(r) / np.std(r))
            except Exception:
                pass
    var_95 = _safe_float(data.get("var_95"), 0.0)
    consec = int(data.get("consec_losses", 0))

    # Mirror kalshi_bot.sim_state.is_halted() so the banner shows the real reason.
    # (Old logic labeled every halt as consec_loss_cooldown and used consec>=3.)
    try:
        from kalshi_bot.config import cfg as _cfg
        max_dd = float(getattr(_cfg, "MAX_DRAWDOWN_PCT", 0.25))
        dd_enabled = bool(getattr(_cfg, "DRAWDOWN_HALT_ENABLED", True))
        dd_equity = bool(getattr(_cfg, "DRAWDOWN_USE_EQUITY", True))
        max_daily = float(getattr(_cfg, "MAX_DAILY_LOSS_PCT", 0.40))
        max_consec = int(getattr(_cfg, "MAX_CONSEC_LOSSES", 8))
    except Exception:
        max_dd, dd_enabled, dd_equity, max_daily, max_consec = 0.25, True, True, 0.40, 8

    daily_start = _safe_float(data.get("daily_start"), start)
    daily_dd = ((daily_start - balance) / daily_start) if daily_start > 0 else 0.0
    vault_tmp = _safe_float(data.get("vault_balance"), 0.0)
    equity_tmp = _safe_float(data.get("total_equity"), balance + vault_tmp)
    peak_eq = _safe_float(data.get("peak_equity"), equity_tmp)
    if dd_equity:
        peak_dd = ((peak_eq - equity_tmp) / peak_eq) if peak_eq > 0 else 0.0
        dd_label = "max_drawdown_equity"
    else:
        peak_dd = ((peak - balance) / peak) if peak > 0 else 0.0
        dd_label = "max_drawdown_trading"

    halted = False
    halt_reason = ""
    if daily_dd >= max_daily:
        halted, halt_reason = True, f"daily_loss {daily_dd:.1%}"
    elif dd_enabled and peak_dd >= max_dd:
        halted, halt_reason = True, f"{dd_label} {peak_dd:.1%}>={max_dd:.0%}"
    elif data.get("_halted_at") is not None or consec >= max_consec:
        halted, halt_reason = True, "consec_loss_cooldown"
    else:
        by_asset = data.get("consec_losses_by_asset") or {}
        halted_assets = data.get("_halted_at_by_asset") or {}
        if isinstance(by_asset, dict):
            for sym, streak in by_asset.items():
                if int(streak or 0) >= max_consec or (isinstance(halted_assets, dict) and sym in halted_assets):
                    halted, halt_reason = True, f"consec_loss_cooldown[{sym}]"
                    break

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

    vault = _safe_float(data.get("vault_balance"), 0.0)
    equity = _safe_float(data.get("total_equity"), balance + vault)
    skimmable = max(0.0, balance - start)

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
        vault_balance=vault,
        total_equity=equity,
        skimmable_profit=skimmable,
    )


def load_trades() -> List[TradeEvent]:
    """Load closed trades from kalshi_trades.jsonl.

    In live mode with no closed trades yet, return [] — never inject mock trades
    (that made the journal disagree with the $500 paper balance).
    """
    events: List[TradeEvent] = []
    live = _live_logs_present()
    try:
        if not TRADES_PATH.exists() or TRADES_PATH.stat().st_size == 0:
            return [] if live else generate_trades()
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
                contracts = int(t.get("contracts", 0) or 0)
                side = (t.get("side") or ("yes" if entry > 0.5 else "no")).lower()
                amount = t.get("amount_usdc")
                if amount is None:
                    amount = entry * contracts
                events.append(TradeEvent(
                    ts=t.get("ts", ""),
                    asset=t.get("asset", "BTC"),
                    ticker=t.get("ticker", ""),
                    side=side,
                    entry=entry,
                    exit=exit_val,
                    contracts=contracts,
                    amount_usdc=_safe_float(amount, entry * contracts),
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
        return [] if live else generate_trades()

    if not events:
        return [] if live else generate_trades()
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


def _enabled_assets() -> list:
    try:
        from kalshi_bot.config import enabled_asset_symbols
        return enabled_asset_symbols()
    except Exception:
        return ["BTC", "ETH", "SOL"]


def _dashboard_assets() -> list:
    """All configured symbols (enabled + disabled) for Mission Control cards."""
    try:
        from kalshi_bot.config import all_asset_symbols
        return all_asset_symbols()
    except Exception:
        return ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "NEAR", "ZEC"]


def _placeholder_snapshot(asset: str, portfolio: PortfolioSnapshot, reason: str) -> StateSnapshot:
    """Empty card for disabled / no-feed assets — never inject BTC-priced mock spots."""
    return StateSnapshot(
        ts=datetime.now(timezone.utc).isoformat(),
        asset=asset,
        window_id=0,
        time_remaining_secs=0.0,
        spot_now=None,
        spot_start=None,
        synthetic_confidence=0.0,
        dislocation=0.0,
        z_threshold=0.0,
        p_base=None,
        alpha_micro=0.0,
        p_real=None,
        p_market=None,
        mispricing_base=None,
        confidence_weighted_mispricing=None,
        lag_confidence=0.0,
        spot_confidence=0.0,
        active_strategy="—",
        router_action="WAIT",
        wait_reason=reason,
        open_position_side=None,
        open_position_entry=None,
        open_position_contracts=None,
        unrealized_pnl=None,
        halt_state=False,
        halt_reason="",
        kalshi_quote_age_secs=0.0,
        kalshi_spread=0.0,
    )


def _load_open_positions_map() -> dict:
    """asset → open position dict from logs/open_positions.json."""
    path = LOGS_DIR / "open_positions.json"
    out: dict = {}
    try:
        if not path.exists() or path.stat().st_size == 0:
            return out
        data = json.loads(path.read_text(encoding="utf-8"))
        for p in data.get("positions") or []:
            a = p.get("asset")
            if a:
                out[a] = p
    except (OSError, json.JSONDecodeError):
        return {}
    return out


def load_latest_snapshots() -> dict:
    """Build StateSnapshot per asset from decisions + portfolio. Falls back to mock."""
    try:
        portfolio = load_portfolio()
        decisions = load_decisions(last_n=200)
        open_map = _load_open_positions_map()
        live = _live_logs_present()
        enabled = set(_enabled_assets())
        if not decisions and not live:
            return generate_state_snapshots()

        snapshots = {}
        for asset in _dashboard_assets():
            if asset not in enabled:
                snapshots[asset] = _placeholder_snapshot(
                    asset, portfolio, "disabled_in_config (not trading)"
                )
                continue
            asset_dec = [d for d in decisions if d.asset == asset]
            dec = asset_dec[-1] if asset_dec else None
            pos = open_map.get(asset)
            if dec:
                # Prefer last non-null spot_confidence in recent ticks (WAIT stubs often omit it)
                spot_conf = dec.spot_confidence
                if not spot_conf:
                    for d in reversed(asset_dec[-30:]):
                        if d.spot_confidence:
                            spot_conf = d.spot_confidence
                            break
                snapshots[asset] = StateSnapshot(
                    ts=dec.ts,
                    asset=asset,
                    window_id=dec.window_id,
                    time_remaining_secs=dec.time_remaining_secs,
                    spot_now=dec.spot_now,
                    spot_start=dec.spot_start,
                    synthetic_confidence=spot_conf or 0.0,
                    dislocation=0.001,
                    z_threshold=dec.z_threshold,
                    p_base=dec.p_base,
                    alpha_micro=dec.alpha_micro,
                    p_real=dec.p_real,
                    p_market=dec.p_market,
                    mispricing_base=dec.p_base - dec.p_market if dec.p_base and dec.p_market else None,
                    confidence_weighted_mispricing=dec.confidence_weighted_mispricing,
                    lag_confidence=dec.lag_confidence,
                    spot_confidence=spot_conf or 0.0,
                    active_strategy=dec.strategy,
                    router_action=dec.action,
                    wait_reason=dec.reason if dec.action == "WAIT" else "",
                    open_position_side=(pos.get("side") if pos else None),
                    open_position_entry=(pos.get("entry") if pos else None),
                    open_position_contracts=(pos.get("contracts") if pos else None),
                    unrealized_pnl=None,
                    halt_state=portfolio.halt_state,
                    halt_reason=portfolio.halt_reason,
                    kalshi_quote_age_secs=dec.kalshi_quote_age_secs,
                    kalshi_spread=dec.kalshi_spread,
                )
            elif live:
                # Live session but this asset has no ticks yet — do not fake BTC prices
                snap = _placeholder_snapshot(
                    asset, portfolio, "no_live_decisions_yet"
                )
                if pos:
                    snap.open_position_side = pos.get("side")
                    snap.open_position_entry = pos.get("entry")
                    snap.open_position_contracts = pos.get("contracts")
                    snap.wait_reason = "position_open"
                    snap.router_action = "WAIT"
                snapshots[asset] = snap
            else:
                mock = generate_state_snapshots()
                snapshots[asset] = mock.get(asset, mock["BTC"])
        return snapshots
    except Exception:
        return generate_state_snapshots()


def _window_display(wid: str) -> str:
    """Convert '2026-03-17 09:15' to '09:15-09:30'."""
    if not wid or len(wid) < 16:
        return wid or "?"
    try:
        from datetime import timedelta
        dt = datetime.strptime(wid[:16], "%Y-%m-%d %H:%M")
        end = dt + timedelta(minutes=15)
        return f"{dt.strftime('%H:%M')}-{end.strftime('%H:%M')}"
    except ValueError:
        return wid


def _row_from_window_log(w: dict) -> dict:
    wid = w.get("window_id") or ""
    return {
        "window": _window_display(wid) if isinstance(wid, str) else "?",
        "window_id": wid,
        "asset": w.get("asset") or "?",
        "price_to_beat": _safe_float(w.get("price_to_beat"), 0.0),
        "exit_price": _safe_float(w.get("exit_spot"), 0.0),
        "actual_outcome": w.get("actual_outcome") or "?",
        "bot_action": w.get("bot_action") or "NO_TRADE",
        "entry_price": w.get("entry"),
        "risked_$": _safe_float(w.get("amount_usdc"), 0.0),
        "pnl": _safe_float(w.get("pnl"), 0.0),
        "correct": w.get("correct"),
        "strategy": w.get("strategy") or "",
        "reason": (w.get("reason") or "")[:80],
        "p_market": w.get("p_market"),
        "p_base": w.get("p_base"),
        "lag": _safe_float(w.get("lag_confidence"), 0.0),
        "spot_conf": _safe_float(w.get("spot_confidence"), 0.0),
        "cwm": w.get("confidence_weighted_mispricing"),
    }


def _load_windows_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists() or path.stat().st_size == 0:
        return rows
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    w = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rows.append(_row_from_window_log(w))
    except IOError:
        return []
    rows.sort(key=lambda r: (r.get("window_id", ""), r.get("asset", "")), reverse=True)
    return rows


def _last_archive_windows_path() -> Optional[Path]:
    """Prefer session_meta last_archive; else newest sessions/* with windows or decisions."""
    try:
        if META_PATH.exists():
            meta = json.loads(META_PATH.read_text(encoding="utf-8"))
            rel = meta.get("last_archive")
            if rel:
                p = ROOT / rel / "kalshi_windows.jsonl"
                if p.exists() and p.stat().st_size > 0:
                    return p
                # Older archives: no windows file yet
                return None
    except (OSError, json.JSONDecodeError):
        pass
    if not SESSIONS_DIR.exists():
        return None
    candidates = sorted(
        [p for p in SESSIONS_DIR.iterdir() if p.is_dir()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for c in candidates:
        wp = c / "kalshi_windows.jsonl"
        if wp.exists() and wp.stat().st_size > 0:
            return wp
    return None


def load_window_performance() -> List[Dict[str, Any]]:
    """
    Prefer kalshi_windows.jsonl (one row per asset×window at rollover).
    Fall back to reconstructing from trades+decisions, then last archive.
    """
    rows = _load_windows_jsonl(WINDOWS_PATH)
    if rows:
        return rows

    # Reconstruct from live trades + decisions (legacy / mid-window before first rollover file)
    rows = _reconstruct_windows_from_trades_decisions(TRADES_PATH, DECISIONS_PATH)
    if rows:
        return rows

    arch = _last_archive_windows_path()
    if arch:
        return _load_windows_jsonl(arch)

    # Last resort: rebuild from newest archive decisions/trades
    if SESSIONS_DIR.exists():
        archives = sorted(
            [p for p in SESSIONS_DIR.iterdir() if p.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for a in archives[:1]:
            rebuilt = _reconstruct_windows_from_trades_decisions(
                a / "kalshi_trades.jsonl",
                a / "kalshi_decisions.jsonl",
            )
            if rebuilt:
                return rebuilt
    return []


def _reconstruct_windows_from_trades_decisions(
    trades_path: Path,
    decisions_path: Path,
) -> List[Dict[str, Any]]:
    """Legacy path: synthesize window rows from trades + last decision per window."""
    rows: List[Dict[str, Any]] = []
    trades_by_key: Dict[str, dict] = {}
    decisions_by_key: Dict[str, dict] = {}

    try:
        if trades_path.exists() and trades_path.stat().st_size > 0:
            with open(trades_path, encoding="utf-8") as f:
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
                    if not wid:
                        continue
                    key = f"{wid}|{asset}"
                    entry = _safe_float(t.get("entry"), 0.5)
                    side = (t.get("side") or ("yes" if entry > 0.5 else "no")).lower()
                    ptb = _safe_float(t.get("price_to_beat"), 0.0)
                    exit_spot = _safe_float(t.get("exit_spot"), 0.0)
                    amount = t.get("amount_usdc")
                    if amount is None:
                        amount = entry * int(t.get("contracts") or 0)
                    bot_action = "BUY_YES" if side == "yes" else "BUY_NO"
                    actual = "YES" if exit_spot > ptb else "NO"
                    correct = (side == "yes") == (actual == "YES")
                    trades_by_key[key] = {
                        "window": _window_display(wid),
                        "window_id": wid,
                        "asset": asset,
                        "price_to_beat": ptb,
                        "exit_price": exit_spot,
                        "actual_outcome": actual,
                        "bot_action": bot_action,
                        "entry_price": entry,
                        "risked_$": _safe_float(amount, 0.0),
                        "pnl": _safe_float(t.get("pnl"), 0.0),
                        "correct": correct,
                        "strategy": t.get("strategy") or "",
                        "reason": t.get("reason") or "",
                        "p_market": t.get("p_market"),
                        "p_base": t.get("p_base"),
                        "lag": _safe_float(t.get("lag_confidence_at_entry"), 0.0),
                        "spot_conf": _safe_float(t.get("spot_confidence_at_entry"), 0.0),
                        "cwm": t.get("confidence_weighted_mispricing_at_entry"),
                    }
    except IOError:
        pass

    try:
        if decisions_path.exists() and decisions_path.stat().st_size > 0:
            with open(decisions_path, encoding="utf-8") as f:
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

    for t in trades_by_key.values():
        rows.append(t)
    for d in decisions_by_key.values():
        wid = d.get("window_id", "")
        wid_ts = d.get("window_id_ts")
        if (not wid or isinstance(wid, int)) and isinstance(wid_ts, (int, float)) and wid_ts:
            wid = datetime.fromtimestamp(int(wid_ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        ptb = _safe_float(d.get("spot_start") or d.get("price_to_beat"), 0.0)
        exit_spot = _safe_float(d.get("spot_now"), 0.0)
        action = d.get("action", "WAIT")
        bot_action = "NO_TRADE" if action == "WAIT" else action
        actual = "YES" if exit_spot > ptb else "NO" if ptb > 0 else "?"
        rows.append({
            "window": _window_display(wid) if isinstance(wid, str) else "?",
            "window_id": wid,
            "asset": d.get("asset", "BTC"),
            "price_to_beat": ptb,
            "exit_price": exit_spot,
            "actual_outcome": actual,
            "bot_action": bot_action,
            "entry_price": None,
            "risked_$": 0.0,
            "pnl": 0.0,
            "correct": None,
            "strategy": d.get("strategy") or "",
            "reason": (d.get("reason") or "")[:80],
            "p_market": d.get("p_market"),
            "p_base": d.get("p_base"),
            "lag": _safe_float(d.get("lag_confidence"), 0.0),
            "spot_conf": _safe_float(d.get("spot_confidence"), 0.0),
            "cwm": d.get("confidence_weighted_mispricing"),
        })

    rows.sort(key=lambda r: (r.get("window_id", ""), r.get("asset", "")), reverse=True)
    return rows
