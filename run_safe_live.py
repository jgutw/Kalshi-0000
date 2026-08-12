#!/usr/bin/env python3
"""CLI: safe live start sized from Kalshi available cash."""

from __future__ import annotations

import argparse
import sys

from kalshi_bot.safe_live import (
    format_account,
    format_live_start_result,
    preflight_account,
    start_live_safe,
)


def main() -> None:
    ap = argparse.ArgumentParser(description="Safe LIVE start from Kalshi balance")
    ap.add_argument("--profile", default="max_risk_micro")
    ap.add_argument("--force-opens", action="store_true")
    ap.add_argument("--check-only", action="store_true", help="Print account and exit")
    args = ap.parse_args()

    pf = preflight_account()
    print(format_account(pf))
    if args.check_only:
        return
    try:
        result = start_live_safe(args.profile, force_with_opens=args.force_opens)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(1)
    print()
    print(format_live_start_result(result))


if __name__ == "__main__":
    main()
