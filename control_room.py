"""Streamlit entry for the Kalshi Control Room.

This file lives at the repository root on purpose. Streamlit auto-runs a
``pages/`` directory next to the entry script. ``dashboard/pages/`` must not
be that directory, or ``/ops`` and the other legacy paths execute Live
controls before the acknowledgement gate.
"""
from dashboard.control_room import main

if __name__ == "__main__":
    main()
