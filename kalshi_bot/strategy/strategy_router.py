"""
strategy_router.py — Routes among strategies with veto logic.
"""

from __future__ import annotations

from typing import List

from .lag_arb import StrategySignal


class StrategyRouter:
    """
    Runs strategies and selects the best signal, with dislocation veto.
    """

    def __init__(self, strategies: list):
        self.strategies = strategies

    def veto_check(
        self,
        candidate: StrategySignal,
        snapshot: dict,
    ) -> StrategySignal:
        """
        If DislocationReversionStrategy fires opposite to candidate,
        return WAIT with dislocation_veto.
        """
        from .dislocation_reversion import DislocationReversionStrategy
        disloc = next((s for s in self.strategies if isinstance(s, DislocationReversionStrategy)), None)
        if disloc is None:
            return candidate
        if candidate.action == "WAIT":
            return candidate

        disloc_sig = disloc.compute_signal(snapshot)
        if disloc_sig.action == "WAIT":
            return candidate

        cand_dir = 1 if candidate.action == "BUY_YES" else -1
        disloc_dir = 1 if disloc_sig.action == "BUY_YES" else -1
        if cand_dir != disloc_dir:
            return StrategySignal(
                "router",
                0.0,
                "WAIT",
                "dislocation_veto",
                {"vetoed_strategy": candidate.strategy, "vetoed_action": candidate.action},
            )
        return candidate

    def route(self, snapshot: dict) -> StrategySignal:
        """
        1. Run all strategies.
        2. Veto check: if DislocationReversion contradicts best candidate, WAIT.
        3. If time_remaining <= 120 and CloseBoundary matches LagArb direction, use CloseBoundary.
        4. Otherwise return highest abs(score) non-WAIT.
        """
        from .close_boundary import CloseBoundaryStrategy
        from .dislocation_reversion import DislocationReversionStrategy
        from .lag_arb import LagArbStrategy

        pairs = [(strat, strat.compute_signal(snapshot)) for strat in self.strategies]
        signals = [sig for _, sig in pairs]
        non_wait = [(strat, sig) for strat, sig in pairs if sig.action != "WAIT"]

        if not non_wait:
            return signals[0] if signals else StrategySignal("router", 0.0, "WAIT", "all_waited", {})

        best_strat, best_sig = max(non_wait, key=lambda p: abs(p[1].score))
        time_remaining = snapshot.get("time_remaining_secs", 999.0)

        if time_remaining <= 120:
            lag_sig = next((sig for s, sig in pairs if isinstance(s, LagArbStrategy)), None)
            close_sig = next((sig for s, sig in pairs if isinstance(s, CloseBoundaryStrategy)), None)
            if lag_sig and close_sig and lag_sig.action != "WAIT" and close_sig.action != "WAIT":
                lag_dir = 1 if lag_sig.action == "BUY_YES" else -1
                close_dir = 1 if close_sig.action == "BUY_YES" else -1
                if lag_dir == close_dir:
                    return self.veto_check(close_sig, snapshot)

        return self.veto_check(best_sig, snapshot)
