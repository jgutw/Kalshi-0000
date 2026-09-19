"""Append-only, rollover-only research forecast persistence.

This store intentionally keeps a candidate only in memory until its window
closes. It is not an operational log and is never rotated.
"""

from __future__ import annotations

import json
import logging
import math
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .session_meta import current_session_tag


log = logging.getLogger("research_store")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PMARKET_SEMANTICS = "decision-row p_market; WAIT-row values may be raw YES while C7 BUY-entry p_market may be smoothed"


def _finite(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _git_sha(root: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
            text=True, timeout=2, check=False,
        )
        return result.stdout.strip() or None if result.returncode == 0 else None
    except Exception:
        return None


class ResearchForecastStore:
    """Shared, best-effort permanent store keyed by asset and window boundary."""

    def __init__(
        self,
        root: Path | str = PROJECT_ROOT,
        *,
        dry_run: bool = True,
        config_profile: Optional[str] = None,
        git_sha_provider: Optional[Callable[[Path], Optional[str]]] = None,
        session_tag_provider: Optional[Callable[[], str]] = None,
    ) -> None:
        self.root = Path(root)
        self.mode = "paper" if dry_run else "live"
        self.dry_run = dry_run
        self.config_profile = config_profile
        try:
            self.git_sha = (git_sha_provider or _git_sha)(self.root)
        except Exception:
            self.git_sha = None
        self.session_tag_provider = session_tag_provider or current_session_tag
        self._candidates: dict[tuple[str, int], dict] = {}
        self._frozen: set[tuple[str, int]] = set()
        self._persisted: dict[str, set[tuple[str, int]]] = {}
        self._lock = threading.Lock()

    def observe(self, rec: dict) -> None:
        """Remember the last qualifying >=60s record; never write on this path."""
        try:
            key = self._key(rec.get("asset"), rec.get("window_id_ts"))
            if key is None:
                return
            remaining = _finite(rec.get("time_remaining"))
            with self._lock:
                if remaining is not None and remaining < 60:
                    self._frozen.add(key)
                    return
                if key in self._frozen or remaining is None or remaining < 60:
                    return
                if _finite(rec.get("p_real")) is None:
                    return
                self._candidates[key] = dict(rec)
        except Exception as exc:
            log.warning("Research observe skipped: %s", exc)

    def finalize(
        self,
        *,
        asset: str,
        closed_window_id_ts: Any,
        actual_outcome: Any,
        exit_spot: Any,
        close_time_utc: Optional[str],
    ) -> None:
        """Append one completed row if a qualified candidate exists; never raise."""
        key = self._key(asset, closed_window_id_ts)
        if key is None:
            return
        try:
            with self._lock:
                candidate = self._candidates.get(key)
                if candidate is None or str(actual_outcome).upper() not in {"YES", "NO"}:
                    return
                path, partition = self._path_for(key[1])
                persisted = self._keys_for(path, partition)
                if key in persisted:
                    log.warning("Research duplicate skipped: %s", key)
                    return
                row = self._row(candidate, key, str(actual_outcome).upper(), exit_spot, close_time_utc)
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
                    persisted.add(key)
                except OSError as exc:
                    log.warning("Research write failed for %s: %s", key, exc)
        except Exception as exc:
            log.warning("Research finalize skipped for %s: %s", key, exc)
        finally:
            with self._lock:
                self._candidates.pop(key, None)
                self._frozen.discard(key)

    def _row(self, rec: dict, key: tuple[str, int], outcome: str, exit_spot: Any, close_time_utc: Optional[str]) -> dict:
        try:
            session_tag = self.session_tag_provider()
        except Exception:
            session_tag = None
        fields = (
            "ticker", "window_id", "ts", "time_remaining", "price_to_beat", "price_to_beat_source",
            "spot_now", "spot_start", "p_base", "p_real", "alpha_micro", "yes_price_raw", "p_market",
            "lag_signal", "lag_confidence", "response_gap", "response_beta", "per_venue_mids",
            "per_venue_staleness", "raw_features", "reason", "strategy", "action",
        )
        row = {field: rec.get(field) for field in fields}
        row.update({
            "schema_version": 1, "asset": key[0], "window_id_ts": key[1],
            "snapshot_ts": rec.get("ts"), "dry_run": self.dry_run,
            "session_tag": session_tag, "git_sha": self.git_sha,
            "regime": rec.get("regime") if rec.get("regime") is not None else self.config_profile,
            "p_market_semantics": rec.get("p_market_semantics") or PMARKET_SEMANTICS,
            "actual_outcome": outcome, "yes_settled": 1 if outcome == "YES" else 0,
            "outcome_ts": datetime.now(timezone.utc).isoformat(), "exit_spot": exit_spot,
            "close_time_utc": close_time_utc, "outcome_source": "spot_vs_price_to_beat",
        })
        row.pop("ts", None)
        return row

    def _path_for(self, window_id_ts: int) -> tuple[Path, str]:
        partition = datetime.fromtimestamp(window_id_ts, tz=timezone.utc).date().isoformat()
        return self.root / "research_data" / self.mode / f"{partition}.jsonl", partition

    def _keys_for(self, path: Path, partition: str) -> set[tuple[str, int]]:
        if partition in self._persisted:
            return self._persisted[partition]
        keys: set[tuple[str, int]] = set()
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            row = json.loads(line)
                            key = self._key(row.get("asset"), row.get("window_id_ts"))
                            if key is not None:
                                keys.add(key)
                        except (json.JSONDecodeError, TypeError, AttributeError):
                            log.warning("Malformed research JSONL ignored in %s", path)
            except OSError as exc:
                log.warning("Research duplicate index unavailable for %s: %s", path, exc)
        self._persisted[partition] = keys
        return keys

    @staticmethod
    def _key(asset: Any, window_id_ts: Any) -> Optional[tuple[str, int]]:
        timestamp = _finite(window_id_ts)
        if not asset or timestamp is None:
            return None
        return str(asset), int(timestamp)
