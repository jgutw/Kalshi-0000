"""
verify_live_vault.py — prove the live vault reservation holds.

Exercises LiveGuard._apply_sync in isolation (no network, no live account):
  1. vault == 0 reproduces the old behavior byte for byte
  2. vaulted cash is removed from the sizing bankroll and stays removed
  3. equity no longer double-counts, so drawdown reads true
  4. auto-skim cannot run away across repeated closes
  5. a vault larger than the account is clamped
  6. a carried-over paper vault is discarded on a fresh live book
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kalshi_bot.vault as vault_mod
from kalshi_bot.live_guard import LiveGuard
from kalshi_bot.sim_state import SimState

# take_cash() appends to logs/vault_skims.jsonl, which the Telegram AlertWatcher
# tails — an earlier run of this script pushed seven fake skim alerts to the
# phone. Divert to memory so a self-test can never write to a real ledger.
SKIMS: list[dict] = []
vault_mod._append_skim = SKIMS.append

_REAL_LEDGER = Path(__file__).resolve().parent.parent / "logs" / "vault_skims.jsonl"
LEDGER_ROWS_AT_START = (
    len([l for l in _REAL_LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()])
    if _REAL_LEDGER.exists() else 0
)

FAILURES: list[str] = []


def check(label: str, got, want, tol: float = 1e-6) -> None:
    ok = abs(float(got) - float(want)) <= tol if isinstance(want, (int, float)) else got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(label)


def guard(sim: SimState) -> LiveGuard:
    return LiveGuard(kalshi=None, sim=sim)  # _apply_sync never touches the client


def fresh(balance: float = 1000.0, vault: float = 0.0) -> SimState:
    s = SimState()
    s.balance = balance
    s.starting_balance = balance
    s.daily_start = balance
    s.peak_balance = balance
    s.peak_equity = balance + vault
    s.vault_balance = vault
    return s


print("\n[1] vault == 0 -> unchanged legacy behavior")
s = fresh(1000.0, vault=0.0)
guard(s)._apply_sync(available=900.0, portfolio=100.0, open_positions=[])
check("balance == full available", s.balance, 900.0)
check("kalshi_available", s.kalshi_available, 900.0)
check("no halt", s.live_halt_reason or "", "")

print("\n[2] vaulted cash leaves the sizing bankroll and stays out")
s = fresh(1000.0, vault=0.0)
g = guard(s)
g._apply_sync(available=1200.0, portfolio=0.0, open_positions=[])  # $200 profit
check("bankroll before skim", s.balance, 1200.0)
moved = s.take_cash(200.0, reason="test")
check("skim moved", moved, 200.0)
check("balance right after skim", s.balance, 1000.0)
g._apply_sync(available=1200.0, portfolio=0.0, open_positions=[])  # where the old bug hit
check("balance still reserved after resync", s.balance, 1000.0)
check("vault intact", s.vault_balance, 200.0)

print("\n[3] equity does not double-count; drawdown reads true")
check("equity == real Kalshi cash", s.total_equity, 1200.0)
g._apply_sync(available=1000.0, portfolio=0.0, open_positions=[])  # lost $200 trading
check("balance absorbs the loss, not the vault", s.balance, 800.0)
check("equity tracks the loss", s.total_equity, 1000.0)
check("drawdown is the true 16.7%", round(s.peak_drawdown, 4), 0.1667, tol=1e-3)

print("\n[4] auto-skim cannot run away across repeated closes")
s = fresh(1000.0, vault=0.0)
g = guard(s)
g._apply_sync(available=1000.0, portfolio=0.0, open_positions=[])
s.balance = 1300.0  # $300 profit sitting in the book; Kalshi cash stays 1300
for _ in range(5):
    s.take_cash(100.0, reason="auto")
    g._apply_sync(available=1300.0, portfolio=0.0, open_positions=[])
check("vault stops at the real profit", s.vault_balance, 300.0)
check("balance floors at starting capital", s.balance, 1000.0)
check("equity still real", s.total_equity, 1300.0)

print("\n[5] vault larger than the account is clamped")
s = fresh(1000.0, vault=1000.0)
g = guard(s)
g._apply_sync(available=400.0, portfolio=0.0, open_positions=[])
check("vault clamped to account", s.vault_balance, 400.0)
check("tradeable floored at 0", s.balance, 0.0)
check("halts as vault_locked", (s.live_halt_reason or "").startswith("live_vault_locked"), True)
g._apply_sync(available=1000.0, portfolio=0.0, open_positions=[])
check("halt self-clears once tradeable", s.live_halt_reason or "", "")

print("\n[6] carried-over paper vault is discarded on a fresh live book")
s = fresh(500.0, vault=1000.0)
s.total_trades = 0
s.trades = []
g = guard(s)
g._fetch = lambda: (True, 250.0, 0.0)
import kalshi_bot.live_guard as lg
lg.cfg.DRY_RUN = False
ok, msg = g.bootstrap()
check("bootstrap ok", ok, True)
check("paper vault discarded", s.vault_balance, 0.0)
check("bankroll == real cash", s.balance, 250.0)

print("\n[7] the self-test wrote nothing to the real skim ledger")
real_log = Path(__file__).resolve().parent.parent / "logs" / "vault_skims.jsonl"
before = len([l for l in real_log.read_text(encoding="utf-8").splitlines() if l.strip()]) if real_log.exists() else 0
check("skims captured in memory", len(SKIMS) > 0, True)
check("real ledger untouched by this run", before, LEDGER_ROWS_AT_START)

print("\n" + ("ALL CHECKS PASSED" if not FAILURES else f"FAILED: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
