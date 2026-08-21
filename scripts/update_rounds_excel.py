#!/usr/bin/env python3
"""
Update / rebuild the quantitative rounds workbook.

  python scripts/update_rounds_excel.py              # rebuild from sessions/
  python scripts/update_rounds_excel.py --min-trades 1
  python scripts/update_rounds_excel.py --session sessions/session_YYYY-MM-DD_HHMM

Auto-updates also run whenever a round is archived (stop/log or --fresh-round).
Output: reports/kalshi_rounds.xlsx
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kalshi_bot.rounds_excel import main

if __name__ == "__main__":
    main()
