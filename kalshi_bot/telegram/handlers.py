"""Inbound Telegram command handlers."""

from __future__ import annotations

import logging
from typing import Callable

from kalshi_bot.runtime_control import (
    PROFILE_PRESETS,
    enqueue_bot_command,
    trading_bot_running,
)
from kalshi_bot.session_meta import archive_and_log_round, session_is_active
from kalshi_bot.vault import VaultConfig, enqueue_set_config, enqueue_take_cash, load_vault_config

from . import formatters as fmt
from .rounds import (
    RECIPES,
    format_history,
    format_presets,
    format_start_result,
    format_start_round_help,
    parse_start_round_args,
    start_paper_round,
)

log = logging.getLogger("kalshi_bot.telegram.handlers")

SendFn = Callable[[str], None]


def _parse_pct(raw: str) -> float:
    """Accept 0.08 or 8 (percent)."""
    v = float(raw.strip().rstrip("%"))
    if v > 1.0:
        v = v / 100.0
    return v


def _start_round_from_args(args: list[str], send: SendFn) -> None:
    if not args:
        send(format_start_round_help())
        return
    try:
        capital, profile = parse_start_round_args(args)
    except ValueError as e:
        send(f"{e}\n\n{format_start_round_help()}")
        return
    send(f"Starting paper round: ${capital:,.0f} | {profile} …")
    try:
        result = start_paper_round(capital, profile)
        send(format_start_result(result))
    except Exception as e:
        log.exception("start_round failed")
        send(f"Could not start round: {e}")


