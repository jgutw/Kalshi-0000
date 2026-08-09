"""Text formatters — same data sources as the Streamlit dashboard."""

from __future__ import annotations

from collections import Counter
from typing import Any, Optional

from kalshi_bot.config import cfg
from kalshi_bot.runtime_control import (
    PROFILE_PRESETS,
    entries_paused,
    load_live_config,
    load_open_positions,
    load_runtime,
    trading_bot_running,
)
from kalshi_bot.session_meta import session_is_active


def _live(key: str, default=None):
    """Prefer knobs written by the running bot; fall back to this process cfg."""
    live = load_live_config()
    if key in live and live[key] is not None:
        return live[key]
    return getattr(cfg, key, default)
from kalshi_bot.vault import load_recent_skims, load_vault_config

from dashboard.data.loaders import (
    load_decisions,
    load_latest_snapshots,
    load_portfolio,
    load_trades,
    load_window_performance,
)


def _pct(x: Optional[float]) -> str:
    if x is None:
        return "—"
    return f"{x:.1%}"


def _num(x: Optional[float], digits: int = 3) -> str:
    if x is None:
        return "—"
    return f"{x:.{digits}f}"


def _money(x: float) -> str:
    return f"${x:,.2f}"


def format_help() -> str:
    return (
        "Kalshi paper bot - Telegram control\n"
        "\n"
        "START A ROUND (easiest)\n"
        "  /go standard          $500 max_risk\n"
        "  /go big               $2000 max_risk\n"
        "  /go micro             $200 micro\n"
        "  /start_round 500\n"
        "  /start_round 2000 max_risk_paper\n"
        "  /presets   /history\n"
        "\n"
        "Views\n"
        "  /status  /live  /risk  /positions\n"
        "  /router  /why  /trades [n]  /windows\n"
        "  /vault  /summary  /help\n"
        "\n"
        "Vault / take-profit\n"
        "  /take_cash <usd>\n"
        "  /vault_auto on|off\n"
        "  /vault_set <trigger> <skim>\n"
        "\n"
        "Control\n"
        "  /pause  /resume\n"
        "  /stop   (archive + Excel, then idle)\n"
        "\n"
        "Mid-round tweaks (optional)\n"
        "  /sizing\n"
        "  /set_max_pos 8\n"
        "  /set_min_trade 5\n"
        "  /set_kelly 0.5\n"
        "  /profile max_risk_paper\n"
        "\n"
        "Only your TELEGRAM_CHAT_ID is accepted."
    )


def format_idle_status() -> str:
    last = ""
    try:
        import json
        from pathlib import Path

        meta_path = Path("logs/session_meta.json")
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("last_archive"):
                last = f"\nLast archive: {meta.get('last_archive')}"
    except Exception:
        pass
    return (
        "No active paper round\n"
        "Trading bot: not running\n"
        "Start from PC:\n"
        "  python run_kalshi_bot.py --mode run --fresh-round "
        "--sim-balance 500 --session-tag round_21_..."
        f"{last}"
    )


def format_status() -> str:
    if not session_is_active() and not trading_bot_running():
        return format_idle_status()
    p = load_portfolio()
    rt = load_runtime()
    paused = bool(rt.get("entries_paused"))
    equity = p.total_equity or (p.balance + p.vault_balance)
    pnl = equity - p.starting_balance
    pnl_pct = (pnl / p.starting_balance * 100.0) if p.starting_balance else 0.0
    dry = bool(_live("DRY_RUN", cfg.DRY_RUN))
    mode = "PAPER" if dry else "LIVE"
    halt = f"HALTED — {p.halt_reason}" if p.halt_state else "ok"
    kelly = float(_live("KELLY_FRACTION", cfg.KELLY_FRACTION))
    max_pos = float(_live("MAX_POS_PCT", cfg.MAX_POS_PCT))
    min_trade = float(_live("MIN_TRADE_USD", cfg.MIN_TRADE_USD))
    cap = float(_live("PORTFOLIO_GROSS_CAP", cfg.PORTFOLIO_GROSS_CAP))
    profile = str(_live("CONFIG_PROFILE", cfg.CONFIG_PROFILE))
    lines = [
        f"Status ({mode}) | profile={profile}",
        f"Trading {_money(p.balance)} | Vault {_money(p.vault_balance)}",
        f"Equity  {_money(equity)} ({pnl_pct:+.1f}% / {_money(pnl)})",
        f"Trades  {p.total_trades}  W/L {p.wins}/{p.losses}  WR {_pct(p.win_rate)}",
        f"Sharpe  {p.sharpe:.2f}  VaR95 {_pct(p.var_95)}",
        f"Halt    {halt}",
        f"Entries {'PAUSED' if paused else 'active'}",
        f"Sizing  kelly={kelly:.2f} max_pos={max_pos:.0%} "
        f"min_trade=${min_trade:.0f} cap={cap:.0%}",
    ]
    return "\n".join(lines)


