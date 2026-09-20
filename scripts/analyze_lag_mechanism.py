#!/usr/bin/env python3
"""Create an offline August-2026 lag-mechanism development report."""
from __future__ import annotations
import argparse, csv, json, sys
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from kalshi_bot.research.lag_mechanism import LAG_FIELDS, build_lag_dataset, render_report

def load_jsonl(path: Path) -> list[dict]:
    if not path.exists(): return []
    result=[]
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item=json.loads(line)
            if isinstance(item, dict): result.append(item)
        except json.JSONDecodeError: pass
    return result

def report_path(path: Path) -> Path:
    root, candidate=(ROOT/"reports").resolve(), path.resolve()
    if candidate != root and root not in candidate.parents: raise ValueError("outputs must be under reports/")
    return candidate

def write_outputs(rows, report, out_dir, stem):
    out_dir=report_path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    text, jsonl, csv_path=out_dir/f"{stem}.txt", out_dir/f"{stem}.jsonl", out_dir/f"{stem}.csv"
    text.write_text(report, encoding="utf-8")
    with jsonl.open("w", encoding="utf-8") as handle:
        for row in rows: handle.write(json.dumps(row, sort_keys=True, default=str)+"\n")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer=csv.DictWriter(handle, fieldnames=LAG_FIELDS, extrasaction="ignore"); writer.writeheader(); writer.writerows(rows)
    return text, jsonl, csv_path

def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("sessions", nargs="*", type=Path); parser.add_argument("--out-dir", type=Path, default=ROOT/"reports")
    args=parser.parse_args(); paths=args.sessions or sorted(path for path in (ROOT/"sessions").iterdir() if path.is_dir())
    sessions=[{"name": path.name, "decisions": load_jsonl(path/"kalshi_decisions.jsonl"), "windows": load_jsonl(path/"kalshi_windows.jsonl"), "trades": load_jsonl(path/"kalshi_trades.jsonl")} for path in paths]
    rows, coverage=build_lag_dataset(sessions); report=render_report(rows, coverage, ", ".join(str(path) for path in paths)); stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    outputs=write_outputs(rows, report, args.out_dir, f"lag_mechanism_{stamp}"); print(report, end=""); print("Outputs written: "+", ".join(str(path) for path in outputs))
if __name__ == "__main__": main()
