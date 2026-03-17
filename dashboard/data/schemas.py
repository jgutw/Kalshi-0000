"""
dashboard/data/schemas.py — Dataclass schemas for dashboard data.

All fields must be present in mock data. Use Optional[float] where a value may not exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class StateSnapshot:
    ts: str
    asset: str
    window_id: int
    time_remaining_secs: float
    spot_now: Optional[float]
    spot_start: Optional[float]
    synthetic_confidence: float
    dislocation: float
    z_threshold: float
    p_base: Optional[float]
    alpha_micro: float
    p_real: Optional[float]
    p_market: Optional[float]
    mispricing_base: Optional[float]
    confidence_weighted_mispricing: Optional[float]
    lag_confidence: float
    spot_confidence: float
    active_strategy: str
    router_action: str
    wait_reason: str
    open_position_side: Optional[str]
    open_position_entry: Optional[float]
    open_position_contracts: Optional[int]
    unrealized_pnl: Optional[float]
    halt_state: bool
    halt_reason: str
    kalshi_quote_age_secs: float
    kalshi_spread: float


@dataclass
class DecisionEvent:
    ts: str
    asset: str
    window_id: int
    action: str
    reason: str
    p_base: Optional[float]
    p_real: Optional[float]
    p_market: Optional[float]
    ev: Optional[float]
    z_threshold: float
    lag_confidence: float
    spot_confidence: float
    confidence_weighted_mispricing: Optional[float]
    alpha_micro: float
    strategy: str
    diagnostics: dict
    raw_features: dict
    time_remaining_secs: float
    spot_now: Optional[float]
    spot_start: Optional[float]
    kalshi_quote_age_secs: float
    kalshi_spread: float


@dataclass
class TradeEvent:
    ts: str
    asset: str
    ticker: str
    side: str
    entry: float
    exit: float
    contracts: int
    pnl: float
    balance: float
    win_rate: float
    strategy: str
    reason: str
    time_remaining_at_entry: float
    kalshi_spread_at_entry: float
    kalshi_quote_age_at_entry: float
    spot_confidence_at_entry: float
    lag_confidence_at_entry: float
    dislocation_at_entry: float
    confidence_weighted_mispricing_at_entry: Optional[float]


@dataclass
class PortfolioSnapshot:
    ts: str
    balance: float
    starting_balance: float
    peak_balance: float
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    sharpe: float
    var_95: float
    consec_losses: int
    halt_state: bool
    halt_reason: str
    asset_stats: Dict[str, Dict[str, Any]]
