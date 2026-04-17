"""Smoke test for MOTIONConnector notification slots & signals.

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


def _make_connector():
    iface = MagicMock()
    iface.is_device_connected.return_value = (False, False, False)
    return MOTIONConnector(interface=iface, app_config={})


def fail(msg: str) -> int:
    print(f"FAIL: {msg}")
    return 1


def main() -> int:
    app = QCoreApplication(sys.argv)  # required for signals
    conn = _make_connector()

    # ── notify: payload, defaults, return value, monotonic ids ─────────
    received = []
    conn.notificationRequested.connect(lambda p: received.append(p))

    id1 = conn.notify("Hello", "success", 5000, True)
    id2 = conn.notify("Default")  # all defaults
    id3 = conn.notify("Tagged", "info", 0, False, "connection-status")

    if not (isinstance(id1, int) and isinstance(id2, int) and isinstance(id3, int)):
        return fail(f"notify did not return ints: {id1!r}, {id2!r}, {id3!r}")
    if not (id1 < id2 < id3):
        return fail(f"notify ids should be monotonically increasing: {id1}, {id2}, {id3}")

    if len(received) != 3:
        return fail(f"expected 3 notify emissions, got {len(received)}")

    expected_first = {
        "id": id1, "tag": "", "text": "Hello", "type": "success",
        "durationMs": 5000, "dismissible": True,
    }
    if received[0] != expected_first:
        return fail(f"first payload wrong: {received[0]}")

    expected_default = {
        "id": id2, "tag": "", "text": "Default", "type": "info",
        "durationMs": 4000, "dismissible": True,
    }
    if received[1] != expected_default:
        return fail(f"defaults payload wrong: {received[1]}")

    expected_tagged = {
        "id": id3, "tag": "connection-status", "text": "Tagged", "type": "info",
        "durationMs": 0, "dismissible": False,
    }
    if received[2] != expected_tagged:
        return fail(f"tagged payload wrong: {received[2]}")

    # ── dismissNotification(int) → emits by-id signal ─────────────────
    by_id = []
    conn.notificationDismissByIdRequested.connect(lambda v: by_id.append(v))
    conn.dismissNotification(id1)
    conn.dismissNotification(42)  # unknown id is fine — slot just emits
    if by_id != [id1, 42]:
        return fail(f"dismissNotification(int) emissions wrong: {by_id}")

    # ── dismissNotification(str) → emits by-tag signal ────────────────
    by_tag = []
    conn.notificationDismissByTagRequested.connect(lambda v: by_tag.append(v))
    conn.dismissNotification("connection-status")
    conn.dismissNotification("nope")
    if by_tag != ["connection-status", "nope"]:
        return fail(f"dismissNotification(str) emissions wrong: {by_tag}")

    # ── dismissNotification(bool) is rejected (bool subclasses int) ───
    by_id_before = list(by_id)
    by_tag_before = list(by_tag)
    conn.dismissNotification(True)
    if by_id != by_id_before or by_tag != by_tag_before:
        return fail("dismissNotification(bool) should be rejected with a warning, not emit")

    # ── dismissAllNotifications → emits the dismiss-all signal ────────
    all_calls = []
    conn.notificationDismissAllRequested.connect(lambda: all_calls.append(True))
    conn.dismissAllNotifications()
    conn.dismissAllNotifications()
    if all_calls != [True, True]:
        return fail(f"dismissAllNotifications emissions wrong: {all_calls}")

    # ── unknown type falls back to 'info' ─────────────────────────────
    received_after = []
    conn.notificationRequested.connect(lambda p: received_after.append(p))
    conn.notify("Bogus", "purple")
    if not received_after or received_after[-1]["type"] != "info":
        return fail(f"unknown type should fall back to info: {received_after}")

    print("OK: notify + dismiss APIs all behave correctly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
