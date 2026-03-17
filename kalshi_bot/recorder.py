"""
recorder.py — Event recording for replay and attribution.

Runs as a background coroutine. Never blocks the main loop.
"""

import asyncio
import json
import time
from pathlib import Path


class EventRecorder:
    """
    Records raw market events to JSONL for later replay and attribution.
    Write is async and fire-and-forget — never blocks the signal path.

    Files written:
        kalshi_events.jsonl   — all raw events (trades, books, Kalshi prices)
        kalshi_features.jsonl — feature snapshots at each decision point
    """

    EVENT_LOG = "logs/kalshi_events.jsonl"
    FEATURE_LOG = "logs/kalshi_features.jsonl"

    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=10000)

    def record(self, event: dict) -> None:
        """Non-blocking. Drops events silently if queue is full."""
        try:
            self._queue.put_nowait({**event, "ts_local": time.time()})
        except asyncio.QueueFull:
            pass

    async def run(self) -> None:
        """Background writer coroutine. Add to asyncio.gather() in orchestrator."""
        while True:
            batch = []
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                batch.append(event)
                # Drain any additional queued events
                while len(batch) < 500:
                    try:
                        batch.append(self._queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
            except asyncio.TimeoutError:
                pass
            if batch:
                for e in batch:
                    path = self.FEATURE_LOG if e.get("_type") == "feature" else self.EVENT_LOG
                    try:
                        Path(path).parent.mkdir(parents=True, exist_ok=True)
                        with open(path, "a", encoding="utf-8") as f:
                            f.write(json.dumps(e) + "\n")
                    except OSError:
                        pass
