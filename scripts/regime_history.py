"""
Per-regime round history, computed from archive files rather than the
summarizer (whose profile column disagrees with the session tags).

Regime is parsed from the session tag, which is the only field written at
round start and never rewritten.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSIONS = ROOT / "sessions"

TAG_RE = re.compile(
    r"^(?:archive_|frozen_)?(?:round_)?(?:live_)?(\d+)_(.+?)_(\d+)(?:_\d+)?$", re.I
)


def parse_tag(tag: str) -> tuple[str, str]:
    """Return (round_number, regime) parsed from a session tag."""
    m = TAG_RE.match(tag.strip())
    if not m:
        return ("?", "?")
    return (m.group(1), m.group(2))


def main() -> None:
    rounds: dict[tuple[str, str], dict] = {}

    for d in sorted(SESSIONS.iterdir()):
        if not d.is_dir():
            continue
        meta_p = d / "session_meta.json"
        sim_p = d / "kalshi_sim.json"
        trades_p = d / "kalshi_trades.jsonl"
        if not (meta_p.exists() and sim_p.exists()):
            continue
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            sim = json.loads(sim_p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        tag = str(meta.get("session_tag") or "")
        trades = []
        if trades_p.exists():
            trades = [
                json.loads(x)
                for x in trades_p.read_text(encoding="utf-8").splitlines()
                if x.strip()
            ]
        trades = [t for t in trades if t.get("side")]
        if len(trades) < 3:
            continue

        rnum, regime = parse_tag(tag)
        start = float(sim.get("starting_balance") or 0)
        bal = float(sim.get("balance") or 0)
        vault = float(sim.get("vault_balance") or 0)
        equity = bal + vault
        if start <= 0:
            continue

        wins = sum(1 for t in trades if float(t.get("pnl") or 0) > 0)
        # De-dupe: same round can be archived twice; keep the version with more trades.
        key = (rnum, regime)
        prior = rounds.get(key)
        if prior and prior["n"] >= len(trades):
            continue
        rounds[key] = {
            "tag": tag,
            "round": rnum,
            "regime": regime,
            "start": start,
            "equity": equity,
            "ret": (equity - start) / start,
            "n": len(trades),
            "wr": wins / len(trades),
        }

    def sort_key(r: dict) -> tuple[str, int]:
        rnum = r["round"]
        return (r["regime"], int(rnum) if rnum.isdigit() else 0)

    rows = sorted(rounds.values(), key=sort_key)

    print(f"{'round':>5}  {'regime':24} {'start':>7} {'equity':>9} {'return':>9} {'WR':>5} {'n':>4}")
    print("-" * 74)
    for r in rows:
        print(
            f"{r['round']:>5}  {r['regime'][:24]:24} {r['start']:7.0f} {r['equity']:9.0f} "
            f"{r['ret']:+8.1%} {r['wr']:5.0%} {r['n']:4}"
        )

    print()
    print("=" * 74)
    print("BY REGIME")
    print("=" * 74)
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[r["regime"]].append(r)

    print(f"{'regime':26} {'rounds':>6} {'trades':>7} {'median':>9} {'best':>9} {'worst':>9} {'>0':>5}")
    print("-" * 74)
    summary = []
    for regime, rs in by.items():
        rets = sorted(x["ret"] for x in rs)
        med = rets[len(rets) // 2] if len(rets) % 2 else (rets[len(rets) // 2 - 1] + rets[len(rets) // 2]) / 2
        summary.append((med, regime, rs, rets))
    for med, regime, rs, rets in sorted(summary, reverse=True):
        green = sum(1 for x in rets if x > 0)
        print(
            f"{regime[:26]:26} {len(rs):6} {sum(x['n'] for x in rs):7} "
            f"{med:+8.1%} {max(rets):+8.1%} {min(rets):+8.1%} {green}/{len(rs):>2}"
        )


if __name__ == "__main__":
    main()