def format_live() -> str:
    if not session_is_active() and not trading_bot_running():
        return format_idle_status()
    snaps = load_latest_snapshots()
    lines = ["Live (latest decisions)"]
    for asset in ("BTC", "ETH", "SOL"):
        s = snaps.get(asset)
        if not s:
            lines.append(f"{asset}: no data")
            continue
        feed = "OK" if s.spot_confidence >= 0.6 else "WEAK"
        action = s.router_action or "—"
        reason = s.wait_reason or ""
        if action == "WAIT" and reason:
            tail = f" — {reason[:48]}"
        else:
            tail = ""
        lines.append(
            f"{asset}: {action}{tail}\n"
            f"  p_mkt={_num(s.p_market)} p_base={_num(s.p_base)} p_real={_num(s.p_real)} "
            f"lag={_num(s.lag_confidence, 2)} spot={_num(s.spot_confidence, 2)} feed={feed}"
        )
    return "\n".join(lines)


def format_risk() -> str:
    if not session_is_active() and not trading_bot_running():
        return format_idle_status()
    p = load_portfolio()
    rt = load_runtime()
    vcfg = load_vault_config()
    equity = p.total_equity or (p.balance + p.vault_balance)
    peak_eq = equity
    daily_start = p.starting_balance
    try:
        import json
        from pathlib import Path

        sim = json.loads(Path("logs/kalshi_sim.json").read_text(encoding="utf-8"))
        peak_eq = float(sim.get("peak_equity") or equity)
        daily_start = float(sim.get("daily_start") or p.starting_balance)
    except Exception:
        pass
    eq_dd = ((peak_eq - equity) / peak_eq) if peak_eq > 0 else 0.0
    daily_dd = ((daily_start - p.balance) / daily_start) if daily_start > 0 else 0.0
    return "\n".join(
        [
            "Risk",
            f"Halt: {'YES — ' + p.halt_reason if p.halt_state else 'no'}",
            f"Consec losses: {p.consec_losses} / max {int(_live('MAX_CONSEC_LOSSES', cfg.MAX_CONSEC_LOSSES))}",
            f"Equity DD: {_pct(eq_dd)} (halt @{_pct(float(_live('MAX_DRAWDOWN_PCT', cfg.MAX_DRAWDOWN_PCT)))})",
            f"Daily DD (trading): {_pct(daily_dd)} (halt @{_pct(float(_live('MAX_DAILY_LOSS_PCT', cfg.MAX_DAILY_LOSS_PCT)))})",
            f"Entries paused: {bool(rt.get('entries_paused'))}",
            f"Vault auto: {'on' if vcfg.auto_enabled else 'off'} "
            f"(≥{_money(vcfg.profit_trigger)} profit → skim {_money(vcfg.skim_amount)})",
            f"Skimmable now: {_money(p.skimmable_profit)}",
        ]
    )


def format_positions() -> str:
    positions = load_open_positions()
    if not positions:
        return "Positions: none open"
    lines = ["Open positions"]
    for p in positions:
        lines.append(
            f"{p.get('asset')}: {str(p.get('side', '?')).upper()} "
            f"×{p.get('contracts')} @ {_num(p.get('entry'), 4)} "
            f"({_money(float(p.get('amount_usdc') or 0))}) "
            f"ticker={p.get('ticker', '?')}"
        )
    return "\n".join(lines)


def format_router() -> str:
    snaps = load_latest_snapshots()
    lines = ["Router", "asset | action | lag | spot | z | reason"]
    for asset in ("BTC", "ETH", "SOL"):
        s = snaps.get(asset)
        if not s:
            continue
        reason = (s.wait_reason or "")[:40]
        lines.append(
            f"{asset} | {s.router_action} | {_num(s.lag_confidence, 2)} | "
            f"{_num(s.spot_confidence, 2)} | {_num(s.z_threshold, 2)} | {reason}"
        )
    return "\n".join(lines)


def format_why(last_n: int = 200) -> str:
    decisions = load_decisions(last_n=last_n)
    waits = [d for d in decisions if d.action == "WAIT" and d.reason]
    if not waits:
        return "Why: no recent WAIT reasons"
    counts = Counter(d.reason.split("(")[0] for d in waits)
    lines = [f"Top WAIT reasons (last {len(waits)} waits)"]
    for reason, n in counts.most_common(12):
        lines.append(f"  {n:4d}  {reason}")
    return "\n".join(lines)


