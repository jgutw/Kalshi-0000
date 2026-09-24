"""
Easy paper-round setup from Telegram.

/start_round <capital> [profile]
/history
/presets
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from kalshi_bot.runtime_control import (
    PROFILE_PRESETS,
    prepare_for_new_round,
    trading_bot_running,
)
from kalshi_bot.rounds_excel import SESSIONS_DIR, summarize_session, iter_session_dirs
from kalshi_bot.session_meta import PROJECT_ROOT, session_is_active

log = logging.getLogger("kalshi_bot.telegram.rounds")

# One-tap recipes (capital + profile). Names are what you type after /start_round
# or /go <name>.
RECIPES: dict[str, dict[str, Any]] = {
    "micro": {
        "capital": 200.0,
        "profile": "max_risk_micro",
        "blurb": "Small book $200, micro sizing (min trade $2, max pos 10%)",
    },
    "micro100": {
        "capital": 100.0,
        "profile": "max_risk_micro",
        "blurb": "Tiny book $100, micro sizing",
    },
    "standard": {
        "capital": 500.0,
        "profile": "max_risk_paper",
        "blurb": "Classic max_risk $500 (R19-style)",
    },
    "mid": {
        "capital": 1000.0,
        "profile": "max_risk_paper",
        "blurb": "max_risk $1000",
    },
    "big": {
        "capital": 2000.0,
        "profile": "max_risk_paper",
        "blurb": "max_risk $2000 (R20-style)",
    },
    # Risk Update v1 recipes — added alongside the originals, which are unchanged.
    "engineered": {
        "capital": 500.0,
        "profile": "engineered_risk",
        "blurb": "$500 engineered_risk — same signals, ~half the position size",
    },
    "engineered_micro": {
        "capital": 200.0,
        "profile": "engineered_risk",
        "blurb": "$200 engineered_risk — small book, capped gross + lottery sleeve",
    },
    "ab": {
        "capital": 500.0,
        "profile": "engineered_risk",
        "blurb": "$500 engineered_risk, matched to /go standard for A/B comparison",
    },
}

PROFILE_ALIASES = {
    "micro": "max_risk_micro",
    "max_risk": "max_risk_paper",
    "max_risk_paper": "max_risk_paper",
    "max_risk_micro": "max_risk_micro",
    "paper": "max_risk_paper",
    # Risk Update v1
    "engineered": "engineered_risk",
    "engineered_risk": "engineered_risk",
    "safe": "engineered_risk",
    "live_safe": "live_safe",
    "standard": "max_risk_paper",
    "conservative": "engineered_risk",
    "aggressive": "max_risk_micro",
    "tight": "live_safe",
}


def next_round_number() -> int:
    """Scan session tags for round_N_... and return N+1."""
    best = 0
    pat = re.compile(r"(?:^|_)round_(\d+)(?:_|$)|(?:^|_)archive_(?:round_)?(\d+)_", re.I)
    # Also plain archive_20_max_risk...
    pat2 = re.compile(r"archive_(\d+)_")
    roots = []
    if SESSIONS_DIR.exists():
        roots.extend(SESSIONS_DIR.iterdir())
    meta = PROJECT_ROOT / "logs" / "session_meta.json"
    texts: list[str] = []
    for p in roots:
        if p.is_dir():
            texts.append(p.name)
            sm = p / "session_meta.json"
            if sm.exists():
                try:
                    texts.append(sm.read_text(encoding="utf-8"))
                except OSError:
                    pass
    if meta.exists():
        try:
            texts.append(meta.read_text(encoding="utf-8"))
        except OSError:
            pass
    for text in texts:
        for m in pat.finditer(text):
            for g in m.groups():
                if g:
                    best = max(best, int(g))
        for m in pat2.finditer(text):
            best = max(best, int(m.group(1)))
    return best + 1 if best else 21


def resolve_profile(name: str) -> str:
    key = name.strip().lower()
    if key in PROFILE_ALIASES:
        return PROFILE_ALIASES[key]
    if key in PROFILE_PRESETS:
        return key
    raise ValueError(
        f"Unknown profile '{name}'. Try: {', '.join(PROFILE_PRESETS)}"
    )


def load_history(limit: int = 8) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for d in iter_session_dirs():
        try:
            s = summarize_session(d)
        except Exception:
            continue
        if not s:
            continue
        r = s.get("round") or {}
        if int(r.get("trades") or 0) < 1:
            continue
        rows.append(r)
    # most recent first by archive folder mtime already in iter order? sort by archived_at
    rows.sort(key=lambda r: str(r.get("archived_at") or r.get("last_trade_ts") or ""), reverse=True)
    return rows[:limit]


def format_history(limit: int = 8) -> str:
    rows = load_history(limit)
    if not rows:
        return "History: no archived rounds with trades yet."
    lines = ["Past rounds (newest first)", "tag | start -> equity | WR | profile"]
    for r in rows:
        tag = str(r.get("session_tag") or "?")
        if len(tag) > 36:
            tag = tag[:33] + "..."
        start = float(r.get("start_$") or 0)
        eq = float(r.get("equity_end_$") or 0)
        pnl_pct = float(r.get("pnl_equity_%") or 0)
        wr = float(r.get("win_rate_%") or 0)
        profile = r.get("profile") or "?"
        trades = int(r.get("trades") or 0)
        lines.append(
            f"{tag}\n"
            f"  ${start:,.0f} -> ${eq:,.0f} ({pnl_pct:+.1f}%) | "
            f"{trades}t WR {wr:.0f}% | {profile}"
        )
    lines.append("")
    lines.append("Tip: /start_round 500   or   /go standard")
    return "\n".join(lines)


def format_presets() -> str:
    lines = [
        "Easy start recipes (type /go <name>)",
        "",
    ]
    for name, rec in RECIPES.items():
        lines.append(
            f"  /go {name}  -> ${rec['capital']:.0f} {rec['profile']}\n"
            f"      {rec['blurb']}"
        )
    lines.append("")
    lines.append("Or set capital yourself:")
    lines.append("  /start_round 500")
    lines.append("  /start_round 2000 max_risk_paper")
    lines.append("  /start_round 200 max_risk_micro")
    lines.append("")
    # Capital-bucket hints from history
    hist = load_history(12)
    if hist:
        lines.append("What worked recently (by start capital):")
        buckets: dict[str, list[dict]] = {}
        for r in hist:
            start = float(r.get("start_$") or 0)
            if start <= 300:
                b = "$100-300"
            elif start <= 750:
                b = "$500-ish"
            elif start <= 1500:
                b = "$1000-ish"
            else:
                b = "$2000+"
            buckets.setdefault(b, []).append(r)
        for b, items in buckets.items():
            best = max(items, key=lambda x: float(x.get("pnl_equity_%") or -999))
            lines.append(
                f"  {b}: best {best.get('session_tag')} "
                f"{float(best.get('pnl_equity_%') or 0):+.1f}% equity | "
                f"profile={best.get('profile') or '?'}"
            )
    lines.append("")
    lines.append("Mid-round tweaks (after started): /set_max_pos 8  /sizing")
    lines.append("Regime descriptions: /regimes")
    return "\n".join(lines)


def format_regimes() -> str:
    """Every trading style available, what it is, and whether it has been traded."""
    from kalshi_bot.runtime_control import PROFILE_META, profile_description

    lines = ["Trading regimes", ""]
    for name in PROFILE_PRESETS:
        if name in PROFILE_META:
            lines.append(profile_description(name))
        else:
            lines.append(f"{name}: no description on file.")
        lines.append("")
    lines.append("Start one:  /start_round 500 engineered_risk")
    lines.append("Switch mid-round:  /profile engineered_risk")
    return "\n".join(lines).rstrip()


def format_start_round_help() -> str:
    n = next_round_number()
    return (
        "Start a paper round from Telegram\n"
        "\n"
        f"Next round # will be ~{n}\n"
        "\n"
        "Easy:\n"
        "  /go standard     ($500 max_risk)\n"
        "  /go big          ($2000 max_risk)\n"
        "  /go micro        ($200 micro)\n"
        "\n"
        "Custom:\n"
        "  /start_round 500\n"
        "  /start_round 2000 max_risk_paper\n"
        "  /start_round 200 max_risk_micro\n"
        "\n"
        "See also: /presets   /history\n"
        "Must /stop an active round before starting another."
    )


def build_session_tag(round_n: int, profile: str, capital: float) -> str:
    cap = int(capital) if float(capital).is_integer() else capital
    return f"round_{round_n}_{profile}_{cap}"


def start_paper_round(
    capital: float,
    profile: str = "max_risk_paper",
    round_n: Optional[int] = None,
) -> dict[str, Any]:
    """
    Spawn run_kalshi_bot.py --fresh-round with the given capital/profile.
    Returns a result dict for Telegram formatting.
    """
    if capital != capital or capital in (float("inf"), float("-inf")) or capital < 50 or capital > 100_000:
        raise ValueError("Capital must be between $50 and $100000")
    profile = resolve_profile(profile)
    if profile not in PROFILE_PRESETS:
        raise ValueError(f"Unknown profile {profile}")

    from kalshi_bot.process_ownership import record_ownership, refuse_if_conflict, start_lock
    with start_lock():
        conflict = refuse_if_conflict("paper")
        if conflict:
            raise RuntimeError(conflict)
        if trading_bot_running():
            raise RuntimeError("heartbeat is fresh but trader identity is not confirmed")
        return _spawn_paper_round(capital, profile, round_n)


def _spawn_paper_round(capital, profile, round_n):
    if session_is_active():
        raise RuntimeError(
            "A session is still marked active. Send /stop first (archives + clears)."
        )

    # Critical: a leftover Telegram /stop in bot_control.jsonl will kill the
    # new bot within seconds of startup (this bit Round 21).
    prepare_for_new_round(source="telegram_start_round")

    n = int(round_n or next_round_number())
    tag = build_session_tag(n, profile, capital)
    py = sys.executable
    script = str(PROJECT_ROOT / "run_kalshi_bot.py")
    cmd = [
        py,
        script,
        "--mode",
        "run",
        "--fresh-round",
        "--sim-balance",
        str(capital),
        "--profile",
        profile,
        "--session-tag",
        tag,
    ]
    logs_dir = PROJECT_ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    out_path = logs_dir / "bot_stdout.log"
    out_f = open(out_path, "a", encoding="utf-8")
    out_f.write(f"\n\n===== start {tag} =====\n")
    out_f.flush()

    kwargs: dict[str, Any] = {
        "cwd": str(PROJECT_ROOT),
        "stdout": out_f,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        # Detach so bridge exit doesn't kill the bot
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

    log.info("Spawning paper bot: %s", " ".join(cmd))
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        from kalshi_bot.process_ownership import record_ownership
        record_ownership("paper", int(proc.pid), tag)
    except Exception as exc:
        log.warning("paper ownership was not recorded: %s", exc)

    # Wait briefly for heartbeat / meta
    ready = False
    for _ in range(25):
        time.sleep(0.4)
        if trading_bot_running() or session_is_active():
            ready = True
            break
        if proc.poll() is not None:
            break

    from kalshi_bot.runtime_control import profile_summary_line

    preset = PROFILE_PRESETS[profile]
    return {
        "ok": ready or proc.poll() is None,
        "pid": proc.pid,
        "session_tag": tag,
        "round": n,
        "capital": capital,
        "profile": profile,
        "profile_title": profile_summary_line(profile),
        "kelly": preset.get("KELLY_FRACTION"),
        "max_pos": preset.get("MAX_POS_PCT"),
        "min_trade": preset.get("MIN_TRADE_USD"),
        "gross_cap": preset.get("PORTFOLIO_GROSS_CAP"),
        "ready": ready,
        "exit_code": proc.poll(),
        "log": str(out_path.relative_to(PROJECT_ROOT)),
    }


def format_start_result(result: dict[str, Any]) -> str:
    lines = [
        "Paper round STARTING" if result.get("ready") else "Paper round LAUNCHED",
        f"Tag: {result.get('session_tag')}",
        f"Capital: ${float(result.get('capital') or 0):,.0f}",
        f"Profile: {result.get('profile')}"
        + (f" — {result.get('profile_title')}" if result.get("profile_title") else ""),
        f"Sizing: kelly={result.get('kelly')} max_pos={float(result.get('max_pos') or 0):.0%} "
        f"gross_cap={float(result.get('gross_cap') or 0):.0%} "
        f"min_trade=${float(result.get('min_trade') or 0):.0f}",
        f"PID: {result.get('pid')}",
    ]
    if result.get("ready"):
        lines.append("Bot heartbeat OK — try /status")
    elif result.get("exit_code") is not None:
        lines.append(
            f"Bot exited early (code {result.get('exit_code')}). Check {result.get('log')}"
        )
    else:
        lines.append("Bot spawning — wait ~10s then /status")
    lines.append("When done: /stop  (archives + Excel)")
    return "\n".join(lines)


def parse_start_round_args(args: list[str]) -> tuple[float, str]:
    """
    Forms:
      500
      500 max_risk_paper
      200 micro
      standard          (recipe name only)
    """
    if not args:
        raise ValueError("missing args")
    # Recipe-only
    if len(args) == 1 and args[0].lower() in RECIPES:
        rec = RECIPES[args[0].lower()]
        return float(rec["capital"]), str(rec["profile"])
    capital = float(args[0])
    profile = "max_risk_paper"
    if len(args) >= 2:
        # second token may be recipe profile alias or full profile
        tok = args[1].lower()
        if tok in RECIPES and tok not in PROFILE_ALIASES:
            # e.g. /start_round 500 standard -> ignore capital from recipe? prefer explicit capital
            profile = str(RECIPES[tok]["profile"])
        else:
            profile = resolve_profile(tok)
    elif capital <= 300:
        profile = "max_risk_micro"
    return capital, profile
