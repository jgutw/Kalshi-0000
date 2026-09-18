"""
runtime_control.py — Cross-process control plane for Telegram / dashboard.

State file:  logs/runtime_control.json   (pause flag, last applied knobs)
Command queue: logs/bot_control.jsonl    (Telegram → trading bot)
Open positions snapshot: logs/open_positions.json  (bot → Telegram)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .config import cfg

log = logging.getLogger("kalshi_bot.runtime_control")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
RUNTIME_PATH = LOGS_DIR / "runtime_control.json"
COMMANDS_PATH = LOGS_DIR / "bot_control.jsonl"
OPEN_POSITIONS_PATH = LOGS_DIR / "open_positions.json"
LIVE_CONFIG_PATH = LOGS_DIR / "live_config.json"
BOT_HEARTBEAT_PATH = LOGS_DIR / "bot_heartbeat.json"
BOT_HEARTBEAT_STALE_SECS = 20.0

# Risk Update v1 knobs, at their legacy no-op values. Spread into the two
# original presets so switching back from engineered_risk / live_safe fully
# restores the behavior those profiles were traded with.
_LEGACY_RISK_KNOBS: dict[str, Any] = {
    "PORTFOLIO_GROSS_HARD_STOP": 1.0,   # disabled
    "LOTTERY_MAX_RISK_PCT": 0.0,        # no lottery cap (full Kelly)
    "LOTTERY_MAX_CONCURRENT": 0,        # unlimited
    "YES_SIZE_MULT": 1.0,
    "NO_SIZE_MULT": 1.0,
    "MID_BAND_SIZE_MULT": 1.0,
    "MAX_CONSEC_LOSSES": 8,
    "MAX_DAILY_LOSS_PCT": 0.40,
    # Stated explicitly so switching profiles is deterministic: the live ceiling
    # forces this False, and without a value here that would leak into whatever
    # profile is applied next in the same process.
    "PER_ASSET_CIRCUIT_BREAKER": True,
    # Activity probe off unless a preset turns it on (max_risk_micro).
    "ACTIVITY_MANDATE_ENABLED": False,
    "ACTIVITY_IDLE_SECS": 3600.0,
    "FILL_QUOTA_ENABLED": False,
    "MIN_FILLS_PER_HOUR": 1,
}

# Presets that Telegram /profile can apply (paper only).
PROFILE_PRESETS: dict[str, dict[str, Any]] = {
    # ── Original profiles — unchanged behavior (R26-R32 were traded on these) ──
    "max_risk_paper": {
        "CONFIG_PROFILE": "max_risk_paper",
        "KELLY_FRACTION": 0.50,
        "MAX_POS_PCT": 0.08,
        "PORTFOLIO_GROSS_CAP": 0.30,
        "MIN_TRADE_USD": 5.0,
        "MIN_ENTRY_PRICE": 0.02,
        "MAX_ENTRY_PRICE": 0.98,
        **_LEGACY_RISK_KNOBS,
    },
    "max_risk_micro": {
        "CONFIG_PROFILE": "max_risk_micro",
        "KELLY_FRACTION": 0.50,
        "MAX_POS_PCT": 0.10,
        "PORTFOLIO_GROSS_CAP": 0.35,
        "MIN_TRADE_USD": 2.0,
        "MIN_ENTRY_PRICE": 0.02,
        "MAX_ENTRY_PRICE": 0.98,
        **_LEGACY_RISK_KNOBS,
        # After 30m with no close, ease lag/spot/edge and size a small probe.
        # Does not bypass p_base_near_50, 1¢ books, halt, or window edge.
        "ACTIVITY_MANDATE_ENABLED": True,
        "ACTIVITY_IDLE_SECS": 1800.0,
        "ACTIVITY_PROBE_SIZE_PCT": 0.025,
        "FILL_QUOTA_ENABLED": True,
        "MIN_FILLS_PER_HOUR": 1,
        "QUOTA_SIZE_PCT": 0.02,
    },
    # ── Risk Update v1 (see PROPOSAL_RISK_UPDATE.md) ──────────────────────────
    # Same trade population as max_risk_paper, smaller positions. Sizing was the
    # dominant loss driver on 2026-08-10, not signal quality.
    "engineered_risk": {
        "CONFIG_PROFILE": "engineered_risk",
        "KELLY_FRACTION": 0.30,          # C2: from 0.50
        "MAX_POS_PCT": 0.05,             # C2: from 0.08 (median trade risked 7.65%)
        "PORTFOLIO_GROSS_CAP": 0.20,     # C1: 10-20% gross won 93% of windows
        "PORTFOLIO_GROSS_HARD_STOP": 0.28,
        "MIN_TRADE_USD": 2.0,
        "MIN_ENTRY_PRICE": 0.02,         # lotteries still allowed, but capped below
        "MAX_ENTRY_PRICE": 0.98,
        "LOTTERY_MAX_RISK_PCT": 0.015,   # C3: was ~8.4% average
        "LOTTERY_MAX_CONCURRENT": 1,
        # C4: 8 was never reached before the damage was done. At ~2% risk per
        # trade, 4 straight losses is roughly a 8-12% drawdown — early enough
        # to be worth a human look, which is the point of the breaker.
        "MAX_CONSEC_LOSSES": 4,
        "PER_ASSET_CIRCUIT_BREAKER": True,   # paper regime keeps per-symbol scope
        "MAX_DAILY_LOSS_PCT": 0.25,
        "YES_SIZE_MULT": 0.7,            # C5: YES -15% vs NO +298% risk-normalized
        "NO_SIZE_MULT": 1.0,
        "MID_BAND_SIZE_MULT": 0.8,       # C6: weakest positive bucket
        "ACTIVITY_MANDATE_ENABLED": False,
        "ACTIVITY_IDLE_SECS": 3600.0,
        "FILL_QUOTA_ENABLED": True,
        "MIN_FILLS_PER_HOUR": 1,
        "QUOTA_SIZE_PCT": 0.02,
    },
    # Live candidate: strictly tighter than engineered_risk. Not auto-selected;
    # live start still goes through safe_live preflight + LiveGuard.
    "live_safe": {
        "CONFIG_PROFILE": "live_safe",
        "KELLY_FRACTION": 0.25,
        "MAX_POS_PCT": 0.04,
        "PORTFOLIO_GROSS_CAP": 0.15,
        "PORTFOLIO_GROSS_HARD_STOP": 0.20,
        "MIN_TRADE_USD": 2.0,
        "MIN_ENTRY_PRICE": 0.15,         # no lottery tickets with real money
        "MAX_ENTRY_PRICE": 0.95,
        "LOTTERY_MAX_RISK_PCT": 0.005,
        "LOTTERY_MAX_CONCURRENT": 0,
        # Tighter than engineered_risk's 4: with real money the breaker exists to
        # buy a human a look, not to ride out a streak. Also enforced as a live
        # ceiling in apply_profile(), so no profile can trade live looser.
        "MAX_CONSEC_LOSSES": 3,
        # Whole-book scope, matching live, so a paper shadow run of this profile
        # behaves the way real money will.
        "PER_ASSET_CIRCUIT_BREAKER": False,
        "MAX_DAILY_LOSS_PCT": 0.15,
        "YES_SIZE_MULT": 0.7,
        "NO_SIZE_MULT": 1.0,
        "MID_BAND_SIZE_MULT": 0.8,
        "ACTIVITY_MANDATE_ENABLED": False,
        "ACTIVITY_IDLE_SECS": 3600.0,
        "FILL_QUOTA_ENABLED": False,
    },
    # Quality-first live/paper: tighter gates + rolling Sharpe floor after warmup.
    "higher_sharpe": {
        "CONFIG_PROFILE": "higher_sharpe",
        "KELLY_FRACTION": 0.25,
        "MAX_POS_PCT": 0.04,
        "PORTFOLIO_GROSS_CAP": 0.15,
        "PORTFOLIO_GROSS_HARD_STOP": 0.20,
        "MIN_TRADE_USD": 5.0,
        "MIN_ENTRY_PRICE": 0.12,
        "MAX_ENTRY_PRICE": 0.92,
        "LAG_CONFIDENCE_MIN": 0.22,
        "LAG_ABSENT_MIN": 0.18,
        "CWM_MIN": 0.035,
        "ALPHA_EDGE_ENABLED": False,
        "MIN_CONVICTION": 4,
        "P_BASE_CENTER_MIN": 0.08,
        "SHARPE_MIN": 0.50,
        "SHARPE_MIN_TRADES": 20,
        "MIN_EDGE_PCT": 0.03,
        "SPOT_CONFIDENCE_MIN": 0.45,
        "LOTTERY_MAX_RISK_PCT": 0.01,
        "LOTTERY_MAX_CONCURRENT": 0,
        "YES_SIZE_MULT": 0.7,
        "NO_SIZE_MULT": 1.0,
        "MID_BAND_SIZE_MULT": 0.75,
        "MAX_CONSEC_LOSSES": 3,
        "PER_ASSET_CIRCUIT_BREAKER": True,
        "MAX_DAILY_LOSS_PCT": 0.15,
        "ACTIVITY_MANDATE_ENABLED": False,
        "ACTIVITY_IDLE_SECS": 3600.0,
        "FILL_QUOTA_ENABLED": False,
    },
}


# Human-readable description of every trading regime this bot has run or can run.
# Kept SEPARATE from PROFILE_PRESETS on purpose: apply_profile() setattr's every
# key of a preset onto cfg, so putting prose in there would pollute the config
# object. Nothing here affects behavior — it is documentation the bot can print.
PROFILE_META: dict[str, dict[str, str]] = {
    "max_risk_paper": {
        "title": "Max risk (paper)",
        "style": "Aggressive fire-rate. Loose gates, half Kelly, 8% per position, "
                 "30% gross. Variance shrink off, lottery entries allowed.",
        "use_when": "Paper research when you want maximum sample size per round.",
        "history": "Used for R11-R12, R19-R20, R23, R26, R29-R31 (9 rounds, 326 "
                   "trades). Median round +88%, best +630% (R19), worst -36% "
                   "(R29); 5 of 9 green. Highest ceiling and the deepest holes. "
                   "Earlier regimes (disciplined_paper_v2, phase1_windows, "
                   "balanced_flow, higher_sharpe, baseline_*) predate it and "
                   "survive only as archives — their parameters were never "
                   "recorded. PAPER ONLY.",
        "status": "unchanged — preserved exactly as traded",
    },
    "max_risk_micro": {
        "title": "Max risk (micro book)",
        "style": "Same gates as max_risk_paper, sized for a small book: $2 min "
                 "trade, 10% per position, 35% gross. After 30 minutes with no "
                 "close, eases lag/spot/edge and sizes a 2.5% probe.",
        "use_when": "$100-$300 starting capital, paper or cautious live micro.",
        "history": "Used for R22, R24-R25, R27-R28, R32 and the live micro "
                   "rounds (8 rounds, 150 trades). Median round +18%, best "
                   "+150% (live_02), worst -46% (R28); 4 of 8 green. "
                   "30m activity probe added 2026-08-16.",
        "status": "30m activity probe on (soft gates only)",
    },
    "engineered_risk": {
        "title": "Engineered risk (Risk Update v1)",
        "style": "Same signals and same trade population as max_risk_paper, but "
                 "roughly half the position size: Kelly 0.30, 5% per position, "
                 "20% gross with a 28% hard stop, lottery risk capped at 1.5%, "
                 "breaker at 4 losses, YES sized x0.7, mid-band x0.8.",
        "use_when": "Default paper research from 2026-08-11 onward. Run it "
                    "alongside max_risk_paper to compare drawdown, not headline PnL.",
        "history": "Built from 174 trades on 2026-08-10. Sizing, not signal "
                   "quality, was the dominant loss driver: windows above 35% "
                   "gross went 0-for-3, while 10-20% gross won 93% of windows.",
        "status": "new — additive, does not modify any existing regime",
    },
    "live_safe": {
        "title": "Live safe",
        "style": "Strictly tighter than engineered_risk: Kelly 0.25, 4% per "
                 "position, 15% gross, no entries below 0.15, breaker at 3 losses, "
                 "15% daily loss limit.",
        "use_when": "Real money only, and only after engineered_risk has run "
                    "several paper rounds and LiveGuard is green.",
        "history": "Never traded yet. Candidate profile only.",
        "status": "new — additive, not auto-selected anywhere",
    },
    "higher_sharpe": {
        "title": "Higher Sharpe",
        "style": "Quality over quantity: lag + CWM + conviction gates tightened, "
                 "no alpha_edge overlay, no fill quota. Kelly 0.25, 4% per trade, "
                 "15% gross. After 20 closes, pauses if rolling Sharpe < 0.50.",
        "use_when": "Live or paper when you want risk-adjusted returns, not max fire rate.",
        "history": "Added 2026-08-29 after live micro session lost on alpha_edge "
                   "lottery-style entries in one window.",
        "status": "new — live candidate with Sharpe gate",
    },
}


def profile_description(name: str) -> str:
    """Multi-line description of a regime, for Telegram and round logs."""
    meta = PROFILE_META.get(str(name).strip().lower())
    if not meta:
        return f"{name}: no description on file."
    return (
        f"{meta['title']} ({name})\n"
        f"  Style:    {meta['style']}\n"
        f"  Use when: {meta['use_when']}\n"
        f"  History:  {meta['history']}\n"
        f"  Status:   {meta['status']}"
    )


def profile_summary_line(name: str) -> str:
    """One-line description for compact listings."""
    meta = PROFILE_META.get(str(name).strip().lower())
    return meta["title"] if meta else str(name)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_runtime() -> dict[str, Any]:
    return {
        "entries_paused": False,
        "stop_requested": False,
        "updated_at": _now(),
        "source": "init",
    }


def load_runtime() -> dict[str, Any]:
    if not RUNTIME_PATH.exists():
        return default_runtime()
    try:
        data = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default_runtime()
        base = default_runtime()
        base.update(data)
        return base
    except (json.JSONDecodeError, OSError) as e:
        log.warning("runtime_control read failed: %s", e)
        return default_runtime()


def save_runtime(data: dict[str, Any]) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data["updated_at"] = _now()
    RUNTIME_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def entries_paused() -> bool:
    return bool(load_runtime().get("entries_paused"))


def stop_requested() -> bool:
    return bool(load_runtime().get("stop_requested"))


def set_entries_paused(paused: bool, source: str = "telegram") -> None:
    rt = load_runtime()
    rt["entries_paused"] = bool(paused)
    rt["source"] = source
    # Clearing pause should not leave a stale stop bit unless explicitly set.
    save_runtime(rt)


def request_stop(source: str = "telegram") -> None:
    rt = load_runtime()
    rt["stop_requested"] = True
    rt["entries_paused"] = True
    rt["source"] = source
    save_runtime(rt)


def clear_stop(source: str = "bot") -> None:
    rt = load_runtime()
    rt["stop_requested"] = False
    rt["source"] = source
    save_runtime(rt)


def clear_bot_command_queue() -> None:
    """Drop pending Telegram control commands (avoids stale /stop killing a new round)."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        COMMANDS_PATH.write_text("", encoding="utf-8")
    except OSError as e:
        log.warning("clear bot_control queue failed: %s", e)


