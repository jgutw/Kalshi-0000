"""
Peak-to-trough drawdown helpers and counterfactual loss analysis.

Used both live (halt flags) and offline (session reverse-engineering).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional


@dataclass
class DrawdownState:
    peak: float
    current: float

    @property
    def drawdown_pct(self) -> float:
        if self.peak <= 0:
            return 0.0
        return max(0.0, (self.peak - self.current) / self.peak)

    def update(self, balance: float) -> float:
        if balance > self.peak:
            self.peak = balance
        self.current = balance
        return self.drawdown_pct


def max_drawdown_from_balances(balances: Iterable[float], start: float) -> float:
    """Peak-to-trough max drawdown over an equity path (fraction 0–1)."""
    peak = float(start)
    max_dd = 0.0
    for b in balances:
        bal = float(b)
        if bal > peak:
            peak = bal
        if peak > 0:
            max_dd = max(max_dd, (peak - bal) / peak)
    return max_dd


def equity_path(trades: list[dict], start: float) -> list[float]:
    """Prefer logged balance; else walk start + cumulative pnl."""
    out: list[float] = []
    eq = float(start)
    for t in trades:
        if t.get("balance") is not None:
            eq = float(t["balance"])
        else:
            eq += float(t.get("pnl") or 0.0)
        out.append(eq)
    return out


@dataclass
class Counterfactual:
    name: str
    trades_kept: int
    pnl: float
    ending_balance: float
    max_drawdown: float
    rule: str


def _kept(trade: dict, predicate) -> bool:
    return bool(predicate(trade))


def analyze_counterfactuals(
    trades: list[dict],
    start: float = 1000.0,
) -> list[Counterfactual]:
    """
    Reverse-engineer simple skip rules: recompute PnL/DD if we had avoided
    certain loss-prone setups. Pure accounting — not causal proof of edge.
    """

    def run(name: str, rule: str, predicate) -> Counterfactual:
        kept = [t for t in trades if _kept(t, predicate)]
        pnl = sum(float(t.get("pnl") or 0.0) for t in kept)
        # Rebuild path from filtered pnls for fair DD (ignore original balances)
        eq = float(start)
        bals = []
        for t in kept:
            eq += float(t.get("pnl") or 0.0)
            bals.append(eq)
        dd = max_drawdown_from_balances(bals, start) if bals else 0.0
        return Counterfactual(
            name=name,
            trades_kept=len(kept),
            pnl=round(pnl, 2),
            ending_balance=round(start + pnl, 2),
            max_drawdown=round(dd, 4),
            rule=rule,
        )

    def near_miss_bps(t: dict) -> Optional[float]:
        ptb, spot = t.get("price_to_beat"), t.get("exit_spot")
        if ptb is None or spot is None:
            return None
        ptb_f = float(ptb)
        if ptb_f == 0:
            return None
        return abs(float(spot) - ptb_f) / ptb_f * 10_000

    results = [
        run("baseline", "all closed trades", lambda t: True),
        run(
            "skip_entry_lt_0.15",
            "skip contracts with entry < 0.15 (lottery tickets)",
            lambda t: float(t.get("entry") or 0) >= 0.15,
        ),
        run(
            "skip_entry_lt_0.20",
            "skip entry < 0.20",
            lambda t: float(t.get("entry") or 0) >= 0.20,
        ),
        run(
            "skip_entry_gt_0.80",
            "skip entry > 0.80 (expensive favorites)",
            lambda t: float(t.get("entry") or 1) <= 0.80,
        ),
        run(
            "cap_notional_5pct",
            "keep only trades with entry*contracts <= 5% of prior equity (approx)",
            lambda t: True,  # replaced below
        ),
    ]

    # Rebuild cap_notional with path-aware filter
    eq = float(start)
    kept_cap = []
    for t in trades:
        notional = float(t.get("entry") or 0) * float(t.get("contracts") or 0)
        if eq > 0 and notional <= 0.05 * eq:
            kept_cap.append(t)
            eq += float(t.get("pnl") or 0.0)
        # skipped trades do not update "would-have" equity for sizing gate;
        # use actual walk only when kept — conservative alternate: still update
        # from kept only (already done).
    pnl_cap = sum(float(t.get("pnl") or 0.0) for t in kept_cap)
    bals_cap = []
    eq = float(start)
    for t in kept_cap:
        eq += float(t.get("pnl") or 0.0)
        bals_cap.append(eq)
    results[4] = Counterfactual(
        name="cap_notional_5pct",
        trades_kept=len(kept_cap),
        pnl=round(pnl_cap, 2),
        ending_balance=round(start + pnl_cap, 2),
        max_drawdown=round(max_drawdown_from_balances(bals_cap, start) if bals_cap else 0.0, 4),
        rule="skip if entry×contracts > 5% of then-equity",
    )

    # Near-miss skip: drop trades that settled within N bps (result sensitive)
    for bps_lim, label in ((2.0, "skip_near_miss_2bps"), (5.0, "skip_near_miss_5bps")):
        results.append(
            run(
                label,
                f"skip closes with |spot-ptb|/ptb < {bps_lim} bps",
                lambda t, lim=bps_lim: (near_miss_bps(t) is None) or (near_miss_bps(t) >= lim),
            )
        )

    # Half-size: scale all pnl by 0.5 (proxy for half Kelly)
    half_pnl = 0.5 * sum(float(t.get("pnl") or 0.0) for t in trades)
    bals_half = []
    eq = float(start)
    for t in trades:
        eq += 0.5 * float(t.get("pnl") or 0.0)
        bals_half.append(eq)
    results.append(
        Counterfactual(
            name="half_size",
            trades_kept=len(trades),
            pnl=round(half_pnl, 2),
            ending_balance=round(start + half_pnl, 2),
            max_drawdown=round(max_drawdown_from_balances(bals_half, start) if bals_half else 0.0, 4),
            rule="scale every trade PnL by 0.5 (proxy for half position size)",
        )
    )

    return results


def loss_buckets(trades: list[dict]) -> dict[str, Any]:
    """Group losses for reverse-engineering (entry band, asset, near-miss)."""
    losses = [t for t in trades if float(t.get("pnl") or 0) <= 0]
    by_band: dict[str, dict[str, float]] = {}
    for t in losses:
        e = float(t.get("entry") or 0)
        if e < 0.20:
            band = "0.00-0.20"
        elif e < 0.40:
            band = "0.20-0.40"
        elif e < 0.60:
            band = "0.40-0.60"
        elif e < 0.80:
            band = "0.60-0.80"
        else:
            band = "0.80-1.00"
        slot = by_band.setdefault(band, {"n": 0, "pnl": 0.0})
        slot["n"] += 1
        slot["pnl"] += float(t["pnl"])

    by_asset: dict[str, dict[str, float]] = {}
    for t in losses:
        a = str(t.get("asset") or "?")
        slot = by_asset.setdefault(a, {"n": 0, "pnl": 0.0})
        slot["n"] += 1
        slot["pnl"] += float(t["pnl"])

    return {
        "loss_count": len(losses),
        "loss_pnl": round(sum(float(t["pnl"]) for t in losses), 2),
        "by_entry_band": {k: {"n": int(v["n"]), "pnl": round(v["pnl"], 2)} for k, v in sorted(by_band.items())},
        "by_asset": {k: {"n": int(v["n"]), "pnl": round(v["pnl"], 2)} for k, v in sorted(by_asset.items())},
    }
