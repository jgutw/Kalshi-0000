#!/usr/bin/env python3
"""
Telegram bridge for the Kalshi paper bot.

Requires .env:
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_CHAT_ID=...
  TELEGRAM_ENABLED=true

Run alongside the trading bot (separate terminal):
  python run_telegram_bridge.py
"""
from kalshi_bot.telegram.bridge import main

if __name__ == "__main__":
    main()
