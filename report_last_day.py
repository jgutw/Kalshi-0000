#!/usr/bin/env python3
"""One-off report: bot activity in the last 24 hours."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

LOGS = Path(__file__).resolve().parent / "logs"
DECISIONS = LOGS / "kalshi_decisions.jsonl"
TRADES = LOGS / "kalshi_trades.jsonl"
SIM = LOGS / "kalshi_sim.json"


def parse_ts(s):
    if not s:
        return None
    try:
        s = str(s).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def main() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    sim = {}
    if SIM.exists():
        with open(SIM, encoding="utf-8") as f:
            sim = json.load(f)

    decisions = []
    if DECISIONS.exists():
        with open(DECISIONS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    decisions.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    recent = [d for d in decisions if parse_ts(d.get("ts")) and parse_ts(d.get("ts")) >= cutoff]
    reasons = Counter(d.get("reason", "?") for d in recent)
    by_asset = defaultdict(lambda: Counter())
    for d in recent:
        by_asset[d.get("asset", "?")][d.get("reason", "?")] += 1

    trades = []
    if TRADES.exists():
        with open(TRADES, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    trades.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    trades_24 = [t for t in trades if parse_ts(t.get("ts")) and parse_ts(t.get("ts")) >= cutoff]

    print("=" * 72)
    print("  KALSHI BOT — LAST 24 HOURS")
    print("=" * 72)
    print()
    print("SIM STATE (current kalshi_sim.json)")
    print(f"  Balance:    ${sim.get('balance', 0):,.2f}  (start ${sim.get('starting_balance', 1000):,.2f})")
    print(f"  All-time:   total_trades={sim.get('total_trades', 0)}  W={sim.get('wins', 0)}  L={sim.get('losses', 0)}")
    halted = sim.get("_halted_at")
    if halted:
        print(f"  Halted at:  {halted}")
    print()
    print(f"DECISION TICKS (last 24h): {len(recent):,}")
    if recent:
        ts_list = [parse_ts(d.get("ts")) for d in recent if parse_ts(d.get("ts"))]
        if ts_list:
            print(f"  Span:       {min(ts_list)}  to  {max(ts_list)}")
    print()
    print("TOP WAIT / REASON COUNTS (last 24h)")
    for r, n in reasons.most_common(20):
        pct = 100 * n / len(recent) if recent else 0
        rshort = (r[:56] + "…") if len(r) > 57 else r
        print(f"  {rshort:<58} {n:>7}  ({pct:5.1f}%)")
    print()
    print("BY ASSET — top reasons")
    for a in ["BTC", "ETH", "SOL", "XRP"]:
        c = by_asset.get(a, Counter())
        if not c:
            continue
        top = ", ".join(f"{k[:18]}={v}" for k, v in c.most_common(5))
        print(f"  {a}: {top}")
    print()
    print(f"TRADES CLOSED (last 24h): {len(trades_24)}")
    if trades_24:
        pnl = sum(float(t.get("pnl", 0)) for t in trades_24)
        print(f"  Sum PnL (24h): ${pnl:+,.2f}")
        print("  Recent:")
        for t in trades_24[-15:]:
            ts = parse_ts(t.get("ts"))
            print(f"    {ts}  {t.get('asset')}  pnl=${float(t.get('pnl', 0)):+.2f}")
    else:
        print("  No trades with timestamps in the last 24h (bot may have been WAIT-only or idle).")
    print()
    print("=" * 72)


if __name__ == "__main__":
    main()
