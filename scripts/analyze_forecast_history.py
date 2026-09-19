#!/usr/bin/env python3
"""Build an offline, globally deduplicated preferred-forecast history."""
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

from kalshi_bot.research.forecast_history import FORECAST_HISTORY_FIELDS, build_forecast_history, render_report


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
    root, candidate = (ROOT / "reports").resolve(), path.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("forecast-history outputs must be under reports/")
    return candidate


def session_paths(explicit: list[Path], include_live_logs: bool) -> list[Path]:
    paths = explicit or sorted(path for path in (ROOT / "sessions").iterdir() if path.is_dir())
    if include_live_logs:
        paths.append(ROOT / "logs")
    return paths


def write_outputs(rows: list[dict], report: str, out_dir: Path, stem: str) -> tuple[Path, Path, Path]:
    out_dir = report_path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    text_path, jsonl_path, csv_path = (out_dir / f"{stem}.txt", out_dir / f"{stem}.jsonl", out_dir / f"{stem}.csv")
    text_path.write_text(report, encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FORECAST_HISTORY_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return text_path, jsonl_path, csv_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sessions", nargs="*", type=Path, help="optional archived session directories; default is all sessions/")
    parser.add_argument("--include-live-logs", action="store_true", help="also read logs/ (off by default)")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "reports")
    args = parser.parse_args()
    paths = session_paths(args.sessions, args.include_live_logs)
    sessions = [{"name": path.name, "decisions": load_jsonl(path / "kalshi_decisions.jsonl"), "windows": load_jsonl(path / "kalshi_windows.jsonl"), "trades": load_jsonl(path / "kalshi_trades.jsonl")} for path in paths]
    rows, coverage = build_forecast_history(sessions)
    report = render_report(rows, coverage, ", ".join(str(path) for path in paths))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    outputs = write_outputs(rows, report, args.out_dir, f"forecast_history_{stamp}")
    print(report, end="")
    print("Outputs written: " + ", ".join(str(path) for path in outputs))


if __name__ == "__main__":
    main()
