"""
Loss ledger — where the loss dollars actually came from.

Everything here is **risk-normalized**: each trade is converted to
return-on-equity-at-entry (pnl / equity_before). Raw dollar totals are
misleading across rounds because bankrolls differ by 10x, so a single large
round dominates every dollar-weighted statistic.

Read-only. Used by Telegram /losses and offline review.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TRADES_PATH = PROJECT_ROOT / "logs" / "kalshi_trades.jsonl"

ENTRY_BUCKETS: tuple[tuple[float, float, str], ...] = (
    (0.00, 0.15, "<0.15"),
    (0.15, 0.20, "0.15-0.20"),
    (0.20, 0.40, "0.20-0.40"),
    (0.40, 0.60, "0.40-0.60"),
    (0.60, 1.01, ">0.60"),
)


def _f(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def load_trades(path: Optional[Path] = None) -> list[dict]:
    p = Path(path or TRADES_PATH)
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def annotate(trades: Iterable[dict]) -> list[dict]:
    """Attach equity_before, return-on-equity and size-as-%-of-equity."""
    rows = []
    for t in trades:
        pnl = _f(t.get("pnl"))
        eq_after = _f(t.get("equity")) or _f(t.get("balance"))
        eq_before = eq_after - pnl
        row = dict(t)
        row["_eq_before"] = eq_before
        row["_r"] = pnl / eq_before if eq_before > 0 else 0.0
        row["_risk"] = _f(t.get("amount_usdc")) / eq_before if eq_before > 0 else 0.0
        rows.append(row)
    return rows


def _agg(rows: Iterable[dict]) -> dict:
    rows = list(rows)
    wins = [r for r in rows if _f(r.get("pnl")) > 0]
    return {
        "n": len(rows),
        "w": len(wins),
        "l": len(rows) - len(wins),
        "wr": len(wins) / len(rows) if rows else 0.0,
        "sum_r": sum(r["_r"] for r in rows),
        "pnl": sum(_f(r.get("pnl")) for r in rows),
    }


def build_ledger(trades: Optional[list[dict]] = None) -> dict:
    rows = annotate(trades if trades is not None else load_trades())
    if not rows:
        return {}

    by_asset: dict[str, list[dict]] = defaultdict(list)
    by_side: dict[str, list[dict]] = defaultdict(list)
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    by_strategy: dict[str, list[dict]] = defaultdict(list)
    by_window: dict[str, list[dict]] = defaultdict(list)

    for r in rows:
        by_asset[str(r.get("asset") or "?")].append(r)
        by_side[str(r.get("side") or "?").lower()].append(r)
        entry = _f(r.get("entry"))
        for lo, hi, label in ENTRY_BUCKETS:
            if lo <= entry < hi:
                by_bucket[label].append(r)
                break
        decision = r.get("decision") or {}
        by_strategy[str(decision.get("strategy") or r.get("strategy") or "?")].append(r)
        by_window[str(r.get("window_id") or "?")].append(r)

    # Crowding: how did windows do as a function of how many positions were open
    by_concurrency: dict[int, list[dict]] = defaultdict(list)
    for _wid, ws in by_window.items():
        key = min(len(ws), 5)
        by_concurrency[key].extend(ws)

    return {
        "totals": _agg(rows),
        "by_asset": {k: _agg(v) for k, v in by_asset.items()},
        "by_side": {k: _agg(v) for k, v in by_side.items()},
        "by_bucket": {k: _agg(v) for k, v in by_bucket.items()},
        "by_strategy": {k: _agg(v) for k, v in by_strategy.items()},
        "by_concurrency": {k: _agg(v) for k, v in by_concurrency.items()},
        "avg_risk_pct": sum(r["_risk"] for r in rows) / len(rows),
        "max_risk_pct": max(r["_risk"] for r in rows),
        "worst": sorted(rows, key=lambda r: _f(r.get("pnl")))[:5],
    }


def format_loss_ledger(trades: Optional[list[dict]] = None) -> str:
    led = build_ledger(trades)
    if not led:
        return "Loss ledger: no trades recorded yet."

    tot = led["totals"]
    lines = [
        "Loss ledger (risk-normalized)",
        f"{tot['n']} trades  {tot['w']}W/{tot['l']}L  WR {tot['wr']:.0%}  "
        f"net {tot['sum_r']:+.1%} of equity",
        f"avg size {led['avg_risk_pct']:.1%} of equity  (max {led['max_risk_pct']:.1%})",
        "",
        "By side",
    ]
    for side in ("yes", "no"):
        a = led["by_side"].get(side)
        if a:
            lines.append(f"  {side.upper():3} n={a['n']:3} WR={a['wr']:.0%} net={a['sum_r']:+.1%}")

    lines.append("")
    lines.append("By entry price")
    for _lo, _hi, label in ENTRY_BUCKETS:
        a = led["by_bucket"].get(label)
        if a:
            lines.append(f"  {label:10} n={a['n']:3} WR={a['wr']:.0%} net={a['sum_r']:+.1%}")

    lines.append("")
    lines.append("Worst assets")
    worst_assets = sorted(led["by_asset"].items(), key=lambda kv: kv[1]["sum_r"])[:4]
    for name, a in worst_assets:
        lines.append(f"  {name:5} n={a['n']:3} WR={a['wr']:.0%} net={a['sum_r']:+.1%}")

    conc = led["by_concurrency"]
    if conc:
        lines.append("")
        lines.append("By positions open in window")
        for k in sorted(conc):
            a = conc[k]
            label = f"{k}+" if k >= 5 else str(k)
            lines.append(f"  {label:3} n={a['n']:3} WR={a['wr']:.0%} net={a['sum_r']:+.1%}")

    if led["worst"]:
        lines.append("")
        lines.append("Biggest single losses")
        for r in led["worst"]:
            if _f(r.get("pnl")) >= 0:
                continue
            lines.append(
                f"  {str(r.get('asset')):5} {str(r.get('side') or '').upper():3} "
                f"entry={_f(r.get('entry')):.3f} "
                f"risk={r['_risk']:.1%} pnl={_f(r.get('pnl')):+.2f}"
            )
    return "\n".join(lines)
