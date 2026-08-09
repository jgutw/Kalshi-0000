#!/usr/bin/env python3
"""
Update / rebuild the quantitative rounds workbook.

  python update_rounds_excel.py              # rebuild from sessions/
  python update_rounds_excel.py --min-trades 1
  python update_rounds_excel.py --session sessions/session_YYYY-MM-DD_HHMM

Auto-updates also run whenever a round is archived (stop/log or --fresh-round).
Output: reports/kalshi_rounds.xlsx
"""
from kalshi_bot.rounds_excel import main

if __name__ == "__main__":
    main()
