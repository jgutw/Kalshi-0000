#!/usr/bin/env python3
"""Create derived one-row-per-asset-window model opportunity artifacts offline."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kalshi_bot.research.opportunities import OPPORTUNITY_FIELDS, build_opportunities, render_report


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


def report_path(path: Path) -> Path:
    root = (ROOT / "reports").resolve()
    candidate = path.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("derived opportunity outputs must be under reports/")
    return candidate


def write_outputs(rows: list[dict], report: str, out_dir: Path, stem: str) -> tuple[Path, Path, Path]:
    out_dir = report_path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_file = out_dir / f"{stem}.txt"
    jsonl_file = out_dir / f"{stem}.jsonl"
    csv_file = out_dir / f"{stem}.csv"
    report_file.write_text(report, encoding="utf-8")
    with jsonl_file.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    with csv_file.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=OPPORTUNITY_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value for key, value in row.items()})
    return report_file, jsonl_file, csv_file


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="operator-supplied logs/ or sessions/<id>/ directory")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "reports", help="must be under reports/")
    args = parser.parse_args()
    source = Path(args.source)
    rows, coverage = build_opportunities(
        load_jsonl(source / "kalshi_decisions.jsonl"),
        load_jsonl(source / "kalshi_windows.jsonl"),
        load_jsonl(source / "kalshi_trades.jsonl"),
    )
    report = render_report(rows, coverage, str(source))
    stem = f"decision_opportunities_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    paths = write_outputs(rows, report, args.out_dir, stem)
    print(report, end="")
    print("Outputs written: " + ", ".join(str(path) for path in paths))


if __name__ == "__main__":
    main()
