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

from . import formatters as fmt
from kalshi_bot.safe_live import format_account, preflight_account

from .rounds import format_history, format_presets, format_regimes

log = logging.getLogger("kalshi_bot.telegram.handlers")

SendFn = Callable[[str], None]

def handle_command(text: str, send: SendFn, chat_id: str | None = None) -> None:
    from kalshi_bot.operator_control import (
        LIVE_ALIASES, PAPER_ALIASES, SETTING_RULES, audit,
        confirm_live, confirm_paper, confirm_profile, confirm_resume,
        confirm_setting, confirm_shadow, confirm_take_cash, confirm_vault,
        request_live, request_paper, request_profile_change, request_resume,
        request_setting, request_shadow, request_take_cash, request_vault_auto,
        request_vault_set,
    )
    from kalshi_bot.telegram.settings import load_settings

    text = (text or "").strip()
    if not text.startswith("/"):
        send("Send /help for commands.")
        return

    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]
    settings = load_settings()
    if not settings.chat_id or str(chat_id) != str(settings.chat_id):
        audit(str(chat_id or ""), cmd, "auth", "refused", "unauthorized chat")
        log.warning("Ignored command from unauthorized chat")
        send("Unauthorized.")
        return

    try:
        if cmd in ("/start", "/help"):
            send(fmt.format_help())
        elif cmd in LIVE_ALIASES:
            send("That command does not start trading. Use /start_live.")
        elif cmd in PAPER_ALIASES:
            send("That command does not start a round. Use /papertrade <capital> [profile].")
        elif cmd == "/start_live":
            request_live(str(chat_id), send)
        elif cmd == "/confirm_live":
            confirm_live(str(chat_id), args[0] if args else "", send)
        elif cmd == "/resume":
            request_resume(str(chat_id), send)
        elif cmd == "/confirm_resume":
            confirm_resume(str(chat_id), args[0] if args else "", send)
        elif cmd == "/papertrade":
            request_paper(str(chat_id), args, send)
        elif cmd == "/confirm_paper":
            confirm_paper(str(chat_id), args[0] if args else "", send)
        elif cmd == "/shadow":
            request_shadow(str(chat_id), args, send)
        elif cmd == "/confirm_shadow":
            confirm_shadow(str(chat_id), args[0] if args else "", send)
        elif cmd == "/take_cash":
            request_take_cash(str(chat_id), args, send)
        elif cmd == "/confirm_take_cash":
            confirm_take_cash(str(chat_id), args[0] if args else "", send)
        elif cmd == "/vault_auto":
            request_vault_auto(str(chat_id), args, send)
        elif cmd == "/vault_set":
            request_vault_set(str(chat_id), args, send)
        elif cmd == "/confirm_vault":
            confirm_vault(str(chat_id), args[0] if args else "", send)
        elif cmd in {f"/{name}" for name in SETTING_RULES}:
            request_setting(str(chat_id), cmd[1:], args, send)
        elif cmd == "/confirm_set":
            confirm_setting(str(chat_id), args[0] if args else "", send)
        elif cmd == "/bankroll" and not args:
            from kalshi_bot.bankroll import trading_bankroll_cap
            amount = trading_bankroll_cap()
            send("Trading bankroll unset. Live sizes from tradeable cash." if amount is None else f"Trading bankroll ${amount:,.2f}")
        elif cmd == "/bankroll":
            from kalshi_bot.operator_control import request_bankroll
            request_bankroll(str(chat_id), args, send)
        elif cmd == "/confirm_bankroll":
            from kalshi_bot.operator_control import confirm_bankroll
            confirm_bankroll(str(chat_id), args[0] if args else "", send)
        elif cmd == "/profile" and args:
            request_profile_change(str(chat_id), args[0], send)
        elif cmd == "/confirm_profile":
            confirm_profile(str(chat_id), args[0] if args else "", send)
        elif cmd in ("/presets", "/recipes"):
            send(format_presets())
        elif cmd in ("/history", "/rounds"):
            n = int(args[0]) if args else 8
            send(format_history(max(1, min(n, 20))))
        elif cmd == "/status":
            send(fmt.format_status())
        elif cmd in ("/account", "/kalshi"):
            send("Checking Kalshi account…")
            try:
                send(format_account(preflight_account()))
            except Exception as e:
                log.exception("account preflight failed")
                send(f"Account check failed: {e}")
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
        elif cmd in ("/losses", "/ledger"):
            from kalshi_bot.risk.loss_ledger import format_loss_ledger
            send(format_loss_ledger())
        elif cmd == "/vault":
            send(fmt.format_vault())
        elif cmd == "/summary":
            send(fmt.format_summary())
        elif cmd == "/sizing":
            send(fmt.format_sizing())
        elif cmd == "/pause":
            enqueue_bot_command("pause", source="telegram")
            send("Queued /pause — new entries will stop; open positions still manage/settle.")
        elif cmd == "/stop":
            from kalshi_bot.process_ownership import request_graceful_stop
            result = request_graceful_stop()
            note = "Stopping the process does not close Kalshi positions."
            outcome = result.get("outcome")
            if outcome == "stopped":
                send(f"Stop verified for {result.get('mode')} pid {result.get('pid')}.\n{note}")
            elif outcome == "already_stopped":
                send(f"Owned trader is already stopped ({result.get('detail')}).\n{note}")
            elif outcome == "timeout":
                send(f"Stop timed out for {result.get('mode')} pid {result.get('pid')}. Process was not killed.\n{note}")
            else:
                send(f"Stop uncertain: {result.get('detail')}\n{note}")
        elif cmd in ("/regimes", "/styles"):
            send(format_regimes())
        elif cmd == "/profile":
            send(
                "Profile is read-only unless you name one.\n"
                f"Allowed: {', '.join(PROFILE_PRESETS)}\n"
                "Change: /profile <name> then /confirm_profile <challenge>"
            )
        else:
            send(f"Unknown command {cmd}. Try /help")
    except ValueError:
        send("Could not parse numbers in that command. Check /help.")
    except Exception as e:
        log.exception("command failed: %s", cmd)
        send(f"Error: {e}")
