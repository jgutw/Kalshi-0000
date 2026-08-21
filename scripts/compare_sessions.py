#!/usr/bin/env python3
"""Compare archived paper-trading sessions side by side."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_trades(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def summarize(name: str, folder: Path) -> dict:
    sim_path = folder / "kalshi_sim.json"
    trades_path = folder / "kalshi_trades.jsonl"
    dec_path = folder / "kalshi_decisions.jsonl"

    sim = json.loads(sim_path.read_text(encoding="utf-8")) if sim_path.exists() else {}
    trades = load_trades(trades_path)
    real = [t for t in trades if t.get("side")]
    test = [t for t in trades if not t.get("side")]

    start = float(sim.get("starting_balance", 1000))
    balance = float(sim.get("balance", start))
    wins = sum(1 for t in real if float(t.get("pnl", 0)) > 0)
    losses = len(real) - wins

    by_asset: dict[str, dict] = {}
    for t in real:
        a = t.get("asset", "?")
        by_asset.setdefault(a, {"n": 0, "pnl": 0.0, "w": 0})
        by_asset[a]["n"] += 1
        by_asset[a]["pnl"] += float(t.get("pnl", 0))
        if float(t.get("pnl", 0)) > 0:
            by_asset[a]["w"] += 1

    reasons: Counter = Counter()
    if dec_path.exists():
        lines = dec_path.read_text(encoding="utf-8").splitlines()
        for line in lines[-100_000:]:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("action") == "WAIT":
                r = str(d.get("reason", "?")).split("(")[0]
                reasons[r] += 1

    ts_list = [t.get("ts", "") for t in real if t.get("ts")]
    span = f"{min(ts_list)[:16]} to {max(ts_list)[:16]}" if ts_list else "n/a"

    return {
        "name": name,
        "folder": str(folder),
        "start": start,
        "balance": balance,
        "pnl": balance - start,
        "trades": len(real),
        "wins": wins,
        "losses": losses,
        "wr": wins / len(real) if real else 0.0,
        "test_rows": len(test),
        "span": span,
        "by_asset": by_asset,
        "top_waits": reasons.most_common(8),
    }


def print_summary(s: dict) -> None:
    print(f"=== {s['name']} ===")
    print(f"  Folder:   {s['folder']}")
    print(f"  Balance:  ${s['balance']:,.2f}  (start ${s['start']:,.2f})  P&L ${s['pnl']:+,.2f}")
    print(f"  Trades:   {s['trades']}  ({s['wins']}W / {s['losses']}L)  WR={s['wr']:.1%}")
    if s["test_rows"]:
        print(f"  Warning:  {s['test_rows']} test-engine rows (no side/exit_spot) — exclude from comparisons")
    print(f"  Span:     {s['span']}")
    print("  By asset:")
    for a, v in sorted(s["by_asset"].items()):
        wr = v["w"] / v["n"] if v["n"] else 0
        print(f"    {a:4}  {v['n']:3} trades  WR={wr:5.1%}  PnL=${v['pnl']:+8.2f}")
    if s["top_waits"]:
        waits = ", ".join(f"{k}={n}" for k, n in s["top_waits"][:5])
        print(f"  Top WAIT: {waits}")
    print()


def main() -> None:
    folders: list[tuple[str, Path]] = []

    sessions_dir = ROOT / "sessions"
    if sessions_dir.exists():
        for d in sorted(sessions_dir.iterdir()):
            if d.is_dir() and (d / "kalshi_sim.json").exists():
                folders.append((d.name, d))

    current = ROOT / "logs"
    if (current / "kalshi_sim.json").exists():
        folders.append(("CURRENT (logs/)", current))

    if len(folders) < 1:
        print("No sessions found. Archive a round first:")
        print("  copy logs/* to sessions/session_YYYY-MM-DD_HHMM/")
        sys.exit(1)

    summaries = [summarize(name, path) for name, path in folders]
    for s in summaries:
        print_summary(s)

    if len(summaries) >= 2:
        a, b = summaries[-2], summaries[-1]
        print("=" * 60)
        print(f"DELTA: {b['name']} vs {a['name']}")
        print(f"  P&L:      ${b['pnl']:+,.2f} vs ${a['pnl']:+,.2f}  ({b['pnl']-a['pnl']:+,.2f})")
        print(f"  Trades:   {b['trades']} vs {a['trades']}")
        print(f"  Win rate: {b['wr']:.1%} vs {a['wr']:.1%}")
        print(f"  Return:   {b['pnl']/b['start']:+.1%} vs {a['pnl']/a['start']:+.1%}")


if __name__ == "__main__":
    main()