def format_trades(n: int = 5) -> str:
    if not session_is_active() and not trading_bot_running():
        return format_idle_status()
    trades = load_trades()
    real = [t for t in trades if t.side]
    if not real:
        return "Trades: none yet"
    lines = [f"Last {min(n, len(real))} trades"]
    for t in real[-n:]:
        lines.append(
            f"{t.ts[:19]} {t.asset} {t.side.upper()} "
            f"{_num(t.entry, 3)}→{_num(t.exit, 1)} ×{t.contracts} "
            f"pnl={_money(t.pnl)} bal={_money(t.balance)}"
        )
    return "\n".join(lines)


def format_windows(n: int = 8) -> str:
    rows = load_window_performance()
    if not rows:
        return "Windows: no data"
    # loaders return most-recent-first
    slice_rows = rows[:n]
    lines = [f"Recent windows (last {len(slice_rows)})"]
    for r in slice_rows:
        lines.append(
            f"{r.get('window', '?')} {r.get('asset', '?')}: "
            f"pnl={_money(float(r.get('pnl') or 0))} "
            f"bot={r.get('bot_action', '?')} actual={r.get('actual_outcome', '?')}"
        )
    return "\n".join(lines)


def format_vault() -> str:
    if not session_is_active() and not trading_bot_running():
        return format_idle_status()
    p = load_portfolio()
    vcfg = load_vault_config()
    skims = load_recent_skims(5)
    lines = [
        "Vault",
        f"Balance: {_money(p.vault_balance)}",
        f"Trading: {_money(p.balance)} | Equity: {_money(p.total_equity or p.balance + p.vault_balance)}",
        f"Skimmable profit: {_money(p.skimmable_profit)}",
        f"Auto: {'ON' if vcfg.auto_enabled else 'OFF'} | "
        f"trigger ${_money(vcfg.profit_trigger)[1:]} | skim ${_money(vcfg.skim_amount)[1:]}",
        "Recent skims:",
    ]
    if not skims:
        lines.append("  (none)")
    else:
        for s in skims[:5]:
            lines.append(
                f"  {str(s.get('ts', ''))[:19]} {_money(float(s.get('amount') or 0))} "
                f"({s.get('reason', '?')}) → vault {_money(float(s.get('vault_after') or 0))}"
            )
    return "\n".join(lines)


def format_summary() -> str:
    if not session_is_active() and not trading_bot_running():
        return format_idle_status()
    p = load_portfolio()
    equity = p.total_equity or (p.balance + p.vault_balance)
    pnl = equity - p.starting_balance
    return "\n".join(
        [
            "Session summary",
            f"Start {_money(p.starting_balance)} -> Equity {_money(equity)} ({_money(pnl)})",
            f"{p.total_trades} trades | {p.wins}W / {p.losses}L | WR {_pct(p.win_rate)} | Sharpe {p.sharpe:.2f}",
            f"Vault {_money(p.vault_balance)} | Trading {_money(p.balance)}",
            f"Profile {_live('CONFIG_PROFILE', cfg.CONFIG_PROFILE)} | paused={entries_paused()}",
        ]
    )


def format_sizing() -> str:
    if not session_is_active() and not trading_bot_running():
        return (
            "No active round — sizing presets apply when you start one.\n"
            "\n"
            "Easiest:\n"
            "  /go standard     ($500 max_risk)\n"
            "  /go big          ($2000 max_risk)\n"
            "  /go micro        ($200 micro)\n"
            "  /presets         (recipes + past Excel results)\n"
            "  /start_round 500\n"
            "\n"
            "After a round is running, tweak with:\n"
            "  /set_max_pos 8\n"
            "  /set_min_trade 5\n"
            "  /set_kelly 0.5\n"
            "  /profile max_risk_micro"
        )
    kelly = float(_live("KELLY_FRACTION", cfg.KELLY_FRACTION))
    max_pos = float(_live("MAX_POS_PCT", cfg.MAX_POS_PCT))
    cap = float(_live("PORTFOLIO_GROSS_CAP", cfg.PORTFOLIO_GROSS_CAP))
    min_trade = float(_live("MIN_TRADE_USD", cfg.MIN_TRADE_USD))
    lo = float(_live("MIN_ENTRY_PRICE", cfg.MIN_ENTRY_PRICE))
    hi = float(_live("MAX_ENTRY_PRICE", cfg.MAX_ENTRY_PRICE))
    profile = str(_live("CONFIG_PROFILE", cfg.CONFIG_PROFILE))
    return "\n".join(
        [
            "Current sizing (live bot)",
            f"profile={profile}",
            f"KELLY_FRACTION={kelly}",
            f"MAX_POS_PCT={max_pos:.2%}",
            f"PORTFOLIO_GROSS_CAP={cap:.2%}",
            f"MIN_TRADE_USD=${min_trade:.2f}",
            f"entry band=[{lo:.2f}, {hi:.2f}]",
            "",
            "Examples:",
            "  /set_max_pos 8",
            "  /set_min_trade 5",
            "  /set_kelly 0.5",
            "  /profile max_risk_micro",
            "",
            "New round instead: /go standard   or   /start_round 500",
        ]
    )


