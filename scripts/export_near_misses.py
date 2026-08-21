#!/usr/bin/env python3
r"""
export_near_misses.py — Export mispricing_weak near-miss decisions to CSV.

Equivalent to:
  Get-Content logs/kalshi_decisions.jsonl | ConvertFrom-Json |
  Where-Object { $_.reason -like "mispricing_weak" -and $_.confidence_weighted_mispricing -ne $null } |
  Select-Object ts, asset, p_base, p_market, p_real, confidence_weighted_mispricing, lag_confidence |
  Export-Csv logs\near_misses.csv -NoTypeInformation

Run from project root: python scripts/export_near_misses.py

To analyze whether near-misses would have won: compare p_real direction vs p_market.
- p_real > p_market → model favors YES; if contract resolved YES, missed winning trade.
- p_real < p_market → model favors NO; if contract resolved NO, missed winning trade.
Resolution is not in the decisions log (we didn't trade). Infer from trades or Kalshi API.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DECISIONS_PATH = PROJECT_ROOT / "logs" / "kalshi_decisions.jsonl"
OUTPUT_PATH = PROJECT_ROOT / "logs" / "near_misses.csv"


def main() -> None:
    if not DECISIONS_PATH.exists():
        print(f"Decisions file not found: {DECISIONS_PATH}")
        return

    rows = []
    with open(DECISIONS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("reason") != "mispricing_weak":
                continue
            cwm = d.get("confidence_weighted_mispricing")
            if cwm is None:
                continue
            p_real = d.get("p_real")
            p_market = d.get("p_market")
            ev_mag = abs((p_real or 0) - (p_market or 0))
            if ev_mag < 0.01:
                inferred = "NEUTRAL"
            elif (p_real or 0) > (p_market or 0):
                inferred = "BUY_YES"
            else:
                inferred = "BUY_NO"
            rows.append({
                "ts": d.get("ts", ""),
                "asset": d.get("asset", ""),
                "p_base": d.get("p_base"),
                "p_market": p_market,
                "p_real": p_real,
                "confidence_weighted_mispricing": cwm,
                "lag_confidence": d.get("lag_confidence"),
                "inferred_action": inferred,
                "ev_magnitude": round(ev_mag, 4),
            })

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["ts", "asset", "p_base", "p_market", "p_real",
                        "confidence_weighted_mispricing", "lag_confidence",
                        "inferred_action", "ev_magnitude"],
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Exported {len(rows)} near-miss decisions to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
