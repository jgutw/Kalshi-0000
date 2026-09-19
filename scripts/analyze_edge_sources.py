#!/usr/bin/env python3
"""Render a descriptive four-layer attribution report from one log/session directory."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kalshi_bot.research.edge_sources import join_closed_trades, render_report


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", default=str(ROOT / "logs"), help="logs/ or one archived session directory")
    parser.add_argument("--out", type=Path, help="report path (default: reports/edge_sources_<UTC>.txt)")
    args = parser.parse_args()
    source = Path(args.source)
    trades = load_jsonl(source / "kalshi_trades.jsonl")
    fills = load_jsonl(source / "kalshi_fills.jsonl")
    rows = join_closed_trades(trades, fills)
    report = render_report(rows, str(source))
    out = args.out or ROOT / "reports" / f"edge_sources_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(report, end="")
    print(f"Report written: {out}")


if __name__ == "__main__":
    main()
