"""Telegram bridge: alerts + remote control."""

# Keep this package lightweight — do not import bridge/handlers here
# (avoids circular imports with safe_live / rounds).

__all__ = ["main", "run_bridge"]


def __getattr__(name: str):
    if name in ("main", "run_bridge"):
        from .bridge import main, run_bridge

        return main if name == "main" else run_bridge
    raise AttributeError(name)