def format_trade_alert(trade: dict[str, Any]) -> str:
    pnl = float(trade.get("pnl") or 0)
    sign = "+" if pnl >= 0 else ""
    return (
        f"Trade closed {'WIN' if pnl > 0 else 'LOSS'}\n"
        f"{trade.get('asset')} {str(trade.get('side', '?')).upper()} "
        f"{trade.get('ticker')}\n"
        f"entry={trade.get('entry')} exit={trade.get('exit')} ×{trade.get('contracts')}\n"
        f"PnL {sign}{_money(pnl)} | bal {_money(float(trade.get('balance') or 0))} "
        f"| vault {_money(float(trade.get('vault') or 0))} "
        f"| eq {_money(float(trade.get('equity') or 0))}"
    )


def format_skim_alert(skim: dict[str, Any]) -> str:
    return (
        f"Vault skim ({skim.get('reason', '?')})\n"
        f"Moved {_money(float(skim.get('amount') or 0))} → vault "
        f"{_money(float(skim.get('vault_after') or 0))}\n"
        f"Trading now {_money(float(skim.get('balance_after') or 0))} | "
        f"Equity {_money(float(skim.get('equity_after') or 0))}"
    )


def format_halt_alert(reason: str, halted: bool) -> str:
    if halted:
        return f"HALT — {reason or 'unknown'}\nNew entries blocked until clear / resume."
    return "Halt cleared — trading may resume (unless /pause)."


def format_stop_ack(*, bot_running: bool) -> str:
    """Immediate hard confirmation for /stop (before/while archive)."""
    p = load_portfolio()
    equity = p.total_equity or (p.balance + p.vault_balance)
    mode = "PAPER" if bool(_live("DRY_RUN", cfg.DRY_RUN)) else "LIVE"
    lines = [
        "STOP confirmed",
        f"Mode: {mode}",
        f"Trading bot: {'RUNNING (shutting down now)' if bot_running else 'NOT RUNNING'}",
        f"Trading {_money(p.balance)} | Vault {_money(p.vault_balance)} | Equity {_money(equity)}",
        f"Trades {p.total_trades} | {p.wins}W/{p.losses}L | start {_money(p.starting_balance)}",
    ]
    if bot_running:
        lines.append("Next: archive + Excel update, then bot exits.")
    else:
        lines.append("Next: archiving logs + Excel from Telegram bridge now.")
    return "\n".join(lines)


def format_archive_notice(notice: dict[str, Any]) -> str:
    summary = notice.get("summary") or {}
    start = float(summary.get("starting_balance") or 0)
    end = float(summary.get("ending_balance") or 0)
    trades = int(summary.get("real_trades") or 0)
    wins = int(summary.get("wins") or 0)
    losses = int(summary.get("losses") or 0)
    vault = 0.0
    equity = end
    try:
        import json
        from pathlib import Path

        from kalshi_bot.session_meta import PROJECT_ROOT

        arch = notice.get("archive_dir")
        candidates = []
        if arch:
            candidates.append(PROJECT_ROOT / str(arch) / "kalshi_sim.json")
        candidates.append(PROJECT_ROOT / "logs" / "kalshi_sim.json")
        for sim_path in candidates:
            if sim_path.exists():
                sim = json.loads(sim_path.read_text(encoding="utf-8"))
                vault = float(sim.get("vault_balance") or 0)
                equity = float(sim.get("total_equity") or (end + vault))
                break
    except Exception:
        pass
    excel = notice.get("excel_path") or "(excel update failed - close workbook and rebuild)"
    note = notice.get("excel_note")
    lines = [
        "STOP complete — round logged",
        f"Tag: {notice.get('session_tag')}",
        f"Archive: {notice.get('archive_dir')}",
        f"Trading: {_money(start)} -> {_money(end)}",
        f"Vault: {_money(vault)} | Equity: {_money(equity)}",
        f"Trades: {trades} ({wins}W/{losses}L)",
        f"Excel: {excel}",
    ]
    if note:
        lines.append(f"Note: {note}")
    return "\n".join(lines)
