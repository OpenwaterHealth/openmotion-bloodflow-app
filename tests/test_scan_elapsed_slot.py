"""Unit test for the QML-facing scanElapsedStr slot (no hardware)."""
from motion_connector import MotionConnector


def test_scan_elapsed_str_slot_delegates(monkeypatch):
    # Build the object without running __init__ (avoids hardware/Qt setup).
    conn = MotionConnector.__new__(MotionConnector)
    monkeypatch.setattr(conn, "_scan_elapsed_str", lambda: "01:02:03")
    assert conn.scanElapsedStr() == "01:02:03"
