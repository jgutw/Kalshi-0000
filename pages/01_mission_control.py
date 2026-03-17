"""Mission Control page — loads from dashboard.pages."""
import runpy
from pathlib import Path

_path = Path(__file__).resolve().parent.parent / "dashboard" / "pages" / "01_mission_control.py"
runpy.run_path(str(_path), run_name="__main__")