def prepare_for_new_round(source: str = "start_round") -> None:
    """Reset pause/stop flags and drain command queue before spawning a bot."""
    clear_bot_command_queue()
    rt = default_runtime()
    rt["entries_paused"] = False
    rt["stop_requested"] = False
    rt["source"] = source
    save_runtime(rt)


def enqueue_bot_command(action: str, **payload: Any) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    cmd = {"ts": _now(), "action": action, **payload}
    with open(COMMANDS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(cmd) + "\n")


def write_open_positions(positions: list[dict[str, Any]]) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"ts": _now(), "positions": positions}
    OPEN_POSITIONS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_live_config() -> None:
    """Snapshot of trading knobs from the running bot process (for Telegram views)."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": _now(),
        "CONFIG_PROFILE": cfg.CONFIG_PROFILE,
        "KELLY_FRACTION": cfg.KELLY_FRACTION,
        "MAX_POS_PCT": cfg.MAX_POS_PCT,
        "PORTFOLIO_GROSS_CAP": cfg.PORTFOLIO_GROSS_CAP,
        "MIN_TRADE_USD": cfg.MIN_TRADE_USD,
        "MIN_ENTRY_PRICE": cfg.MIN_ENTRY_PRICE,
        "MAX_ENTRY_PRICE": cfg.MAX_ENTRY_PRICE,
        "MIN_EDGE_PCT": cfg.MIN_EDGE_PCT,
        "DRY_RUN": cfg.DRY_RUN,
        "SIM_BALANCE": cfg.SIM_BALANCE,
        "MAX_DRAWDOWN_PCT": cfg.MAX_DRAWDOWN_PCT,
        "MAX_DAILY_LOSS_PCT": cfg.MAX_DAILY_LOSS_PCT,
        "MAX_CONSEC_LOSSES": cfg.MAX_CONSEC_LOSSES,
        # Risk Update v1 knobs so /sizing and the dashboard show what is active
        "PORTFOLIO_GROSS_HARD_STOP": getattr(cfg, "PORTFOLIO_GROSS_HARD_STOP", 1.0),
        "LOTTERY_MAX_RISK_PCT": getattr(cfg, "LOTTERY_MAX_RISK_PCT", 0.0),
        "LOTTERY_MAX_CONCURRENT": getattr(cfg, "LOTTERY_MAX_CONCURRENT", 0),
        "YES_SIZE_MULT": getattr(cfg, "YES_SIZE_MULT", 1.0),
        "MID_BAND_SIZE_MULT": getattr(cfg, "MID_BAND_SIZE_MULT", 1.0),
        "ACTIVITY_MANDATE_ENABLED": getattr(cfg, "ACTIVITY_MANDATE_ENABLED", False),
        "ACTIVITY_IDLE_SECS": getattr(cfg, "ACTIVITY_IDLE_SECS", 3600.0),
        "ACTIVITY_PROBE_SIZE_PCT": getattr(cfg, "ACTIVITY_PROBE_SIZE_PCT", 0.025),
        "FILL_QUOTA_ENABLED": getattr(cfg, "FILL_QUOTA_ENABLED", False),
        "MIN_FILLS_PER_HOUR": getattr(cfg, "MIN_FILLS_PER_HOUR", 1),
        "QUOTA_SIZE_PCT": getattr(cfg, "QUOTA_SIZE_PCT", 0.02),
    }
    LIVE_CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_live_config() -> dict[str, Any]:
    if not LIVE_CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(LIVE_CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def write_bot_heartbeat(extra: Optional[dict[str, Any]] = None) -> None:
    """Trading bot liveness signal for Telegram /stop behavior."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "ts": _now(),
        "unix": __import__("time").time(),
        "profile": cfg.CONFIG_PROFILE,
        "dry_run": cfg.DRY_RUN,
    }
    if extra:
        payload.update(extra)
    BOT_HEARTBEAT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def trading_bot_running(stale_secs: float = BOT_HEARTBEAT_STALE_SECS) -> bool:
    """True if the trading bot wrote a recent heartbeat."""
    if not BOT_HEARTBEAT_PATH.exists():
        return False
    try:
        data = json.loads(BOT_HEARTBEAT_PATH.read_text(encoding="utf-8"))
        unix = float(data.get("unix") or 0)
        if unix <= 0:
            return False
        age = __import__("time").time() - unix
        return age <= float(stale_secs)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return False


