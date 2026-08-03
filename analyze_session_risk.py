"""
Offline risk reverse-engineering for an archived (or live) trade log.

Usage:
  python analyze_session_risk.py sessions/session_2026-08-01_1651
  python analyze_session_risk.py logs
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kalshi_bot.risk.drawdown import (
    analyze_counterfactuals,
    equity_path,
    loss_buckets,
    max_drawdown_from_balances,
)


def _load_trades(path: Path) -> list[dict]:
    trades_path = path / "kalshi_trades.jsonl" if path.is_dir() else path
    if not trades_path.exists():
        raise SystemExit(f"No trades file at {trades_path}")
    out = []
    for line in trades_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _start_balance(session_dir: Path, trades: list[dict]) -> float:
    sim_path = session_dir / "kalshi_sim.json" if session_dir.is_dir() else None
    if sim_path and sim_path.exists():
        try:
            sim = json.loads(sim_path.read_text(encoding="utf-8"))
            if sim.get("starting_balance") is not None:
                return float(sim["starting_balance"])
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass
    if trades and trades[0].get("balance") is not None and trades[0].get("pnl") is not None:
        return float(trades[0]["balance"]) - float(trades[0]["pnl"])
    return 1000.0


def main() -> None:
    ap = argparse.ArgumentParser(description="Session drawdown + counterfactual loss analysis")
    ap.add_argument("path", nargs="?", default="logs", help="Session dir or trades.jsonl")
    args = ap.parse_args()
    root = Path(args.path)
    trades = _load_trades(root)
    start = _start_balance(root, trades)
    if not trades:
        print("No trades found.")
        sys.exit(0)

    # Prefer real tickers only when side is present (skip startup junk without side)
    real = [t for t in trades if t.get("side")]
    use = real if real else trades

    path = equity_path(use, start)
    mdd = max_drawdown_from_balances(path, start)
    total_pnl = sum(float(t.get("pnl") or 0) for t in use)

    print(f"Session: {root}")
    print(f"Trades:  {len(use)} (of {len(trades)} rows)  start=${start:.2f}")
    print(f"PnL:     ${total_pnl:+.2f}  end~${start + total_pnl:.2f}")
    print(f"Max DD:  {mdd:.1%} peak-to-trough")
    print()
    print("=== Loss buckets ===")
    buckets = loss_buckets(use)
    print(json.dumps(buckets, indent=2))
    print()
    print("=== Counterfactuals (what if we skipped X?) ===")
    print(f"{'rule':<22} {'kept':>5} {'pnl':>10} {'end':>10} {'maxDD':>8}")
    for cf in analyze_counterfactuals(use, start=start):
        print(
            f"{cf.name:<22} {cf.trades_kept:>5} {cf.pnl:>+10.2f} "
            f"{cf.ending_balance:>10.2f} {cf.max_drawdown:>7.1%}"
        )
        print(f"     {cf.rule}")


if __name__ == "__main__":
    main()
