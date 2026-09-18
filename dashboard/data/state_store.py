"""
dashboard/data/state_store.py — Singleton StateStore backed by st.session_state.

Uses FileWatcher to avoid re-parsing when files unchanged.
Falls back to mock data transparently. Exposes is_mock_mode().
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import streamlit as st

from .file_watcher import FileWatcher
from .loaders import (
    load_decisions,
    load_latest_snapshots,
    load_portfolio,
    load_trades,
    load_window_performance,
)
from .schemas import DecisionEvent, PortfolioSnapshot, StateSnapshot, TradeEvent

ROOT = Path(__file__).resolve().parent.parent.parent
LOGS_DIR = ROOT / "logs"
PATHS = [
    str(LOGS_DIR / "kalshi_sim.json"),
    str(LOGS_DIR / "kalshi_trades.jsonl"),
    str(LOGS_DIR / "kalshi_decisions.jsonl"),
    str(LOGS_DIR / "kalshi_fills.jsonl"),
    str(LOGS_DIR / "open_positions.json"),
]


def _files_exist_and_nonempty() -> bool:
    """Live mode: kalshi_sim.json and kalshi_decisions.jsonl must exist and be non-empty.
    kalshi_trades.jsonl may be missing or empty (no trades yet in a fresh round)."""
    sim = LOGS_DIR / "kalshi_sim.json"
    decisions = LOGS_DIR / "kalshi_decisions.jsonl"
    if not sim.exists() or sim.stat().st_size == 0:
        return False
    if not decisions.exists() or decisions.stat().st_size == 0:
        return False
    return True


@st.cache_data(ttl=5)
def _cached_portfolio() -> PortfolioSnapshot:
    return load_portfolio()


@st.cache_data(ttl=5)
def _cached_trades() -> List[TradeEvent]:
    return load_trades()


@st.cache_data(ttl=5)
def _cached_decisions(asset: Optional[str], last_n: int) -> List[DecisionEvent]:
    return load_decisions(asset=asset, last_n=last_n)


@st.cache_data(ttl=5)
def _cached_snapshots() -> Dict[str, StateSnapshot]:
    return load_latest_snapshots()


@st.cache_data(ttl=5)
def _cached_window_performance():
    return load_window_performance()


class StateStore:
    """Singleton state store for dashboard data."""

    _instance: Optional["StateStore"] = None
    _watcher = FileWatcher()

    def __new__(cls) -> "StateStore":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def is_mock_mode(self) -> bool:
        """True when no live logs detected (files missing or empty)."""
        return not _files_exist_and_nonempty()

    def get_portfolio(self) -> PortfolioSnapshot:
        return _cached_portfolio()

    def get_decisions(
        self,
        asset: Optional[str] = None,
        last_n: int = 500,
    ) -> List[DecisionEvent]:
        return _cached_decisions(asset, last_n)

    def get_trades(self, asset: Optional[str] = None) -> List[TradeEvent]:
        trades = _cached_trades()
        if asset:
            return [t for t in trades if t.asset == asset]
        return trades

    def get_latest_snapshots(self) -> Dict[str, StateSnapshot]:
        return _cached_snapshots()

    def get_window_performance(self):
        return _cached_window_performance()