def load_open_positions() -> list[dict[str, Any]]:
    if not OPEN_POSITIONS_PATH.exists():
        return []
    try:
        data = json.loads(OPEN_POSITIONS_PATH.read_text(encoding="utf-8"))
        pos = data.get("positions") if isinstance(data, dict) else None
        return list(pos) if isinstance(pos, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def apply_profile(name: str) -> tuple[bool, str]:
    key = name.strip().lower()
    preset = PROFILE_PRESETS.get(key)
    if not preset:
        return False, f"Unknown profile '{name}'. Try: {', '.join(PROFILE_PRESETS)}"
    for field, value in preset.items():
        setattr(cfg, field, value)
    note = _apply_live_ceilings()
    title = profile_summary_line(key)
    return True, f"Applied profile {key} — {title}{note}"


def _apply_live_ceilings() -> str:
    """
    Clamp risk knobs that must never be loose with real money, whatever preset
    was chosen. Runs after every apply_profile(); a no-op in paper.

    run_kalshi_bot sets cfg.DRY_RUN from --live *before* applying the profile,
    so DRY_RUN is already correct here.
    """
    if cfg.DRY_RUN:
        return ""
    notes: list[str] = []
    ceiling = int(getattr(cfg, "LIVE_MAX_CONSEC_LOSSES", 3))
    if int(cfg.MAX_CONSEC_LOSSES) > ceiling:
        was = int(cfg.MAX_CONSEC_LOSSES)
        cfg.MAX_CONSEC_LOSSES = ceiling
        log.warning("LIVE ceiling: MAX_CONSEC_LOSSES %d -> %d", was, ceiling)
        notes.append(f"MAX_CONSEC_LOSSES {was}->{ceiling}")

    per_asset = bool(getattr(cfg, "LIVE_PER_ASSET_BREAKER", False))
    if bool(cfg.PER_ASSET_CIRCUIT_BREAKER) != per_asset:
        cfg.PER_ASSET_CIRCUIT_BREAKER = per_asset
        log.warning("LIVE ceiling: PER_ASSET_CIRCUIT_BREAKER -> %s", per_asset)
        notes.append(f"PER_ASSET_CIRCUIT_BREAKER->{per_asset}")

    return f" [live ceiling: {', '.join(notes)}]" if notes else ""


def process_bot_commands(
    on_stop: Optional[Callable[[], None]] = None,
    sim: Any = None,
) -> list[str]:
    """
    Drain bot_control.jsonl and apply to cfg / runtime flags.
    Returns human-readable result lines for logging.

    `sim` is optional so callers without state still work; it is only needed so
    /resume can reset a live consecutive-loss breaker, which never times out.
    """
    if not COMMANDS_PATH.exists():
        return []
    try:
        raw = COMMANDS_PATH.read_text(encoding="utf-8")
    except OSError:
        return []
    if not raw.strip():
        return []

    results: list[str] = []
    kept: list[str] = []

    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError:
            continue
        action = str(cmd.get("action") or "")
        try:
            if action == "pause":
                set_entries_paused(True, source=str(cmd.get("source") or "telegram"))
                results.append("entries paused")
            elif action == "resume":
                src = str(cmd.get("source") or "telegram")
                set_entries_paused(False, source=src)
                msg = "entries resumed"
                if sim is not None:
                    cleared = sim.clear_breaker_halt(source=src)
                    if cleared:
                        msg += f" | breaker reset ({cleared})"
                results.append(msg)
            elif action == "stop":
                request_stop(source=str(cmd.get("source") or "telegram"))
                results.append("stop requested")
                if on_stop:
                    on_stop()
            elif action == "set_max_pos":
                val = float(cmd["value"])
                if not 0.01 <= val <= 0.50:
                    results.append(f"set_max_pos rejected: {val}")
                else:
                    cfg.MAX_POS_PCT = val
                    results.append(f"MAX_POS_PCT={val:.2%}")
            elif action == "set_min_trade":
                val = float(cmd["value"])
                if val < 0.5:
                    results.append(f"set_min_trade rejected: {val}")
                else:
                    cfg.MIN_TRADE_USD = val
                    results.append(f"MIN_TRADE_USD=${val:.2f}")
            elif action == "set_kelly":
                val = float(cmd["value"])
                if not 0.05 <= val <= 1.0:
                    results.append(f"set_kelly rejected: {val}")
                else:
                    cfg.KELLY_FRACTION = val
                    results.append(f"KELLY_FRACTION={val:.2f}")
            elif action == "set_portfolio_cap":
                val = float(cmd["value"])
                if not 0.05 <= val <= 1.0:
                    results.append(f"set_portfolio_cap rejected: {val}")
                else:
                    cfg.PORTFOLIO_GROSS_CAP = val
                    results.append(f"PORTFOLIO_GROSS_CAP={val:.2%}")
            elif action == "set_consec_losses":
                # Circuit-breaker tightening mid-round. Loosening past the
                # legacy 8 is refused so this can't be used to disable the
                # breaker while a round is bleeding.
                val = int(float(cmd["value"]))
                hi = 8 if cfg.DRY_RUN else int(getattr(cfg, "LIVE_MAX_CONSEC_LOSSES", 3))
                if not 2 <= val <= hi:
                    results.append(f"set_consec_losses rejected: {val} (allowed 2-{hi})")
                else:
                    cfg.MAX_CONSEC_LOSSES = val
                    results.append(f"MAX_CONSEC_LOSSES={val}")
            elif action == "set_daily_loss":
                val = float(cmd["value"])
                if not 0.05 <= val <= 0.50:
                    results.append(f"set_daily_loss rejected: {val}")
                else:
                    cfg.MAX_DAILY_LOSS_PCT = val
                    results.append(f"MAX_DAILY_LOSS_PCT={val:.2%}")
            elif action == "set_max_drawdown":
                val = float(cmd["value"])
                if not 0.05 <= val <= 0.60:
                    results.append(f"set_max_drawdown rejected: {val}")
                else:
                    cfg.MAX_DRAWDOWN_PCT = val
                    results.append(f"MAX_DRAWDOWN_PCT={val:.2%}")
            elif action == "profile":
                ok, msg = apply_profile(str(cmd.get("name") or ""))
                results.append(msg if ok else f"profile failed: {msg}")
            else:
                kept.append(line)
                results.append(f"unknown action kept: {action}")
        except (KeyError, TypeError, ValueError) as e:
            results.append(f"bad command {action}: {e}")

    try:
        if kept:
            COMMANDS_PATH.write_text("\n".join(kept) + "\n", encoding="utf-8")
        else:
            COMMANDS_PATH.write_text("", encoding="utf-8")
    except OSError as e:
        log.warning("bot_control cleanup failed: %s", e)

    if results:
        write_live_config()
    for line in results:
        log.info("runtime_control: %s", line)
    return results
