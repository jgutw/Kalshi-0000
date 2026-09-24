"""Inbound Telegram command handlers."""

from __future__ import annotations

import logging
from typing import Callable

from kalshi_bot.runtime_control import (
    PROFILE_PRESETS,
    enqueue_bot_command,
    profile_description,
    trading_bot_running,
)
from kalshi_bot.session_meta import archive_and_log_round, session_is_active
from kalshi_bot.vault import VaultConfig, enqueue_set_config, enqueue_take_cash, load_vault_config

from . import formatters as fmt
from kalshi_bot.safe_live import (
    format_account,
    format_live_start_result,
    preflight_account,
    start_live_safe,
)

from .rounds import (
    RECIPES,
    format_history,
    format_presets,
    format_regimes,
    format_start_result,
    format_start_round_help,
    parse_start_round_args,
    start_paper_round,
)

log = logging.getLogger("kalshi_bot.telegram.handlers")

SendFn = Callable[[str], None]

# Remote commands that start trading, move cash, or raise exposure.
# Status, pause, and stop stay available. Local CLI start is unchanged.
_EXPOSURE_COMMANDS = {
    "/resume_live", "/start_live", "/safe_live",
    "/go", "/start_round", "/new", "/new_round",
    "/take_cash", "/vault_auto", "/vault_set",
    "/resume",
    "/set_max_pos", "/set_min_trade", "/set_kelly", "/set_portfolio_cap",
    "/set_consec_losses", "/set_daily_loss", "/set_max_drawdown",
    "/profile",
}
_EXPOSURE_REFUSAL = (
    "Refused. This Telegram release can report status and pause or stop the bot. "
    "It cannot start trading, move cash, or raise exposure."
)


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


def _handle_start_with_args(args: list[str], send: SendFn) -> None:
    """
    /start takes no arguments, so "/start round 5000" used to print help and
    silently drop the rest — one missing underscore away from /start_round.
    Act on the clear cases, and say what was ignored on the rest.
    """
    head = args[0].lower().lstrip("/")
    if head in ("round", "_round", "new", "new_round", "start_round"):
        send("Heads up: the command is /start_round (underscore). Starting it for you…")
        _start_round_from_args(args[1:], send)
        return
    if head in RECIPES:
        send(f"Heads up: that recipe is /go {head}. Starting it for you…")
        _start_round_from_args([head], send)
        return
    try:
        float(head.replace("$", "").replace(",", ""))
    except ValueError:
        send(
            f"/start takes no arguments, so \"{' '.join(args)}\" was ignored — "
            "nothing started.\n\n"
            "To start a paper round:\n"
            "  /start_round 5000\n"
            "  /go standard\n\n"
            "Send /help for the full list."
        )
        return
    send("Heads up: the command is /start_round. Starting it for you…")
    _start_round_from_args(args, send)


def handle_command(text: str, send: SendFn) -> None:
    text = (text or "").strip()
    if not text.startswith("/"):
        send("Send /help for commands.")
        return

    # Drop @botname suffix
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]
    if cmd in _EXPOSURE_COMMANDS or (cmd == "/start" and args):
        send(_EXPOSURE_REFUSAL)
        return

    try:
        if cmd in ("/start", "/help"):
            if cmd == "/start" and args:
                _handle_start_with_args(args, send)
                return
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
        elif cmd in ("/account", "/kalshi"):
            send("Checking Kalshi account…")
            try:
                send(format_account(preflight_account()))
            except Exception as e:
                log.exception("account preflight failed")
                send(f"Account check failed: {e}")
        elif cmd in ("/resume_live", "/start_live", "/safe_live"):
            # /resume_live [profile] [force]
            profile = "max_risk_micro"
            force = False
            for a in args:
                al = a.lower()
                if al in ("force", "force_opens", "with_opens"):
                    force = True
                else:
                    profile = a
            send(
                f"Safe LIVE resume: profile={profile}"
                f"{' FORCE opens' if force else ''} …"
            )
            try:
                result = start_live_safe(profile, force_with_opens=force)
                send(format_live_start_result(result))
            except Exception as e:
                log.exception("resume_live failed")
                send(f"Could not start live: {e}")
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
            send(
                "Queued /resume — new entries allowed, and a tripped "
                "consecutive-loss breaker is reset.\n"
                "LiveGuard halts (balance divergence, cash too low) are not "
                "cleared by this; they lift on their own when healthy."
            )
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
        elif cmd == "/set_consec_losses":
            if not args:
                send(
                    "Usage: /set_consec_losses 4   (tightening only)\n"
                    "Paper allows 2-8. Live is capped at 3 and counts the streak "
                    "across the whole book, with no timed resume."
                )
                return
            val = int(float(args[0]))
            enqueue_bot_command("set_consec_losses", value=val, source="telegram")
            send(f"Queued MAX_CONSEC_LOSSES={val} (breaker trips after {val} straight losses)")
        elif cmd == "/set_daily_loss":
            if not args:
                send("Usage: /set_daily_loss 25   (percent of equity, 5-50)")
                return
            val = _parse_pct(args[0])
            enqueue_bot_command("set_daily_loss", value=val, source="telegram")
            send(f"Queued MAX_DAILY_LOSS_PCT={val:.2%}")
        elif cmd == "/set_max_drawdown":
            if not args:
                send("Usage: /set_max_drawdown 30   (percent of equity, 5-60)")
                return
            val = _parse_pct(args[0])
            enqueue_bot_command("set_max_drawdown", value=val, source="telegram")
            send(f"Queued MAX_DRAWDOWN_PCT={val:.2%}")
        elif cmd in ("/regimes", "/styles"):
            send(format_regimes())
        elif cmd == "/profile":
            if not args:
                send(
                    "Mid-round profile switch (does not reset balance).\n"
                    f"Presets: {', '.join(PROFILE_PRESETS)}\n"
                    "Example: /profile max_risk_micro\n"
                    "What each one means: /regimes\n"
                    "For a fresh round: /go micro"
                )
                return
            if not trading_bot_running():
                send("No running bot. Start a round first: /go standard")
                return
            name = args[0].strip().lower()
            if name not in PROFILE_PRESETS:
                send(f"Unknown profile. Try: {', '.join(PROFILE_PRESETS)}\nSee /regimes")
                return
            enqueue_bot_command("profile", name=name, source="telegram")
            send(
                f"Queued profile={name} (bot applies sizing/gates preset)\n\n"
                + profile_description(name)
            )
        else:
            send(f"Unknown command {cmd}. Try /help")
    except ValueError:
        send("Could not parse numbers in that command. Check /help.")
    except Exception as e:
        log.exception("command failed: %s", cmd)
        send(f"Error: {e}")


def _money(x: float) -> str:
    return f"{x:,.2f}"