def handle_command(text: str, send: SendFn) -> None:
    text = (text or "").strip()
    if not text.startswith("/"):
        send("Send /help for commands.")
        return

    # Drop @botname suffix
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]

    try:
        if cmd in ("/start", "/help"):
            send(fmt.format_help())
        elif cmd in ("/start_round", "/new", "/new_round"):
            _start_round_from_args(args, send)
        elif cmd == "/go":
            if not args:
                send(format_presets())
                return
            name = args[0].lower()
            if name not in RECIPES:
                send(f"Unknown recipe '{name}'.\n\n{format_presets()}")
                return
            _start_round_from_args([name], send)
        elif cmd in ("/presets", "/recipes"):
            send(format_presets())
        elif cmd in ("/history", "/rounds"):
            n = int(args[0]) if args else 8
            send(format_history(max(1, min(n, 20))))
        elif cmd == "/status":
            send(fmt.format_status())
        elif cmd in ("/live", "/assets"):
            send(fmt.format_live())
        elif cmd == "/risk":
            send(fmt.format_risk())
        elif cmd == "/positions":
            send(fmt.format_positions())
        elif cmd == "/router":
            send(fmt.format_router())
        elif cmd == "/why":
            send(fmt.format_why())
        elif cmd == "/trades":
            n = int(args[0]) if args else 5
            send(fmt.format_trades(max(1, min(n, 25))))
        elif cmd == "/windows":
            send(fmt.format_windows())
        elif cmd == "/vault":
            send(fmt.format_vault())
        elif cmd == "/summary":
            send(fmt.format_summary())
        elif cmd == "/sizing":
            send(fmt.format_sizing())
        elif cmd == "/take_cash":
            if not args:
                send("Usage: /take_cash <usd>\nExample: /take_cash 100")
                return
            amt = float(args[0])
            if amt <= 0:
                send("Amount must be > 0")
                return
            enqueue_take_cash(amt, reason="telegram")
            send(f"Queued take_cash ${_money(amt)} — bot will apply within a few seconds.")
        elif cmd == "/vault_auto":
            if not args or args[0].lower() not in ("on", "off"):
                send("Usage: /vault_auto on|off")
                return
            vcfg = load_vault_config()
            vcfg.auto_enabled = args[0].lower() == "on"
            enqueue_set_config(vcfg)
            send(f"Vault auto -> {'ON' if vcfg.auto_enabled else 'OFF'}")
        elif cmd == "/vault_set":
            if len(args) < 2:
                send("Usage: /vault_set <profit_trigger> <skim_amount>\nExample: /vault_set 200 100")
                return
            trigger = float(args[0])
            skim = float(args[1])
            vcfg = VaultConfig(
                auto_enabled=load_vault_config().auto_enabled,
                profit_trigger=trigger,
                skim_amount=skim,
            ).clamp()
            enqueue_set_config(vcfg)
            send(
                f"Vault auto rule saved: profit >= ${_money(vcfg.profit_trigger)} "
                f"-> skim ${_money(vcfg.skim_amount)}"
            )
        elif cmd == "/pause":
            enqueue_bot_command("pause", source="telegram")
            send("Queued /pause — new entries will stop; open positions still manage/settle.")
        elif cmd == "/resume":
            enqueue_bot_command("resume", source="telegram")
            send("Queued /resume — new entries allowed (unless halted).")
        elif cmd == "/stop":
            running = trading_bot_running()
            if not running and not session_is_active():
                send(fmt.format_idle_status())
                return
            send(fmt.format_stop_ack(bot_running=running))
            if running:
                enqueue_bot_command("stop", source="telegram")
                # Final "STOP complete" comes from telegram_notices after bot archives.
            else:
                try:
                    # Bridge sends the confirmation itself — don't also emit
                    # telegram_notices (that caused duplicate STOP complete msgs).
                    notice = archive_and_log_round(
                        source="telegram_stop_bridge",
                        emit_telegram_notice=False,
                        mark_idle=True,
                    )
                    send(fmt.format_archive_notice(notice))
                except Exception as e:
                    log.exception("bridge /stop archive failed")
                    send(f"STOP archive failed: {e}")
        elif cmd == "/set_max_pos":
            if not args:
                send(
                    "Mid-round tweak — set max % of bankroll per trade.\n"
                    "Example: /set_max_pos 8\n"
                    "To start a new round instead: /go standard"
                )
                return
            if not trading_bot_running():
                send("No running bot. Start a round first: /go standard")
                return
            val = _parse_pct(args[0])
            enqueue_bot_command("set_max_pos", value=val, source="telegram")
            send(f"Queued MAX_POS_PCT={val:.2%}")
        elif cmd == "/set_min_trade":
            if not args:
                send(
                    "Mid-round tweak — minimum dollars per trade.\n"
                    "Example: /set_min_trade 5\n"
                    "New round: /start_round 500"
                )
                return
            if not trading_bot_running():
                send("No running bot. Start a round first: /go standard")
                return
            val = float(args[0])
            enqueue_bot_command("set_min_trade", value=val, source="telegram")
            send(f"Queued MIN_TRADE_USD=${val:.2f}")
        elif cmd == "/set_kelly":
            if not args:
                send(
                    "Mid-round tweak — Kelly fraction 0-1.\n"
                    "Example: /set_kelly 0.5\n"
                    "New round: /go big"
                )
                return
            if not trading_bot_running():
                send("No running bot. Start a round first: /go standard")
                return
            val = float(args[0])
            enqueue_bot_command("set_kelly", value=val, source="telegram")
            send(f"Queued KELLY_FRACTION={val:.2f}")
        elif cmd == "/set_portfolio_cap":
            if not args:
                send(
                    "Mid-round tweak — total open exposure cap.\n"
                    "Example: /set_portfolio_cap 30"
                )
                return
            if not trading_bot_running():
                send("No running bot. Start a round first: /go standard")
                return
            val = _parse_pct(args[0])
            enqueue_bot_command("set_portfolio_cap", value=val, source="telegram")
            send(f"Queued PORTFOLIO_GROSS_CAP={val:.2%}")
        elif cmd == "/profile":
            if not args:
                send(
                    "Mid-round profile switch (does not reset balance).\n"
                    f"Presets: {', '.join(PROFILE_PRESETS)}\n"
                    "Example: /profile max_risk_micro\n"
                    "For a fresh round: /go micro"
                )
                return
            if not trading_bot_running():
                send("No running bot. Start a round first: /go standard")
                return
            name = args[0].strip().lower()
            if name not in PROFILE_PRESETS:
                send(f"Unknown profile. Try: {', '.join(PROFILE_PRESETS)}")
                return
            enqueue_bot_command("profile", name=name, source="telegram")
            send(f"Queued profile={name} (bot applies sizing/gates preset)")
        else:
            send(f"Unknown command {cmd}. Try /help")
    except ValueError:
        send("Could not parse numbers in that command. Check /help.")
    except Exception as e:
        log.exception("command failed: %s", cmd)
        send(f"Error: {e}")


def _money(x: float) -> str:
    return f"{x:,.2f}"
