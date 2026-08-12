"""
verify_start_typo.py — /start with arguments no longer silently prints help.

start_paper_round is stubbed, so nothing is spawned and no session is touched.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kalshi_bot.telegram.handlers as h

STARTED: list[tuple[float, str]] = []
h.start_paper_round = lambda capital, profile: STARTED.append((capital, profile)) or {
    "session_tag": "stub", "capital": capital, "profile": profile
}
h.format_start_result = lambda result: f"[started {result['capital']:.0f} {result['profile']}]"

FAILURES: list[str] = []


def run(text: str) -> str:
    STARTED.clear()
    out: list[str] = []
    h.handle_command(text, out.append)
    return "\n".join(out)


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(label)


print("\n[typo] '/start round 5000' now starts the round it obviously meant")
reply = run("/start round 5000")
check("round started", STARTED, [(5000.0, "max_risk_paper")])
check("correction is stated", "/start_round" in reply, True)
check("help is not dumped", "AFTER OUTAGE" not in reply, True)

print("\n[typo] '/start round 5000 engineered' keeps the profile")
run("/start round 5000 engineered")
check("profile resolved", STARTED, [(5000.0, "engineered_risk")])

print("\n[typo] a bare amount works too")
run("/start 200")
check("small capital picks micro", STARTED, [(200.0, "max_risk_micro")])

print("\n[typo] a recipe name is redirected to /go")
reply = run("/start engineered")
check("recipe started", STARTED, [(500.0, "engineered_risk")])
check("points at /go", "/go engineered" in reply, True)

print("\n[safe] unrecognized args explain instead of starting anything")
reply = run("/start please help me")
check("nothing started", STARTED, [])
check("says it was ignored", "was ignored" in reply, True)
check("says nothing started", "nothing started" in reply, True)

print("\n[unchanged] bare /start and /help still print help")
reply = run("/start")
check("bare /start prints help", "AFTER OUTAGE" in reply, True)
check("nothing started", STARTED, [])
reply = run("/help")
check("/help prints help", "AFTER OUTAGE" in reply, True)

print("\n[unchanged] the real command still works")
run("/start_round 5000 engineered")
check("/start_round unaffected", STARTED, [(5000.0, "engineered_risk")])

print("\n" + ("ALL CHECKS PASSED" if not FAILURES else f"FAILED: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
