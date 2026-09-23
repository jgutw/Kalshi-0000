"""Scripted Binance and OKX feed behavior. No live sockets."""
import ast
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest

from kalshi_bot.data.binance_feed import (
    BINANCE_PING_INTERVAL, BINANCE_RETRY_SECS, BINANCE_WS, binance_stream_url,
    handle_binance_message, run_binance_feed,
)
from kalshi_bot.data.okx_feed import (
    OKX_PING_INTERVAL, OKX_RETRY_SECS, OKX_WS, handle_okx_message, okx_subscription,
    run_okx_feed,
)

ROOT = Path(__file__).resolve().parent.parent


class _Socket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)

    async def send(self, payload):
        self.sent.append(payload)


class _Connect:
    def __init__(self, items):
        self.items = list(items)
        self.calls = []

    def __call__(self, url, ping_interval=None):
        self.calls.append((url, ping_interval))
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FeedMessageTests(unittest.TestCase):
    def test_public_urls_match_pre_refactor_strings(self):
        self.assertEqual(BINANCE_WS, "wss://stream.binance.com:9443/stream")
        self.assertEqual(OKX_WS, "wss://ws.okx.com:8443/ws/v5/public")
        self.assertEqual(BINANCE_RETRY_SECS, 30)
        self.assertEqual(OKX_RETRY_SECS, 5)

    def test_binance_trade_callback_arguments(self):
        trades, books, spots = [], [], []
        raw = json.dumps({
            "stream": "btcusdt@aggTrade",
            "data": {"p": "100.5", "q": "0.25", "m": True},
        })
        handle_binance_message(raw, lambda *a: trades.append(a), books.append, lambda *a: spots.append(a))
        self.assertEqual(trades, [(100.5, 0.25, True)])
        self.assertEqual(books, [])
        self.assertEqual(spots, [])

    def test_binance_depth_mid_uses_positive_best_prices(self):
        trades, books, spots = [], [], []
        bids = [["0", "9"], ["99.5", "2"], ["100", "1"]]
        asks = [["101", "1"], ["100.25", "4"]]
        raw = json.dumps({"stream": "btcusdt@depth20@100ms", "data": {"bids": bids, "asks": asks}})
        handle_binance_message(
            raw,
            lambda *a: trades.append(a),
            lambda b, a: books.append((b, a)),
            lambda *a: spots.append(a),
        )
        self.assertEqual(trades, [])
        self.assertEqual(books, [(bids, asks)])
        self.assertEqual(spots, [("binance", (100.0 + 100.25) / 2)])

    def test_binance_bad_book_updates_book_not_spot(self):
        books, spots = [], []
        raw = json.dumps({
            "stream": "ethusdt@depth20",
            "data": {"bids": [["nope", "1"]], "asks": [["10", "1"]]},
        })
        handle_binance_message(raw, lambda *a: None, lambda b, a: books.append((b, a)), lambda *a: spots.append(a))
        self.assertEqual(len(books), 1)
        self.assertEqual(spots, [])

    def test_binance_unrecognized_message_is_ignored(self):
        calls = []
        handle_binance_message('{"stream":"other","data":{}}', calls.append, calls.append, calls.append)
        self.assertEqual(calls, [])

    def test_binance_url_construction(self):
        self.assertEqual(
            binance_stream_url(BINANCE_WS, "BTCUSDT"),
            "wss://stream.binance.com:9443/stream?streams=btcusdt@aggTrade/btcusdt@depth20@100ms",
        )

    def test_okx_books5_and_trade_callbacks(self):
        trades, books, spots = [], [], []
        book = json.dumps({
            "arg": {"channel": "books5", "instId": "BTC-USDT"},
            "data": [{"bids": [["42000.5", "1", "0", "1"]], "asks": [["42001.5", "2", "0", "1"]]}],
        })
        trade = json.dumps({
            "arg": {"channel": "trades", "instId": "BTC-USDT"},
            "data": [
                {"px": "42000", "sz": "0.1", "side": "sell"},
                {"px": "0", "sz": "1", "side": "buy"},
                "not-a-row",
                {"sz": "0.2"},
            ],
        })
        handle_okx_message(book, trades.append, lambda b, a: books.append((b, a)), lambda *a: spots.append(a), symbol="BTC")
        handle_okx_message(trade, lambda *a: trades.append(a), books.append, lambda *a: spots.append(a), symbol="BTC")
        self.assertEqual(books, [([["42000.5", "1", "0", "1"]], [["42001.5", "2", "0", "1"]])])
        self.assertEqual(spots, [("okx", (42000.5 + 42001.5) / 2)])
        # Zero price, a non-dict row, and a row with no px are dropped.
        self.assertEqual(trades, [(42000.0, 0.1, "sell")])

    def test_okx_missing_side_defaults_to_buy(self):
        trades = []
        raw = json.dumps({"arg": {"channel": "trades"}, "data": [{"px": "3", "sz": "1"}]})
        handle_okx_message(raw, lambda *a: trades.append(a), lambda *a: None, lambda *a: None, symbol="ETH")
        self.assertEqual(trades, [(3.0, 1.0, "buy")])

    def test_okx_subscription_shape(self):
        self.assertEqual(okx_subscription("BTC-USDT"), {
            "op": "subscribe",
            "args": [
                {"channel": "books5", "instId": "BTC-USDT"},
                {"channel": "trades", "instId": "BTC-USDT"},
            ],
        })

    def test_okx_error_event_does_not_trade(self):
        trades = []
        raw = json.dumps({"event": "error", "msg": "bad"})
        handle_okx_message(raw, trades.append, trades.append, trades.append, symbol="SOL")
        self.assertEqual(trades, [])


