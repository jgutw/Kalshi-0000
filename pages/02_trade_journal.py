"""Trade Journal page — loads from dashboard.pages."""
import runpy
from pathlib import Path

_path = Path(__file__).resolve().parent.parent / "dashboard" / "pages" / "02_trade_journal.py"
runpy.run_path(str(_path), run_name="__main__")
