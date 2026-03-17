"""
dashboard/data/file_watcher.py — Track file mtimes to avoid re-parsing unchanged files.
"""

from __future__ import annotations

import os
from typing import Dict


class FileWatcher:
    """Tracks last-modified timestamps for log files."""

    def __init__(self):
        self._mtimes: Dict[str, float] = {}

    def has_changed(self, path: str) -> bool:
        """Returns True if the file's mtime differs from cached value."""
        try:
            mtime = os.path.getmtime(path)
        except (OSError, FileNotFoundError):
            return True
        cached = self._mtimes.get(path)
        if cached is None:
            return True
        return mtime != cached

    def refresh(self, paths: list) -> None:
        """Updates cached mtimes for all given paths."""
        for path in paths:
            try:
                self._mtimes[path] = os.path.getmtime(path)
            except (OSError, FileNotFoundError):
                self._mtimes[path] = 0.0
