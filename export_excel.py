#!/usr/bin/env python3
"""
export_excel.py — Generate professional Excel trading analytics workbook for Kalshi bot.
Run: python export_excel.py
      python export_excel.py --watch   (regenerate every 60s)
Output: kalshi_report.xlsx
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# Paths
PROJECT_ROOT = Path(__file__).resolve().parent
TRADES_PATH = PROJECT_ROOT / "logs" / "kalshi_trades.jsonl"
SIM_PATH = PROJECT_ROOT / "logs" / "kalshi_sim.json"
DECISIONS_PATH = PROJECT_ROOT / "logs" / "kalshi_decisions.jsonl"
OUTPUT_PATH = PROJECT_ROOT / "kalshi_report.xlsx"

# Styles
HEADER_FILL = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
SUBHEADER_FILL = PatternFill(start_color="2E75B6", end_color="2E75B6", fill_type="solid")
TOTALS_FILL = PatternFill(start_color="DEEAF1", end_color="DEEAF1", fill_type="solid")
ALT_ROW_FILL = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")
LIGHT_GREEN = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
LIGHT_RED = PatternFill(start_color="FFDCD4", end_color="FFDCD4", fill_type="solid")
WARNING_FILL = PatternFill(start_color="FFFF00", end_color="FFFF00", fill_type="solid")
THIN_BORDER = Border(
    left=Side(style="thin"),
    right=Side(style="thin"),
    top=Side(style="thin"),
    bottom=Side(style="thin"),
)
THICK_BORDER = Border(
    left=Side(style="medium"),
    right=Side(style="medium"),
    top=Side(style="medium"),
    bottom=Side(style="medium"),
)

# ET offset (UTC-5 or UTC-4 for DST; we use UTC-5 for simplicity)
ET_OFFSET = -5 * 3600


def load_trades() -> list[dict]:
    """Load trades from JSONL. Return [] if missing."""
    if not TRADES_PATH.exists():
        return []
    trades = []
    with open(TRADES_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                trades.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return trades


def load_sim() -> Optional[dict]:
    """Load sim state. Return None if missing."""
    if not SIM_PATH.exists():
        return None
    try:
        with open(SIM_PATH, encoding="utf-8-sig") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def load_decisions(limit: int = 10_000) -> list[dict]:
    """Load last N decisions from JSONL. Return [] if missing."""
    if not DECISIONS_PATH.exists():
        return []
    lines = []
    with open(DECISIONS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                lines.append(line)
    lines = lines[-limit:]
    decisions = []
    for line in lines:
        try:
            decisions.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return decisions


def parse_ts(ts_str: str) -> Optional[datetime]:
    """Parse ISO timestamp to datetime."""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt
    except (ValueError, TypeError):
        return None


def compute_time_to_expiry(trade: dict) -> Optional[float]:
    """Estimate seconds to expiry from window_id and ts. Return None if unavailable."""
    ts = parse_ts(trade.get("ts", ""))
    wid = trade.get("window_id", "")
    if not ts or not wid:
        return None
    try:
        # window_id format: "2026-03-18 20:15" (window end time, likely UTC)
        parts = str(wid).split()
        if len(parts) >= 2:
            date_part, time_part = parts[0], parts[1]
            end_str = f"{date_part}T{time_part}:00"
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            if ts.tzinfo:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            delta = end_dt - ts
            return max(0, min(900, delta.total_seconds()))
    except (ValueError, TypeError):
        pass
    return None


def result_correct(trade: dict) -> str:
    """YES if predicted direction matched actual outcome."""
    side = (trade.get("side") or "yes").lower()
    exit_ = trade.get("exit", 0)
    ptb = trade.get("price_to_beat")
    spot = trade.get("exit_spot")
    if ptb is None or spot is None:
        return "N/A"
    won = exit_ >= 0.5
    if side == "yes":
        actual_yes = spot > ptb
        correct = won == actual_yes
    else:
        actual_no = spot < ptb
        correct = won == actual_no
    return "YES" if correct else "NO"


# --- Sheet 1: Executive Summary ---
def write_summary_sheet(wb: Workbook, trades: list, sim: Optional[dict], warnings: list[str]) -> None:
    ws = wb.create_sheet("Summary", 0)
    ws.sheet_properties.tabColor = "00B050"

    row = 1
    if warnings:
        ws.cell(row, 1, "WARNING: Missing or incomplete data")
        ws.cell(row, 1).fill = WARNING_FILL
        ws.cell(row, 1).font = Font(bold=True)
        for w in warnings:
            row += 1
            ws.cell(row, 1, f"  • {w}")
            ws.cell(row, 1).fill = WARNING_FILL
        row += 2

    balance = (sim or {}).get("balance", 1000.0)
    start_bal = (sim or {}).get("starting_balance", 1000.0)
    peak = (sim or {}).get("peak_balance", balance)
    total_pnl = balance - start_bal
    pnl_pct = (total_pnl / start_bal * 100) if start_bal else 0

    # Max drawdown from trades
    cum = start_bal
    peak_so_far = start_bal
    max_dd_pct = 0.0
    for t in trades:
        cum += t.get("pnl", 0)
        peak_so_far = max(peak_so_far, cum)
        dd = (peak_so_far - cum) / peak_so_far * 100 if peak_so_far else 0
        max_dd_pct = max(max_dd_pct, dd)
    current_dd = (peak - balance) / peak * 100 if peak else 0

    # Card 1 — Portfolio
    cards = [
        ("Portfolio", [
            ("Current Balance", f"${balance:,.2f}"),
            ("Starting Balance", f"${start_bal:,.2f}"),
            ("Total P&L (dollars)", f"${total_pnl:,.2f}"),
            ("Total P&L (percent)", f"{pnl_pct:.1f}%"),
            ("Peak Balance", f"${peak:,.2f}"),
            ("Max Drawdown (%)", f"{max_dd_pct:.1f}%"),
            ("Current Drawdown (%)", f"{current_dd:.1f}%"),
        ]),
    ]

    # Card 2 — Performance
    wins = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) < 0]
    n = len(trades)
    wr = len(wins) / n * 100 if n else 0.0
    gross_wins = sum(t.get("pnl", 0) for t in wins)
    gross_losses = abs(sum(t.get("pnl", 0) for t in losses))
    pf = gross_wins / gross_losses if gross_losses else float("inf") if gross_wins else 0
    avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
    avg_loss = np.mean([t["pnl"] for t in losses]) if losses else 0
    wl_ratio = avg_win / abs(avg_loss) if avg_loss else 0
    ev = (len(wins) / n * avg_win - len(losses) / n * abs(avg_loss)) if n else 0.0

    returns = []
    for t in trades:
        ref = t.get("entry", 0.01) * t.get("contracts", 1)
        if ref > 0:
            returns.append(t.get("pnl", 0) / ref)
    sharpe = 0.0
    sortino = 0.0
    if len(returns) > 1 and np.std(returns) > 0:
        sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(252 * 96))
    downside = np.array([r for r in returns if r < 0])
    downside_std = np.std(downside) if len(downside) > 1 else 0.01
    if downside_std > 0 and returns:
        sortino = float(np.mean(returns) / downside_std * np.sqrt(252 * 96))

    sim_sharpe = (sim or {}).get("returns_hist")
    if sim_sharpe and len(sim_sharpe) > 1:
        r = np.array(sim_sharpe)
        sharpe = float(np.mean(r) / np.std(r) * np.sqrt(252 * 96)) if np.std(r) > 0 else sharpe

    cards.append(("Performance", [
        ("Total Trades", n),
        ("Wins / Losses", f"{len(wins)} / {len(losses)}"),
        ("Win Rate (%)", f"{wr:.1f}%"),
        ("Profit Factor", f"{pf:.2f}" if pf != float("inf") else "∞"),
        ("Average Win ($)", f"${avg_win:,.2f}"),
        ("Average Loss ($)", f"${avg_loss:,.2f}"),
        ("Win/Loss Ratio", f"{wl_ratio:.2f}"),
        ("Expected Value per trade", f"${ev:,.2f}"),
        ("Sharpe Ratio", f"{sharpe:.2f}"),
        ("Sortino Ratio", f"{sortino:.2f}"),
    ]))

    # Card 3 — Risk
    max_loss = min((t.get("pnl", 0) for t in trades), default=0)
    max_win = max((t.get("pnl", 0) for t in trades), default=0)
    consec_loss = 0
    max_consec_loss = 0
    max_consec_win = 0
    consec_win = 0
    for t in trades:
        if t.get("pnl", 0) > 0:
            consec_win += 1
            consec_loss = 0
            max_consec_win = max(max_consec_win, consec_win)
        else:
            consec_loss += 1
            consec_win = 0
            max_consec_loss = max(max_consec_loss, consec_loss)
    var95 = (sim or {}).get("var_95", 0)
    curr_consec = (sim or {}).get("consec_losses", 0)
    halted = (sim or {}).get("_halted_at") is not None
    cb_status = "ACTIVE" if halted else "OK"

    cards.append(("Risk", [
        ("Largest Single Loss ($)", f"${max_loss:,.2f}"),
        ("Largest Single Win ($)", f"${max_win:,.2f}"),
        ("Max Consecutive Losses", max_consec_loss),
        ("Max Consecutive Wins", max_consec_win),
        ("VaR 95% (%)", f"{var95 * 100:.1f}%"),
        ("Current Consec Losses", curr_consec),
        ("Circuit Breaker Status", cb_status),
    ]))

    # Card 4 — Per-Asset
    assets = ["BTC", "ETH", "SOL", "XRP"]
    astats = (sim or {}).get("asset_stats", {})
    asset_rows = []
    for a in assets:
        s = astats.get(a, {})
        if isinstance(s, dict):
            tr = s.get("wins", 0) + s.get("losses", 0)
            w = s.get("wins", 0)
            l = s.get("losses", 0)
            pnl = s.get("total_pnl", 0)
        else:
            by_asset = [t for t in trades if t.get("asset") == a]
            tr = len(by_asset)
            w = sum(1 for t in by_asset if t.get("pnl", 0) > 0)
            l = tr - w
            pnl = sum(t.get("pnl", 0) for t in by_asset)
        wr_a = w / tr * 100 if tr else 0
        avg_pnl = pnl / tr if tr else 0
        asset_rows.append((a, tr, w, l, wr_a, pnl, avg_pnl))

    # Card 5 — Session
    dates = set()
    for t in trades:
        ts = parse_ts(t.get("ts", ""))
        if ts:
            dates.add(ts.date())
    first_ts = min((parse_ts(t.get("ts", "")) for t in trades if t.get("ts")), default=None)
    last_ts = max((parse_ts(t.get("ts", "")) for t in trades if t.get("ts")), default=None)
    first_str = first_ts.strftime("%Y-%m-%d") if first_ts else "N/A"
    last_str = last_ts.strftime("%Y-%m-%d") if last_ts else "N/A"
    sessions = len(dates)
    avg_trades = n / sessions if sessions else 0

    cards.append(("Session Info", [
        ("Report generated", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Data covers", f"{first_str} to {last_str}"),
        ("Total sessions", sessions),
        ("Avg trades per session", f"{avg_trades:.1f}"),
    ]))

    # Write cards (two-column layout)
    col = 1
    for title, items in cards:
        if title == "Per-Asset Summary Table":
            continue
        ws.cell(row, col, title)
        ws.cell(row, col).font = Font(bold=True, size=12)
        ws.cell(row, col).border = THICK_BORDER
        row += 1
        for label, val in items:
            ws.cell(row, col, label)
            ws.cell(row, col + 1, val)
            for c in range(col, col + 2):
                cell = ws.cell(row, c)
                cell.border = THIN_BORDER
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    if "P&L" in label or "Balance" in label or "Win" in label or "Loss" in label:
                        try:
                            v = float(str(val).replace("$", "").replace("%", "").replace(",", ""))
                            if v > 0:
                                cell.fill = LIGHT_GREEN
                                cell.font = Font(color="00B050")
                            elif v < 0:
                                cell.fill = LIGHT_RED
                                cell.font = Font(color="C00000")
                        except ValueError:
                            pass
                if label == "Circuit Breaker Status":
                    if val == "ACTIVE":
                        cell.fill = LIGHT_RED
                        cell.font = Font(color="C00000")
                    else:
                        cell.fill = LIGHT_GREEN
                        cell.font = Font(color="00B050")
            row += 1
        row += 1

    # Per-Asset table
    row += 1
    ws.cell(row, 1, "Per-Asset Summary")
    ws.cell(row, 1).font = Font(bold=True, size=12)
    row += 1
    headers = ["Asset", "Trades", "Wins", "Losses", "Win Rate", "Total PnL", "Avg PnL"]
    for c, h in enumerate(headers, 1):
        ws.cell(row, c, h)
        ws.cell(row, c).fill = HEADER_FILL
        ws.cell(row, c).font = Font(bold=True, color="FFFFFF")
        ws.cell(row, c).border = THIN_BORDER
    row += 1
    for a, tr, w, l, wr_a, pnl, avg_pnl in asset_rows:
        ws.cell(row, 1, a)
        ws.cell(row, 2, tr)
        ws.cell(row, 3, w)
        ws.cell(row, 4, l)
        ws.cell(row, 5, f"{wr_a:.1f}%")
        ws.cell(row, 6, pnl)
        ws.cell(row, 7, avg_pnl)
        for c in range(1, 8):
            cell = ws.cell(row, c)
            cell.border = THIN_BORDER
            if c == 5:  # Win rate
                if wr_a >= 60:
                    cell.fill = LIGHT_GREEN
                    cell.font = Font(color="00B050")
                elif wr_a >= 45:
                    cell.fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
                else:
                    cell.fill = LIGHT_RED
                    cell.font = Font(color="C00000")
            elif c in (6, 7):  # PnL
                if pnl > 0:
                    cell.fill = LIGHT_GREEN
                    cell.font = Font(color="00B050")
                elif pnl < 0:
                    cell.fill = LIGHT_RED
                    cell.font = Font(color="C00000")
        row += 1

    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 16


# --- Sheet 2: Trade Log ---
def write_trade_log_sheet(wb: Workbook, trades: list) -> None:
    ws = wb.create_sheet("Trade Log", 1)
    ws.sheet_properties.tabColor = "2E75B6"

    headers = [
        "Trade #", "Date", "Time (ET)", "Asset", "Side", "Entry Price", "Exit Price", "Contracts",
        "Gross PnL", "Net PnL", "Cumulative PnL", "Win/Loss", "Strategy", "Price to Beat", "Exit Spot",
        "Edge at Entry", "Window ID", "Time to Expiry", "Result Correct", "Equity Trend",
    ]
    for c, h in enumerate(headers, 1):
        ws.cell(1, c, h)
        ws.cell(1, c).font = Font(bold=True)
        ws.cell(1, c).alignment = Alignment(horizontal="center")
        ws.cell(1, c).fill = HEADER_FILL
        ws.cell(1, c).font = Font(bold=True, color="FFFFFF")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:T{len(trades)+1}"

    cum = 1000.0  # assume starting balance
    for i, t in enumerate(trades, 1):
        r = i + 1
        ts = parse_ts(t.get("ts", ""))
        date_str = ts.strftime("%d-%b-%Y") if ts else ""
        time_str = ts.strftime("%H:%M:%S") if ts else ""
        side = (t.get("side") or "yes").upper()
        entry = t.get("entry", 0)
        exit_ = t.get("exit", 0)
        contracts = t.get("contracts", 0)
        gross = t.get("pnl", 0)
        fee = 0.01 * contracts
        net = gross - fee
        cum += net
        wl = "WIN" if gross > 0 else "LOSS"
        ptb = t.get("price_to_beat")
        spot = t.get("exit_spot")
        edge = abs(entry - 0.5) if entry else 0
        tte = compute_time_to_expiry(t)
        rc = result_correct(t)

        ws.cell(r, 1, i)
        ws.cell(r, 2, date_str)
        ws.cell(r, 3, time_str)
        ws.cell(r, 4, t.get("asset", ""))
        ws.cell(r, 5, "YES" if side == "YES" else "NO")
        ws.cell(r, 6, round(entry, 3))
        ws.cell(r, 7, round(exit_, 3))
        ws.cell(r, 8, contracts)
        ws.cell(r, 9, round(gross, 2))
        ws.cell(r, 10, round(net, 2))
        ws.cell(r, 11, round(cum, 2))
        ws.cell(r, 12, wl)
        ws.cell(r, 13, t.get("strategy", ""))
        ws.cell(r, 14, round(ptb, 4) if ptb is not None else "")
        ws.cell(r, 15, round(spot, 4) if spot is not None else "")
        ws.cell(r, 16, round(edge, 3))
        ws.cell(r, 17, t.get("window_id", ""))
        ws.cell(r, 18, round(tte, 0) if tte is not None else "")
        ws.cell(r, 19, rc)
        # Column 20: Sparkline - openpyxl does not support sparklines; leave blank with TODO
        ws.cell(r, 20, "")

        for c in (9, 10, 11):
            cell = ws.cell(r, c)
            if gross > 0:
                cell.fill = LIGHT_GREEN
                cell.font = Font(color="00B050")
            elif gross < 0:
                cell.fill = LIGHT_RED
                cell.font = Font(color="C00000")
        cell = ws.cell(r, 12)
        if wl == "WIN":
            cell.fill = LIGHT_GREEN
            cell.font = Font(color="00B050")
        else:
            cell.fill = LIGHT_RED
            cell.font = Font(color="C00000")

        if r % 2 == 0:
            for c in range(1, 21):
                if c not in (9, 10, 11, 12):  # Don't overwrite PnL/Win-Loss colors
                    ws.cell(r, c).fill = ALT_ROW_FILL

    for col in range(1, 21):
        ws.column_dimensions[get_column_letter(col)].width = 12


# --- Sheet 3: Performance Analytics ---
def write_performance_sheet(wb: Workbook, trades: list) -> None:
    ws = wb.create_sheet("Performance Analytics", 2)
    ws.sheet_properties.tabColor = "7030A0"

    row = 1
    # Section 1 — Win Rate Analysis
    ws.cell(row, 1, "Win Rate Analysis")
    ws.cell(row, 1).font = Font(bold=True, size=12)
    ws.cell(row, 1).fill = SUBHEADER_FILL
    ws.cell(row, 1).font = Font(bold=True, color="FFFFFF")
    row += 1
    h1 = ["Asset", "Trades", "Win Rate", "vs Expected (50%)", "Z-score significance"]
    for c, h in enumerate(h1, 1):
        ws.cell(row, c, h)
        ws.cell(row, c).font = Font(bold=True)
        ws.cell(row, c).fill = HEADER_FILL
        ws.cell(row, c).font = Font(bold=True, color="FFFFFF")
    row += 1
    for asset in ["BTC", "ETH", "SOL", "XRP"]:
        by_a = [t for t in trades if t.get("asset") == asset]
        n = len(by_a)
        w = sum(1 for t in by_a if t.get("pnl", 0) > 0)
        wr = w / n * 100 if n else 0
        vs_exp = wr - 50
        z = (w - n * 0.5) / (np.sqrt(n * 0.25)) if n > 0 else 0
        ws.cell(row, 1, asset)
        ws.cell(row, 2, n)
        ws.cell(row, 3, f"{wr:.1f}%")
        ws.cell(row, 4, f"{vs_exp:+.1f}%")
        ws.cell(row, 5, f"{z:.2f}" + (" (sig)" if abs(z) > 1.96 else ""))
        row += 1

    row += 2
    # Section 2 — PnL Distribution by entry bucket
    buckets = [
        (0.05, 0.20, "0.05-0.20 (strong NO)"),
        (0.20, 0.35, "0.20-0.35 (lean NO)"),
        (0.35, 0.45, "0.35-0.45 (slight NO)"),
        (0.45, 0.55, "0.45-0.55 (near 50/50)"),
        (0.55, 0.65, "0.55-0.65 (slight YES)"),
        (0.65, 0.80, "0.65-0.80 (lean YES)"),
        (0.80, 0.95, "0.80-0.95 (strong YES)"),
    ]
    ws.cell(row, 1, "PnL Distribution by Entry Price")
    ws.cell(row, 1).font = Font(bold=True, size=12)
    ws.cell(row, 1).fill = SUBHEADER_FILL
    ws.cell(row, 1).font = Font(bold=True, color="FFFFFF")
    row += 1
    h2 = ["Bucket", "Count", "Total PnL", "Avg PnL", "Win Rate"]
    for c, h in enumerate(h2, 1):
        ws.cell(row, c, h)
        ws.cell(row, c).font = Font(bold=True)
        ws.cell(row, c).fill = HEADER_FILL
        ws.cell(row, c).font = Font(bold=True, color="FFFFFF")
    row += 1
    for lo, hi, label in buckets:
        by_b = [t for t in trades if lo <= t.get("entry", 0) < hi]
        n = len(by_b)
        pnl = sum(t.get("pnl", 0) for t in by_b)
        avg = pnl / n if n else 0
        wr_b = sum(1 for t in by_b if t.get("pnl", 0) > 0) / n * 100 if n else 0
        ws.cell(row, 1, label)
        ws.cell(row, 2, n)
        ws.cell(row, 3, round(pnl, 2))
        ws.cell(row, 4, round(avg, 2))
        ws.cell(row, 5, f"{wr_b:.1f}%")
        row += 1

    row += 2
    # Section 3 — Time of Day
    ws.cell(row, 1, "Time of Day Analysis (ET hour)")
    ws.cell(row, 1).font = Font(bold=True, size=12)
    ws.cell(row, 1).fill = SUBHEADER_FILL
    ws.cell(row, 1).font = Font(bold=True, color="FFFFFF")
    row += 1
    h3 = ["Hour", "Trades", "Win Rate", "Avg PnL"]
    for c, h in enumerate(h3, 1):
        ws.cell(row, c, h)
        ws.cell(row, c).font = Font(bold=True)
        ws.cell(row, c).fill = HEADER_FILL
        ws.cell(row, c).font = Font(bold=True, color="FFFFFF")
    row += 1
    by_hour = {}
    for t in trades:
        ts = parse_ts(t.get("ts", ""))
        if ts:
            h = (ts.hour + ET_OFFSET // 3600) % 24
            by_hour.setdefault(h, []).append(t)
    for h in sorted(by_hour.keys()):
        by_h = by_hour[h]
        n = len(by_h)
        wr_h = sum(1 for t in by_h if t.get("pnl", 0) > 0) / n * 100 if n else 0
        avg_h = np.mean([t.get("pnl", 0) for t in by_h]) if by_h else 0
        ws.cell(row, 1, f"{h:02d}:00")
        ws.cell(row, 2, n)
        ws.cell(row, 3, f"{wr_h:.1f}%")
        ws.cell(row, 4, round(avg_h, 2))
        row += 1

    row += 2
    # Section 4 — Time to Expiry
    ws.cell(row, 1, "Time to Expiry Analysis")
    ws.cell(row, 1).font = Font(bold=True, size=12)
    ws.cell(row, 1).fill = SUBHEADER_FILL
    ws.cell(row, 1).font = Font(bold=True, color="FFFFFF")
    row += 1
    tte_buckets = [(0, 60, "0-60s"), (60, 180, "60-180s"), (180, 450, "180-450s"), (450, 900, "450-900s")]
    h4 = ["Bucket", "Trades", "Win Rate", "Avg PnL", "Total PnL"]
    for c, h in enumerate(h4, 1):
        ws.cell(row, c, h)
        ws.cell(row, c).font = Font(bold=True)
        ws.cell(row, c).fill = HEADER_FILL
        ws.cell(row, c).font = Font(bold=True, color="FFFFFF")
    row += 1
    for lo, hi, label in tte_buckets:
        by_t = [t for t in trades if lo <= (compute_time_to_expiry(t) or 0) < hi]
        n = len(by_t)
        pnl_t = sum(t.get("pnl", 0) for t in by_t)
        avg_t = pnl_t / n if n else 0
        wr_t = sum(1 for t in by_t if t.get("pnl", 0) > 0) / n * 100 if n else 0
        ws.cell(row, 1, label)
        ws.cell(row, 2, n)
        ws.cell(row, 3, f"{wr_t:.1f}%")
        ws.cell(row, 4, round(avg_t, 2))
        ws.cell(row, 5, round(pnl_t, 2))
        row += 1

    row += 2
    # Section 5 — Rolling Performance
    ws.cell(row, 1, "Rolling Win Rate")
    ws.cell(row, 1).font = Font(bold=True, size=12)
    ws.cell(row, 1).fill = SUBHEADER_FILL
    ws.cell(row, 1).font = Font(bold=True, color="FFFFFF")
    row += 1
    h5 = ["Trade #", "5-trade rolling WR", "10-trade rolling WR"]
    for c, h in enumerate(h5, 1):
        ws.cell(row, c, h)
        ws.cell(row, c).font = Font(bold=True)
        ws.cell(row, c).fill = HEADER_FILL
        ws.cell(row, c).font = Font(bold=True, color="FFFFFF")
    row += 1
    wins_arr = [1 if t.get("pnl", 0) > 0 else 0 for t in trades]
    for i in range(len(trades)):
        slice5 = wins_arr[max(0, i - 4) : i + 1]
        slice10 = wins_arr[max(0, i - 9) : i + 1]
        r5 = float(np.mean(slice5)) * 100 if slice5 else 0
        r10 = float(np.mean(slice10)) * 100 if slice10 else 0
        ws.cell(row, 1, i + 1)
        ws.cell(row, 2, f"{r5:.1f}%")
        ws.cell(row, 3, f"{r10:.1f}%")
        row += 1

    row += 2
    # Section 6 — Asset Correlation (simplified)
    ws.cell(row, 1, "Asset Correlation (win/loss overlap)")
    ws.cell(row, 1).font = Font(bold=True, size=12)
    ws.cell(row, 1).fill = SUBHEADER_FILL
    ws.cell(row, 1).font = Font(bold=True, color="FFFFFF")
    row += 1
    ws.cell(row, 1, "Pair")
    ws.cell(row, 2, "Correlation")
    ws.cell(row, 1).font = Font(bold=True)
    ws.cell(row, 2).font = Font(bold=True)
    row += 1
    pairs = [("BTC", "ETH"), ("BTC", "SOL"), ("BTC", "XRP"), ("ETH", "SOL"), ("ETH", "XRP"), ("SOL", "XRP")]
    by_wid = {}
    for t in trades:
        wid = t.get("window_id", "")
        if wid:
            by_wid.setdefault(wid, {})[t.get("asset", "")] = 1 if t.get("pnl", 0) > 0 else 0
    for a1, a2 in pairs:
        vec1, vec2 = [], []
        for wid, d in by_wid.items():
            if a1 in d and a2 in d:
                vec1.append(d[a1])
                vec2.append(d[a2])
        if len(vec1) >= 5:
            corr = np.corrcoef(vec1, vec2)[0, 1] if np.std(vec1) > 0 and np.std(vec2) > 0 else 0
            ws.cell(row, 1, f"{a1}/{a2}")
            ws.cell(row, 2, f"{corr:.3f}")
        else:
            ws.cell(row, 1, f"{a1}/{a2}")
            ws.cell(row, 2, "N/A")
        row += 1

    row += 2
    # Section 7 — Side Analysis
    ws.cell(row, 1, "Side Analysis")
    ws.cell(row, 1).font = Font(bold=True, size=12)
    ws.cell(row, 1).fill = SUBHEADER_FILL
    ws.cell(row, 1).font = Font(bold=True, color="FFFFFF")
    row += 1
    h7 = ["Side", "Trades", "Win Rate", "Avg PnL", "Total PnL"]
    for c, h in enumerate(h7, 1):
        ws.cell(row, c, h)
        ws.cell(row, c).font = Font(bold=True)
        ws.cell(row, c).fill = HEADER_FILL
        ws.cell(row, c).font = Font(bold=True, color="FFFFFF")
    row += 1
    for side in ["yes", "no"]:
        by_s = [t for t in trades if (t.get("side") or "yes").lower() == side]
        n = len(by_s)
        pnl_s = sum(t.get("pnl", 0) for t in by_s)
        avg_s = pnl_s / n if n else 0
        wr_s = sum(1 for t in by_s if t.get("pnl", 0) > 0) / n * 100 if n else 0
        ws.cell(row, 1, side.upper())
        ws.cell(row, 2, n)
        ws.cell(row, 3, f"{wr_s:.1f}%")
        ws.cell(row, 4, round(avg_s, 2))
        ws.cell(row, 5, round(pnl_s, 2))
        row += 1

    ws.column_dimensions["A"].width = 35


# --- Sheet 4: Equity Curve ---
def write_equity_sheet(wb: Workbook, trades: list, sim: Optional[dict]) -> None:
    ws = wb.create_sheet("Equity Curve", 3)
    ws.sheet_properties.tabColor = "ED7D31"

    start = (sim or {}).get("starting_balance", 1000.0)
    cum = start
    peak = start
    balances = [start]
    cum_pnls = [0]
    peaks = [start]
    drawdowns = [0]
    dd_pcts = [0]

    for t in trades:
        cum += t.get("pnl", 0)
        peak = max(peak, cum)
        dd = peak - cum
        dd_pct = (dd / peak * 100) if peak else 0
        balances.append(cum)
        cum_pnls.append(cum - start)
        peaks.append(peak)
        drawdowns.append(dd)
        dd_pcts.append(-dd_pct)

    # Data table
    ws.cell(1, 1, "Trade #")
    ws.cell(1, 2, "Date")
    ws.cell(1, 3, "Balance")
    ws.cell(1, 4, "Cumulative PnL")
    ws.cell(1, 5, "Peak")
    ws.cell(1, 6, "Drawdown $")
    ws.cell(1, 7, "Drawdown %")
    for c in range(1, 8):
        ws.cell(1, c).font = Font(bold=True)
        ws.cell(1, c).fill = HEADER_FILL
        ws.cell(1, c).font = Font(bold=True, color="FFFFFF")

    max_dd_row = 1
    max_dd_val = 0
    for i in range(len(balances)):
        r = i + 2
        ts = parse_ts(trades[i - 1].get("ts", "")) if i > 0 else None
        date_str = ts.strftime("%Y-%m-%d") if ts else "Start"
        ws.cell(r, 1, i)
        ws.cell(r, 2, date_str)
        ws.cell(r, 3, round(balances[i], 2))
        ws.cell(r, 4, round(cum_pnls[i], 2))
        ws.cell(r, 5, round(peaks[i], 2))
        ws.cell(r, 6, round(drawdowns[i], 2))
        ws.cell(r, 7, f"{dd_pcts[i]:.1f}%")
        if drawdowns[i] > max_dd_val:
            max_dd_val = drawdowns[i]
            max_dd_row = r
    ws.cell(max_dd_row, 6).fill = LIGHT_RED
    ws.cell(max_dd_row, 7).fill = LIGHT_RED

    # Stats
    total_pnl = cum - start
    max_dd = max(drawdowns)
    max_dd_pct = max(dd_pcts)
    n_days = len(set(parse_ts(t.get("ts", "")).date() for t in trades if parse_ts(t.get("ts", "")))) or 1
    ann_return = (total_pnl / start) * (252 / n_days) * 100 if n_days else 0
    calmar = ann_return / abs(max_dd_pct) if max_dd_pct else 0
    recovery = total_pnl / max_dd if max_dd else 0
    ulcer = np.sqrt(np.mean(np.array(dd_pcts) ** 2)) if dd_pcts else 0

    r = len(balances) + 4
    ws.cell(r, 1, "Calmar Ratio")
    ws.cell(r, 2, f"{calmar:.2f}")
    r += 1
    ws.cell(r, 1, "Recovery Factor")
    ws.cell(r, 2, f"{recovery:.2f}")
    r += 1
    ws.cell(r, 1, "Ulcer Index")
    ws.cell(r, 2, f"{ulcer:.2f}%")

    # Charts
    chart1 = LineChart()
    chart1.title = "Equity Curve — Kalshi Paper Trading"
    chart1.width = 25
    chart1.height = 12
    chart1.y_axis.title = "Balance ($)"
    chart1.x_axis.title = "Trade #"
    data = Reference(ws, min_col=3, min_row=1, max_row=len(balances) + 1)
    cats = Reference(ws, min_col=1, min_row=2, max_row=len(balances) + 1)
    chart1.add_data(data, titles_from_data=True)
    chart1.set_categories(cats)
    ws.add_chart(chart1, "I2")

    chart2 = LineChart()
    chart2.title = "Drawdown"
    chart2.width = 25
    chart2.height = 6
    data2 = Reference(ws, min_col=7, min_row=1, max_row=len(balances) + 1)
    chart2.add_data(data2, titles_from_data=True)
    chart2.set_categories(cats)
    ws.add_chart(chart2, "I18")


# --- Sheet 5: Daily Summary ---
def write_daily_sheet(wb: Workbook, trades: list) -> None:
    ws = wb.create_sheet("Daily Summary", 4)
    ws.sheet_properties.tabColor = "31869B"

    by_date = {}
    for t in trades:
        ts = parse_ts(t.get("ts", ""))
        if ts:
            d = ts.date()
            by_date.setdefault(d, []).append(t)

    headers = ["Date", "Day of Week", "Sessions", "Trades", "Wins", "Losses", "Win Rate",
               "Day PnL", "Cumulative PnL", "Best Trade", "Worst Trade", "Assets Traded"]
    for c, h in enumerate(headers, 1):
        ws.cell(1, c, h)
        ws.cell(1, c).font = Font(bold=True)
        ws.cell(1, c).fill = HEADER_FILL
        ws.cell(1, c).font = Font(bold=True, color="FFFFFF")

    cum = 0
    row = 2
    for d in sorted(by_date.keys()):
        day_trades = by_date[d]
        n = len(day_trades)
        w = sum(1 for t in day_trades if t.get("pnl", 0) > 0)
        l = n - w
        wr = w / n * 100 if n else 0
        pnl_d = sum(t.get("pnl", 0) for t in day_trades)
        cum += pnl_d
        best = max(t.get("pnl", 0) for t in day_trades) if day_trades else 0
        worst = min(t.get("pnl", 0) for t in day_trades) if day_trades else 0
        assets = ", ".join(sorted(set(t.get("asset", "") for t in day_trades)))
        dow = d.strftime("%A")

        ws.cell(row, 1, d.strftime("%Y-%m-%d"))
        ws.cell(row, 2, dow)
        ws.cell(row, 3, 1)
        ws.cell(row, 4, n)
        ws.cell(row, 5, w)
        ws.cell(row, 6, l)
        ws.cell(row, 7, f"{wr:.1f}%")
        ws.cell(row, 8, round(pnl_d, 2))
        ws.cell(row, 9, round(cum, 2))
        ws.cell(row, 10, round(best, 2))
        ws.cell(row, 11, round(worst, 2))
        ws.cell(row, 12, assets)

        if pnl_d > 0:
            ws.cell(row, 8).fill = LIGHT_GREEN
            ws.cell(row, 8).font = Font(color="00B050")
        elif pnl_d < 0:
            ws.cell(row, 8).fill = LIGHT_RED
            ws.cell(row, 8).font = Font(color="C00000")
        if wr >= 60:
            ws.cell(row, 7).fill = LIGHT_GREEN
            ws.cell(row, 7).font = Font(color="00B050")
        elif wr >= 45:
            ws.cell(row, 7).fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
        else:
            ws.cell(row, 7).fill = LIGHT_RED
            ws.cell(row, 7).font = Font(color="C00000")

        if row % 2 == 0:
            for c in range(1, 13):
                if ws.cell(row, c).fill.fill_type is None:
                    ws.cell(row, c).fill = ALT_ROW_FILL
        row += 1

    # Totals row
    row += 1
    ws.cell(row, 1, "TOTALS")
    ws.cell(row, 2, "")
    ws.cell(row, 3, len(by_date))
    ws.cell(row, 4, len(trades))
    ws.cell(row, 5, sum(1 for t in trades if t.get("pnl", 0) > 0))
    ws.cell(row, 6, sum(1 for t in trades if t.get("pnl", 0) <= 0))
    tot_wr = sum(1 for t in trades if t.get("pnl", 0) > 0) / len(trades) * 100 if trades else 0
    ws.cell(row, 7, f"{tot_wr:.1f}%")
    ws.cell(row, 8, round(sum(t.get("pnl", 0) for t in trades), 2))
    ws.cell(row, 9, round(cum, 2))
    ws.cell(row, 10, "")
    ws.cell(row, 11, "")
    ws.cell(row, 12, "")
    for c in range(1, 13):
        ws.cell(row, c).fill = TOTALS_FILL
        ws.cell(row, c).font = Font(bold=True)

    # Footer averages
    row += 2
    ws.cell(row, 1, "Avg trades/day")
    ws.cell(row, 2, f"{len(trades) / len(by_date):.1f}" if by_date else "0")
    row += 1
    ws.cell(row, 1, "Avg win rate")
    ws.cell(row, 2, f"{tot_wr:.1f}%")
    row += 1
    ws.cell(row, 1, "Avg day PnL")
    ws.cell(row, 2, f"${cum / len(by_date):.2f}" if by_date else "$0")

    ws.column_dimensions["A"].width = 14


# --- Sheet 6: WAIT Reason Analysis ---
def write_wait_sheet(wb: Workbook, decisions: list) -> None:
    ws = wb.create_sheet("WAIT Analysis", 5)
    ws.sheet_properties.tabColor = "FFC000"

    # Extract reason from each decision (reason may be in different formats)
    reasons = []
    for d in decisions:
        r = d.get("reason", "")
        if r:
            # Bucket for grouping (e.g. "edge(+0.031<0.116)" -> "edge")
            bucket = r.split("(")[0].split("<")[0].strip()
            reasons.append((bucket, r, d.get("asset", "")))

    from collections import Counter
    cnt = Counter(r[0] for r in reasons)
    total = len(reasons)
    sorted_reasons = cnt.most_common()

    ws.cell(1, 1, "WAIT Reason")
    ws.cell(1, 2, "Count")
    ws.cell(1, 3, "% of Total")
    ws.cell(1, 4, "Trend")
    for c in range(1, 5):
        ws.cell(1, c).font = Font(bold=True)
        ws.cell(1, c).fill = HEADER_FILL
        ws.cell(1, c).font = Font(bold=True, color="FFFFFF")

    RED_REASONS = {"feed_unreliable", "spot_confidence_low", "structural_model_invalid", "no_price_to_beat"}
    AMBER_REASONS = {"lag_absent", "lag_confidence_low", "mispricing_weak", "uncertain_near_50", "vol_too_high"}
    GREEN_REASONS = {"position_open", "window_boundary", "portfolio_cap", "circuit_breaker_cooldown", "circuit_breaker"}

    row = 2
    for reason, count in sorted_reasons:
        pct = count / total * 100 if total else 0
        ws.cell(row, 1, reason)
        ws.cell(row, 2, count)
        ws.cell(row, 3, f"{pct:.1f}%")
        ws.cell(row, 4, "")
        r_lower = reason.lower()
        if any(x in r_lower for x in ["spot_confidence", "structural_model", "no_price", "feed"]):
            ws.cell(row, 1).fill = LIGHT_RED
        elif any(x in r_lower for x in ["lag", "mispricing", "uncertain", "vol_too"]):
            ws.cell(row, 1).fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
        elif any(x in r_lower for x in ["position_open", "window_boundary", "portfolio_cap", "circuit"]):
            ws.cell(row, 1).fill = LIGHT_GREEN
        row += 1

    row += 2
    ws.cell(row, 1, "Interpretation Guide")
    ws.cell(row, 1).font = Font(bold=True)
    row += 1
    guide = [
        "structural_model_invalid — Structural probability model could not compute a valid p_base. Often indicates feed instability.",
        "spot_confidence_low — Fewer than 2 price venues available. Feed unreliable.",
        "lag_absent — Kalshi lag tracker has insufficient data to detect edge.",
        "mispricing_weak — Market price too close to model price; no edge.",
        "position_open — Already holding a position for this asset/window.",
        "window_boundary — Outside trading window (skip open/close).",
        "portfolio_cap — Total exposure would exceed limit.",
    ]
    for g in guide:
        ws.cell(row, 1, g)
        row += 1

    row += 2
    ws.cell(row, 1, "Per-Asset Top WAIT Reasons")
    ws.cell(row, 1).font = Font(bold=True)
    row += 1
    h = ["Asset", "Top Reason", "2nd Reason", "3rd Reason"]
    for c, x in enumerate(h, 1):
        ws.cell(row, c, x)
        ws.cell(row, c).font = Font(bold=True)
        ws.cell(row, c).fill = SUBHEADER_FILL
        ws.cell(row, c).font = Font(bold=True, color="FFFFFF")
    row += 1
    for asset in ["BTC", "ETH", "SOL", "XRP"]:
        by_a = [r[0] for r in reasons if r[2] == asset]
        cnt_a = Counter(by_a)
        top3 = cnt_a.most_common(3)
        ws.cell(row, 1, asset)
        ws.cell(row, 2, top3[0][0] if top3 else "N/A")
        ws.cell(row, 3, top3[1][0] if len(top3) > 1 else "N/A")
        ws.cell(row, 4, top3[2][0] if len(top3) > 2 else "N/A")
        row += 1

    ws.column_dimensions["A"].width = 45


# --- Sheet 7: Risk Dashboard ---
def write_risk_sheet(wb: Workbook, trades: list, sim: Optional[dict]) -> None:
    ws = wb.create_sheet("Risk Dashboard", 6)
    ws.sheet_properties.tabColor = "C00000"

    start = (sim or {}).get("starting_balance", 1000.0)
    cum = start

    # Section 1 — Position Sizing
    ws.cell(1, 1, "Position Sizing Analysis")
    ws.cell(1, 1).font = Font(bold=True, size=12)
    ws.cell(1, 1).fill = SUBHEADER_FILL
    ws.cell(1, 1).font = Font(bold=True, color="FFFFFF")
    row = 2
    h1 = ["Trade #", "Contracts", "Entry", "Position Size ($)", "% of Balance"]
    for c, h in enumerate(h1, 1):
        ws.cell(row, c, h)
        ws.cell(row, c).font = Font(bold=True)
        ws.cell(row, c).fill = HEADER_FILL
        ws.cell(row, c).font = Font(bold=True, color="FFFFFF")
    row += 1
    over_5 = 0
    over_8 = 0
    sizes = []
    for i, t in enumerate(trades, 1):
        pos_size = t.get("entry", 0) * t.get("contracts", 0)
        pct = pos_size / cum * 100 if cum else 0
        cum += t.get("pnl", 0)
        sizes.append(pos_size)
        if pct > 5:
            over_5 += 1
        if pct > 8:
            over_8 += 1
        ws.cell(row, 1, i)
        ws.cell(row, 2, t.get("contracts", 0))
        ws.cell(row, 3, round(t.get("entry", 0), 3))
        ws.cell(row, 4, round(pos_size, 2))
        ws.cell(row, 5, f"{pct:.1f}%")
        if pct > 8:
            ws.cell(row, 5).fill = LIGHT_RED
        elif pct > 5:
            ws.cell(row, 5).fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
        row += 1
    row += 1
    ws.cell(row, 1, "Avg position size")
    ws.cell(row, 2, f"${np.mean(sizes):.2f}" if sizes else "$0")
    row += 1
    ws.cell(row, 1, "Max position size")
    ws.cell(row, 2, f"${max(sizes):.2f}" if sizes else "$0")
    row += 1
    ws.cell(row, 1, "% trades over 5%")
    ws.cell(row, 2, f"{over_5 / len(trades) * 100:.1f}%" if trades else "0%")

    row += 3
    # Section 2 — Consecutive Loss Analysis
    ws.cell(row, 1, "Consecutive Loss Streaks")
    ws.cell(row, 1).font = Font(bold=True, size=12)
    ws.cell(row, 1).fill = SUBHEADER_FILL
    ws.cell(row, 1).font = Font(bold=True, color="FFFFFF")
    row += 1
    h2 = ["Streak #", "Start Date", "Length", "Total Loss", "Recovery Trades", "Recovery PnL"]
    for c, h in enumerate(h2, 1):
        ws.cell(row, c, h)
        ws.cell(row, c).font = Font(bold=True)
        ws.cell(row, c).fill = HEADER_FILL
        ws.cell(row, c).font = Font(bold=True, color="FFFFFF")
    row += 1
    streak_num = 0
    in_streak = False
    streak_start = None
    streak_len = 0
    streak_loss = 0
    for i, t in enumerate(trades):
        if t.get("pnl", 0) <= 0:
            if not in_streak:
                in_streak = True
                streak_start = parse_ts(t.get("ts", ""))
                streak_len = 1
                streak_loss = t.get("pnl", 0)
            else:
                streak_len += 1
                streak_loss += t.get("pnl", 0)
        else:
            if in_streak:
                streak_num += 1
                recovery = 0
                rec_pnl = 0
                for j in range(i, min(i + 20, len(trades))):
                    if trades[j].get("pnl", 0) > 0:
                        recovery += 1
                        rec_pnl += trades[j].get("pnl", 0)
                        break
                    recovery += 1
                    rec_pnl += trades[j].get("pnl", 0)
                ws.cell(row, 1, streak_num)
                ws.cell(row, 2, streak_start.strftime("%Y-%m-%d") if streak_start else "")
                ws.cell(row, 3, streak_len)
                ws.cell(row, 4, round(streak_loss, 2))
                ws.cell(row, 5, recovery)
                ws.cell(row, 6, round(rec_pnl, 2))
                if streak_len >= 3:
                    for c in range(1, 7):
                        ws.cell(row, c).fill = LIGHT_RED
                row += 1
                in_streak = False

    row += 3
    # Section 3 — Drawdown Periods (simplified)
    ws.cell(row, 1, "Drawdown Periods")
    ws.cell(row, 1).font = Font(bold=True, size=12)
    ws.cell(row, 1).fill = SUBHEADER_FILL
    ws.cell(row, 1).font = Font(bold=True, color="FFFFFF")
    row += 1
    h3 = ["Period #", "Start", "Trough", "Recovery", "Duration", "Max DD ($)", "Max DD (%)"]
    for c, h in enumerate(h3, 1):
        ws.cell(row, c, h)
        ws.cell(row, c).font = Font(bold=True)
        ws.cell(row, c).fill = HEADER_FILL
        ws.cell(row, c).font = Font(bold=True, color="FFFFFF")
    row += 1
    # Simplified: one row for max drawdown
    cum = start
    peak = start
    max_dd = 0
    max_dd_pct = 0
    for t in trades:
        cum += t.get("pnl", 0)
        peak = max(peak, cum)
        dd = peak - cum
        dd_pct = dd / peak * 100 if peak else 0
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd_pct
    ws.cell(row, 1, 1)
    ws.cell(row, 2, "Start")
    ws.cell(row, 3, "Trough")
    ws.cell(row, 4, "Recovery")
    ws.cell(row, 5, len(trades))
    ws.cell(row, 6, round(max_dd, 2))
    ws.cell(row, 7, f"{max_dd_pct:.1f}%")

    row += 3
    # Section 4 — Stress Test
    ws.cell(row, 1, "Stress Test (HYPOTHETICAL)")
    ws.cell(row, 1).font = Font(bold=True, size=12)
    ws.cell(row, 1).fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
    ws.cell(row, 1).font = Font(bold=True)
    row += 1
    n = len(trades)
    w = sum(1 for t in trades if t.get("pnl", 0) > 0)
    avg_win = float(np.mean([t["pnl"] for t in trades if t.get("pnl", 0) > 0])) if w else 0.0
    losses_list = [abs(t["pnl"]) for t in trades if t.get("pnl", 0) < 0]
    avg_loss = float(np.mean(losses_list)) if losses_list else 0.0
    max_single_loss = max(losses_list) if losses_list else 0.0

    # What if WR 45%?
    stress_avg_loss = avg_loss if losses_list else 1.0  # placeholder when no losses
    ev_45 = 0.45 * avg_win - 0.55 * stress_avg_loss
    proj_45 = start + ev_45 * n
    ws.cell(row, 1, "Win rate 45%: Expected PnL per trade")
    ws.cell(row, 2, f"${ev_45:.2f}")
    row += 1
    ws.cell(row, 1, "Win rate 45%: Projected balance after same # trades")
    ws.cell(row, 2, f"${proj_45:.2f}")
    row += 1
    # Avg loss +20%
    ev_loss20 = (w / n * avg_win - (n - w) / n * stress_avg_loss * 1.2) if n else 0
    ws.cell(row, 1, "Avg loss +20%: Expected PnL per trade")
    ws.cell(row, 2, f"${ev_loss20:.2f}")
    row += 1
    # 3 concurrent max losses
    ws.cell(row, 1, "3 concurrent max losses impact")
    ws.cell(row, 2, f"${3 * max_single_loss:.2f}")
    row += 1

    ws.column_dimensions["A"].width = 40


# --- Print settings ---
def set_print_settings(ws) -> None:
    ws.print_title_rows = "1:1"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0


def main() -> None:
    warnings = []
    trades = load_trades()
    if not trades:
        warnings.append("logs/kalshi_trades.jsonl missing or empty")
    sim = load_sim()
    if sim is None:
        warnings.append("logs/kalshi_sim.json missing")
    decisions = load_decisions(10_000)
    if not decisions:
        warnings.append("logs/kalshi_decisions.jsonl missing or empty")

    wb = Workbook()
    # Remove default sheet
    if "Sheet" in wb.sheetnames:
        wb.remove(wb["Sheet"])

    write_summary_sheet(wb, trades, sim, warnings)
    write_trade_log_sheet(wb, trades)
    write_performance_sheet(wb, trades)
    write_equity_sheet(wb, trades, sim)
    write_daily_sheet(wb, trades)
    write_wait_sheet(wb, decisions)
    write_risk_sheet(wb, trades, sim)

    for ws in wb.worksheets:
        set_print_settings(ws)

    wb.save(OUTPUT_PATH)

    first_ts = min((parse_ts(t.get("ts", "")) for t in trades if t.get("ts")), default=None)
    last_ts = max((parse_ts(t.get("ts", "")) for t in trades if t.get("ts")), default=None)
    first_str = first_ts.strftime("%Y-%m-%d") if first_ts else "N/A"
    last_str = last_ts.strftime("%Y-%m-%d") if last_ts else "N/A"

    print("Excel report generated: kalshi_report.xlsx")
    print(f"Sheets: 7 | Trades loaded: {len(trades)} | Date range: {first_str} to {last_str}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Kalshi Excel report")
    parser.add_argument("--watch", action="store_true", help="Regenerate every 60 seconds")
    args = parser.parse_args()
    if args.watch:
        try:
            while True:
                main()
                print(f"Updated kalshi_report.xlsx at {time.strftime('%H:%M:%S')}")
                time.sleep(60)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        main()
