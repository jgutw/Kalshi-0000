"""
verify_live_breaker.py — the consecutive-loss breaker is <= 3 on any live start.

start_live_safe() defaults to max_risk_micro (legacy breaker of 8), so checking
only the live_safe preset would miss the realistic path to real money.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kalshi_bot.config import cfg
from kalshi_bot.runtime_control import PROFILE_PRESETS, apply_profile

CEILING = int(getattr(cfg, "LIVE_MAX_CONSEC_LOSSES", 3))
FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(label)


print(f"\n[live] every profile clamps to <= {CEILING} when DRY_RUN is False")
cfg.DRY_RUN = False
for name in PROFILE_PRESETS:
    ok, _ = apply_profile(name)
    assert ok, name
    got = int(cfg.MAX_CONSEC_LOSSES)
    print(f"  {'PASS' if got <= CEILING else 'FAIL'}  {name}: breaker={got}")
    if got > CEILING:
        FAILURES.append(f"live {name}")

print("\n[live] the default profile used by start_live_safe()")
apply_profile("max_risk_micro")  # start_live_safe(profile="max_risk_micro")
check("max_risk_micro breaker under live", int(cfg.MAX_CONSEC_LOSSES), CEILING)

print("\n[live] live_safe preset value itself")
apply_profile("live_safe")
check("live_safe breaker", int(cfg.MAX_CONSEC_LOSSES), 3)
check("live_safe daily loss", round(float(cfg.MAX_DAILY_LOSS_PCT), 4), 0.15)

print("\n[paper] presets keep their own breaker — no paper regime is altered")
cfg.DRY_RUN = True
expected = {
    "max_risk_paper": 8,
    "max_risk_micro": 8,
    "engineered_risk": 4,
    "live_safe": 3,
}
for name, want in expected.items():
    apply_profile(name)
    check(f"paper {name}", int(cfg.MAX_CONSEC_LOSSES), want)


from kalshi_bot.sim_state import SimState


def losing_book(n: int, assets: list[str]) -> SimState:
    """n straight losses spread across `assets`, one contract each."""
    s = SimState()
    # Seed every capital anchor, or peak_equity keeps its cfg.SIM_BALANCE default
    # and the drawdown halt fires before the breaker is ever consulted.
    s.balance = s.starting_balance = s.daily_start = 1000.0
    s.peak_balance = s.peak_equity = 1000.0
    for i in range(n):
        s.record("KX-TEST", assets[i % len(assets)], entry=0.50, exit_=0.0, contracts=1)
    return s


print("\n[live] streak is counted across the book, not per asset")
cfg.DRY_RUN = False
apply_profile("live_safe")
check("per-asset breaker off in live", cfg.PER_ASSET_CIRCUIT_BREAKER, False)
s = losing_book(3, ["BTC", "ETH", "SOL"])  # 3 different assets, 1 loss each
halted, reason = s.is_halted(asset="XRP")
check("3 losses on 3 assets halts the book", halted, True)
check("halt names the streak", reason.startswith("consec_losses(3>=3)"), True)
check("2 losses do not halt", losing_book(2, ["BTC", "ETH"]).is_halted(asset="BTC")[0], False)

print("\n[live] the halt is sticky — no timed resume")
s._halted_at -= 999 * 60  # pretend a very long cooldown has elapsed
check("still halted long after any cooldown", s.is_halted(asset="BTC")[0], True)
check("sticky via live_halt_reason", s.live_halt_reason.startswith("consec_losses"), True)

print("\n[live] /resume clears it, and only it")
s.set_live_halt("live_open_divergence local_open=$50 vs $10")
cleared = s.clear_breaker_halt(source="telegram")
check("LiveGuard halt survives an operator resume", s.live_halt_reason.startswith("live_open_divergence"), True)
s.live_halt_reason = ""
s2 = losing_book(3, ["BTC", "ETH", "SOL"])
s2.is_halted(asset="BTC")
cleared = s2.clear_breaker_halt(source="telegram")
check("reset reports what it cleared", "streak 3" in cleared and "live halt" in cleared, True)
check("trading resumes after reset", s2.is_halted(asset="BTC")[0], False)
check("streak zeroed", s2.consec_losses, 0)

print("\n[live] a win still resets the streak on its own")
s3 = losing_book(2, ["BTC", "ETH"])
s3.record("KX-TEST", "SOL", entry=0.50, exit_=1.0, contracts=1)
s3.record("KX-TEST", "BTC", entry=0.50, exit_=0.0, contracts=1)
s3.record("KX-TEST", "ETH", entry=0.50, exit_=0.0, contracts=1)
check("2 losses after a win do not halt", s3.is_halted(asset="BTC")[0], False)

print("\n[paper] per-asset scope and timed cooldown are unchanged")
cfg.DRY_RUN = True
apply_profile("engineered_risk")
check("per-asset breaker on in paper", cfg.PER_ASSET_CIRCUIT_BREAKER, True)
p = losing_book(4, ["BTC", "ETH", "SOL", "XRP"])  # 4 losses, 1 per asset
check("no single asset benched in paper", p.is_halted(asset="BTC")[0], False)
p2 = losing_book(4, ["BTC"])  # 4 straight on one asset
halted, reason = p2.is_halted(asset="BTC")
check("4 on one asset benches it", halted, True)
check("paper uses a timed cooldown", "cooldown" in reason, True)

print("\n" + ("ALL CHECKS PASSED" if not FAILURES else f"FAILED: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
