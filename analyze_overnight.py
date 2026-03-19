#!/usr/bin/env python3
"""
analyze_overnight.py — Diagnose overnight bot performance and reconcile log files.

Run from project root: python analyze_overnight.py

Outputs:
  - WAIT reason breakdown (why no trades fired)
  - Data reconciliation (trades vs decisions vs events)
  - Actionable summary
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
LOGS = PROJECT_ROOT / "logs"
TRADES_PATH = LOGS / "kalshi_trades.jsonl"
DECISIONS_PATH = LOGS / "kalshi_decisions.jsonl"
EVENTS_PATH = LOGS / "kalshi_events.jsonl"
FEATURES_PATH = LOGS / "kalshi_features.jsonl"
SIM_PATH = LOGS / "kalshi_sim.json"


def parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def main() -> None:
    print("=" * 70)
    print("  OVERNIGHT BOT DIAGNOSTIC — Last 10 Hours")
    print("=" * 70)

    # ─── 1. Sim state ─────────────────────────────────────────────────────
    sim = {}
    if SIM_PATH.exists():
        try:
            with open(SIM_PATH, encoding="utf-8") as f:
                sim = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    total_trades = sim.get("total_trades", 0)
    balance = sim.get("balance", 1000.0)
    print(f"\n[Sim State] balance=${balance:.2f}  total_trades={total_trades}")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=10)

    # ─── 2. Trades ────────────────────────────────────────────────────────
    trades = load_jsonl(TRADES_PATH)
    trades_in_window = [t for t in trades if parse_ts(t.get("ts")) and parse_ts(t.get("ts")) >= cutoff]
    print(f"[Trades]    {len(trades)} total in kalshi_trades.jsonl | {len(trades_in_window)} in last 10h")

    if trades:
        first = parse_ts(trades[0].get("ts"))
        last = parse_ts(trades[-1].get("ts"))
        print(f"            First: {first} | Last: {last}")
    else:
        print("            No trades placed.")

    # ─── 3. Decisions (last 10 hours) ──────────────────────────────────────
    decisions = load_jsonl(DECISIONS_PATH)
    recent = [d for d in decisions if parse_ts(d.get("ts")) and parse_ts(d.get("ts")) >= cutoff]
    print(f"\n[Decisions] {len(decisions)} total | {len(recent)} in last 10h")

    if not recent:
        print("            No decisions in last 10 hours — bot may have crashed or been idle.")
    else:
        first_d = parse_ts(recent[0].get("ts"))
        last_d = parse_ts(recent[-1].get("ts"))
        print(f"            First: {first_d} | Last: {last_d}")

    # ─── 4. WAIT reason breakdown ──────────────────────────────────────────
    reasons = Counter(d.get("reason", "?") for d in recent)
    by_asset = defaultdict(lambda: Counter())
    for d in recent:
        asset = d.get("asset", "?")
        by_asset[asset][d.get("reason", "?")] += 1

    print("\n" + "-" * 70)
    print("  WAIT REASON BREAKDOWN (last 10h)")
    print("-" * 70)
    for reason, count in reasons.most_common(20):
        pct = 100 * count / len(recent) if recent else 0
        print(f"  {reason:<45} {count:>6}  ({pct:.1f}%)")

    print("\n  By asset:")
    for asset in ["BTC", "ETH", "SOL", "XRP"]:
        c = by_asset.get(asset, Counter())
        top = c.most_common(5)
        if top:
            parts = ", ".join(f"{r}={n}" for r, n in top)
            print(f"    {asset}: {parts}")

    # ─── 5. Near-miss analysis ─────────────────────────────────────────────
    near_trades = [d for d in recent if d.get("reason") in (
        "mispricing_weak", "lag_confidence_low", "edge(", "conviction("
    )]
    if near_trades:
        print("\n" + "-" * 70)
        print(f"  NEAR-MISSES ({len(near_trades)} decisions that were close to trading)")
        print("-" * 70)
        for d in near_trades[:15]:
            ts = d.get("ts", "")[:19]
            asset = d.get("asset", "?")
            reason = d.get("reason", "?")
            cwm = d.get("confidence_weighted_mispricing")
            lag = d.get("lag_confidence")
            p_base = d.get("p_base")
            ev = d.get("ev")
            print(f"    {ts} [{asset}] {reason}  cwm={cwm} lag={lag} p_base={p_base} ev={ev}")

    # ─── 6. Events / features ──────────────────────────────────────────────
    events = load_jsonl(EVENTS_PATH)
    features = load_jsonl(FEATURES_PATH)
    events_10h = [e for e in events if parse_ts(e.get("ts")) and parse_ts(e.get("ts")) >= cutoff] if events else []
    features_10h = [f for f in features if parse_ts(f.get("ts")) and parse_ts(f.get("ts")) >= cutoff] if features else []
    print(f"\n[Events]    {len(events)} total | ~{len(events_10h)} in last 10h (if ts present)")
    print(f"[Features]  {len(features)} total | ~{len(features_10h)} in last 10h (if ts present)")

    # ─── 7. Reconciliation ────────────────────────────────────────────────
    print("\n" + "-" * 70)
    print("  RECONCILIATION")
    print("-" * 70)
    print(f"  kalshi_trades.jsonl:   {len(trades)} closed trades")
    print(f"  kalshi_sim.json:       total_trades={total_trades}")
    if len(trades) != total_trades:
        print(f"  [!!] Mismatch: trades file has {len(trades)} lines but sim reports {total_trades}")
    else:
        print("  ✓ Trades count consistent")

    if recent and len(trades) == 0:
        print("\n  → Bot was running (decisions logged) but no trades met all entry thresholds.")
        print("  → This is expected when: markets are decided (p_base < 0.05 or > 0.95),")
        print("    lag_confidence stays low, or CWM/mispricing is below 0.03.")

    # ─── 8. Summary ───────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    if not recent:
        print("  Bot was likely not running or crashed. No decisions in last 10h.")
    else:
        trades_last_10h = len(trades_in_window)
        if trades_last_10h == 0:
            top3 = [r for r, _ in reasons.most_common(3)]
            print(f"  LAST 10 HOURS: 0 trades placed.")
            print(f"  Top WAIT reasons: {', '.join(top3)}")
            print("  The filters are working as designed — no edge met all gates.")
            print("  Historical: %d trades total (sim), %d in file." % (total_trades, len(trades)))
        else:
            print(f"  Last 10h: {trades_last_10h} trades. Total: {total_trades}.")
            print("  Check kalshi_report.xlsx for P&L.")
    print()


if __name__ == "__main__":
    main()
