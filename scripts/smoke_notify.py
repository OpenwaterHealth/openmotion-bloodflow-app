"""Smoke test for MOTIONConnector.notify slot.

Run from repo root:
    python scripts/smoke_notify.py

Exits 0 on success, non-zero on failure.
"""
import os
import sys
from unittest.mock import MagicMock

# Make the repo root importable regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QCoreApplication
from motion_connector import MOTIONConnector


def main() -> int:
    app = QCoreApplication(sys.argv)  # required for signals
    # Mock the MOTIONInterface — the notify slot doesn't touch hardware.
    iface = MagicMock()
    iface.is_device_connected.return_value = (False, False, False)
    conn = MOTIONConnector(interface=iface, app_config={})

    received = []
    conn.notificationRequested.connect(lambda payload: received.append(payload))

    # Call with full args
    conn.notify("Hello", "success", 5000, True)
    # Call with defaults (only required arg is text)
    conn.notify("Default")

    if len(received) != 2:
        print(f"FAIL: expected 2 emissions, got {len(received)}")
        return 1

    a, b = received
    if a != {"text": "Hello", "type": "success", "durationMs": 5000, "dismissible": True}:
        print(f"FAIL: first payload wrong: {a}")
        return 1
    if b != {"text": "Default", "type": "info", "durationMs": 4000, "dismissible": True}:
        print(f"FAIL: second payload wrong (defaults): {b}")
        return 1

    print("OK: notify slot emits notificationRequested with correct payload")
    return 0


if __name__ == "__main__":
    sys.exit(main())