class FeedLoopTests(unittest.TestCase):
    def test_binance_loop_dispatches_and_returns_on_451(self):
        trades = []
        raw = json.dumps({"stream": "btcusdt@aggTrade", "data": {"p": "1", "q": "2", "m": False}})
        connect = _Connect([
            _Socket([raw]),
            Exception("HTTP 451 from upstream"),
        ])
        sleeps = []

        async def sleep(delay):
            sleeps.append(delay)
            raise AssertionError("451 must not retry")

        asyncio.run(run_binance_feed(
            "BTC", "BTCUSDT", BINANCE_WS, lambda *a: trades.append(a),
            lambda *a: None, lambda *a: None, connect=connect, sleep=sleep,
        ))
        self.assertEqual(trades, [(1.0, 2.0, False)])
        self.assertEqual(connect.calls[0], (
            binance_stream_url(BINANCE_WS, "BTCUSDT"), BINANCE_PING_INTERVAL,
        ))
        self.assertEqual(sleeps, [])

    def test_binance_legal_block_returns_without_retry(self):
        connect = _Connect([Exception("Unavailable for legal reasons")])
        sleeps = []

        async def sleep(delay):
            sleeps.append(delay)

        asyncio.run(run_binance_feed(
            "ETH", "ETHUSDT", BINANCE_WS, lambda *a: None, lambda *a: None, lambda *a: None,
            connect=connect, sleep=sleep,
        ))
        self.assertEqual(sleeps, [])

    def test_binance_retry_delay_is_30_seconds(self):
        connect = _Connect([Exception("socket closed")])
        seen = []

        async def sleep(delay):
            seen.append(delay)
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(run_binance_feed(
                "SOL", "SOLUSDT", BINANCE_WS, lambda *a: None, lambda *a: None, lambda *a: None,
                connect=connect, sleep=sleep,
            ))
        self.assertEqual(seen, [30])

    def test_okx_loop_sends_subscription_and_retries_in_5_seconds(self):
        raw = json.dumps({
            "arg": {"channel": "trades"},
            "data": [{"px": "9", "sz": "1", "side": "buy"}],
        })
        socket = _Socket([raw])
        connect = _Connect([socket, Exception("disconnected")])
        trades = []
        seen = []

        async def sleep(delay):
            seen.append(delay)
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(run_okx_feed(
                "BTC", "BTC-USDT", OKX_WS, lambda *a: trades.append(a),
                lambda *a: None, lambda *a: None, connect=connect, sleep=sleep,
            ))
        self.assertEqual(trades, [(9.0, 1.0, "buy")])
        self.assertEqual(json.loads(socket.sent[0]), okx_subscription("BTC-USDT"))
        self.assertEqual(connect.calls[0], (OKX_WS, OKX_PING_INTERVAL))
        self.assertEqual(seen, [5])


class FeedBoundaryTests(unittest.TestCase):
    def test_feed_modules_do_not_import_capital_infrastructure(self):
        body = textwrap.dedent("""
            import kalshi_bot.data.binance_feed
            import kalshi_bot.data.okx_feed
            forbidden = (
                "kalshi_bot.kalshi_client",
                "kalshi_bot.api_config",
                "kalshi_bot.kalshi_bot",
                "kalshi_bot.safe_live",
                "kalshi_bot.telegram",
                "kalshi_bot.telegram.handlers",
            )
            import sys
            for name in forbidden:
                assert name not in sys.modules, name
        """)
        env = dict(**{k: v for k, v in __import__("os").environ.items() if not k.startswith("KALSHI_")})
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = str(ROOT)
        result = subprocess.run(
            [sys.executable, "-B", "-c", body],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_production_wrappers_delegate_without_a_second_parser(self):
        tree = ast.parse((ROOT / "kalshi_bot" / "kalshi_bot.py").read_text(encoding="utf-8"))
        methods = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name in {"run_binance", "run_okx", "run_price_feed"}
        }
        binance_src = ast.get_source_segment((ROOT / "kalshi_bot" / "kalshi_bot.py").read_text(encoding="utf-8"), methods["run_binance"])
        okx_src = ast.get_source_segment((ROOT / "kalshi_bot" / "kalshi_bot.py").read_text(encoding="utf-8"), methods["run_okx"])
        price_src = ast.get_source_segment((ROOT / "kalshi_bot" / "kalshi_bot.py").read_text(encoding="utf-8"), methods["run_price_feed"])
        self.assertIn("run_binance_feed", binance_src)
        self.assertIn("api_cfg.BINANCE_WS", binance_src)
        self.assertIn("update_trade_binance", binance_src)
        self.assertNotIn("websockets.connect", binance_src)
        self.assertNotIn("json.loads", binance_src)
        self.assertIn("run_okx_feed", okx_src)
        self.assertIn("api_cfg.OKX_WS", okx_src)
        self.assertIn("update_trade_okx", okx_src)
        self.assertNotIn("websockets.connect", okx_src)
        self.assertNotIn("json.loads", okx_src)
        self.assertIn("get_yes_mid", price_src)
        self.assertIn("on_price_update", price_src)


if __name__ == "__main__":
    unittest.main()
