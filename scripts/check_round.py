"""Quick health/sizing check for the running paper round."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> None:
    meta = json.loads((LOGS / "session_meta.json").read_text(encoding="utf-8"))
    print("session:", meta.get("session_tag"), "| start $", meta.get("starting_balance"))

    trades = _load_jsonl(LOGS / "kalshi_trades.jsonl")
    print(f"closed trades: {len(trades)}")
    risks = []
    for t in trades:
        d = t.get("decision") or {}
        pnl = float(t.get("pnl") or 0)
        eq_before = float(t.get("equity") or t.get("balance") or 0) - pnl
        risk = float(t.get("amount_usdc") or 0) / eq_before if eq_before > 0 else 0.0
        risks.append(risk)
        print(
            f"  {str(t.get('asset')):5} {str(t.get('side')).upper():3} "
            f"entry={float(t.get('entry') or 0):.3f} risk={risk:6.2%} "
            f"pnl={pnl:+8.2f} | snap={len(d):2} fields "
            f"strat={d.get('strategy')} gross@entry={d.get('portfolio_gross_at_entry')} "
            f"conc={d.get('concurrent_open')} lotto={d.get('is_lottery')}"
        )
    if risks:
        print(f"  -> avg risk/trade {sum(risks)/len(risks):.2%}  max {max(risks):.2%}")

    op = json.loads((LOGS / "open_positions.json").read_text(encoding="utf-8"))
    positions = op.get("positions") or []
    sim = json.loads((LOGS / "kalshi_sim.json").read_text(encoding="utf-8"))
    bal = float(sim.get("balance") or 0)
    gross = sum(float(p.get("amount_usdc") or 0) for p in positions)
    # Mirror SimState.gross_open_exposure exactly. Paper divides by the trading
    # balance; live adds open premium back because Kalshi "available" excludes it.
    # Using the live formula in paper understates exposure against the cap.
    live = not bool(sim.get("dry_run", True))
    denom = max(bal + gross, 1.0) if live else max(bal, 1.0)
    basis = "available+open" if live else "trading balance"
    print(f"\nopen positions: {len(positions)}  gross ${gross:.2f}")
    for p in positions:
        print(
            f"  {p['asset']:5} {p['side'].upper():3} entry={p['entry']:.3f} "
            f"${p['amount_usdc']:.2f} ({p['amount_usdc']/denom:.1%})"
        )
    print(f"  -> portfolio gross {gross/denom:.1%} of {basis} (cap 20%, hard stop 28%)")

    print(
        f"\nbalance ${bal:.2f} | trades {sim.get('total_trades')} "
        f"| W/L {sim.get('wins')}/{sim.get('losses')}"
    )
    cal = _load_jsonl(LOGS / "kalshi_calibration.jsonl")
    print(f"calibration rows: {len(cal)}")

    decisions = _load_jsonl(LOGS / "kalshi_decisions.jsonl")
    waits: dict[str, int] = {}
    for d in decisions[-4000:]:
        reason = str(d.get("reason") or "")
        if reason and reason != "OK":
            key = reason.split("(")[0]
            waits[key] = waits.get(key, 0) + 1
    if waits:
        print("\ntop wait reasons:")
        for k, v in sorted(waits.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  {k:34} {v}")


if __name__ == "__main__":
    main()
