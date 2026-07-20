"""Connector glue for critical errors: signal payload + bug-report routing."""

from unittest.mock import MagicMock

import pytest

import error_codes
from motion_connector import MotionConnector

pytestmark = pytest.mark.unit


def _connector(tmp_path, app_config=None, connected=(True, True, True)):
    iface = MagicMock()
    iface.is_device_connected.return_value = connected
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = None
    cfg = {"engineeringMode": False}
    if app_config:
        cfg.update(app_config)
    return MotionConnector(
        interface=iface, app_config=cfg,
        data_dir=str(tmp_path), config_dir="config",
    )


_FULL_SMTP = {
    "host": "smtp.example.com", "port": 587, "username": "u",
    "password": "p", "from_addr": "app@example.com",
}


def test_raise_critical_emits_registry_payload(tmp_path):
    conn = _connector(tmp_path)
    received = []
    conn.criticalErrorRaised.connect(lambda *a: received.append(a))

    conn._raise_critical("E-101", detail="mux missing")

    assert len(received) == 1
    code, title, message, action, detail = received[0]
    entry = error_codes.lookup("E-101")
    assert code == "E-101"
    assert title == entry.title
    assert message == entry.message
    assert action == entry.suggested_action
    assert detail == "mux missing"


def test_raise_critical_unknown_code_still_emits(tmp_path):
    conn = _connector(tmp_path)
    received = []
    conn.criticalErrorRaised.connect(lambda *a: received.append(a))

    conn._raise_critical("E-777")

    assert len(received) == 1
    assert received[0][0] == "E-777"
    assert received[0][1]  # generic title, non-empty


def _healthy():
    return {"mux": True, "imu": True, "cameras": [True] * 8,
            "fpgas": [True] * 8, "all_present": True}


def test_i2c_health_all_present_raises_nothing(tmp_path):
    conn = _connector(tmp_path)
    conn._interface.left.is_connected.return_value = True
    conn._interface.left.i2c_health = _healthy()
    received = []
    conn.criticalErrorRaised.connect(lambda *a: received.append(a))

    conn._check_sensor_i2c_health("left")

    assert received == []


def test_i2c_health_missing_device_raises_e101_with_detail(tmp_path):
    conn = _connector(tmp_path)
    conn._interface.left.is_connected.return_value = True
    snap = _healthy()
    snap["cameras"] = [True, True, False, True, True, True, True, True]
    snap["imu"] = False
    snap["all_present"] = False
    conn._interface.left.i2c_health = snap
    received = []
    conn.criticalErrorRaised.connect(lambda *a: received.append(a))

    conn._check_sensor_i2c_health("left")

    assert len(received) == 1
    code, _title, _msg, _action, detail = received[0]
    assert code == "E-101"
    assert "imu" in detail
    assert "2" in detail  # camera index 2 missing


def test_i2c_health_none_raises_e102(tmp_path):
    conn = _connector(tmp_path)
    conn._interface.left.is_connected.return_value = True
    conn._interface.left.i2c_health = None
    received = []
    conn.criticalErrorRaised.connect(lambda *a: received.append(a))

    conn._check_sensor_i2c_health("left")

    assert len(received) == 1
    assert received[0][0] == "E-102"


def _notifs(conn):
    """Capture toast payloads emitted via notificationRequested."""
    out = []
    conn.notificationRequested.connect(lambda p: out.append(p))
    return out


def test_connector_connection_timeout_defaults_to_eight_seconds(tmp_path):
    """Guards the fallback at motion_connector.py's
    `cfg.get("connectionTimeoutSec", 8)`: this applies whenever a caller
    constructs MotionConnector with a partial config dict that omits the
    key — including `_connector()` here, whose default app_config doesn't
    set it. Every other watchdog test calls `_check_connection_watchdog()`
    directly and never reads `_connection_timeout_sec`, so a regression of
    this fallback back to 30 would pass all of them silently."""
    conn = _connector(tmp_path)

    assert conn._connection_timeout_sec == 8


def test_watchdog_all_connected_no_notification(tmp_path):
    conn = _connector(tmp_path, connected=(True, True, True))
    notifs = _notifs(conn)

    conn._check_connection_watchdog()

    assert notifs == []


def test_watchdog_is_a_warning_not_a_critical_modal(tmp_path):
    """E-104/E-106 are downgraded: a yellow warning toast, never the modal."""
    conn = _connector(tmp_path, connected=(False, False, False))
    crit = []
    conn.criticalErrorRaised.connect(lambda *a: crit.append(a))
    notifs = _notifs(conn)

    conn._check_connection_watchdog()

    assert crit == []
    assert all(n["type"] == "warning" for n in notifs)


def test_watchdog_both_missing_shows_single_system_not_found(tmp_path):
    conn = _connector(tmp_path, connected=(False, False, False))
    notifs = _notifs(conn)

    conn._check_connection_watchdog()

    assert len(notifs) == 1
    assert notifs[0]["type"] == "warning"
    assert "System not found" in notifs[0]["text"]


def test_watchdog_console_only_missing_warns(tmp_path):
    conn = _connector(tmp_path, connected=(False, True, False))
    notifs = _notifs(conn)

    conn._check_connection_watchdog()

    assert len(notifs) == 1
    assert notifs[0]["type"] == "warning"
    assert "Console" in notifs[0]["text"]
    assert "System not found" not in notifs[0]["text"]


def test_watchdog_sensor_only_missing_warns(tmp_path):
    conn = _connector(tmp_path, connected=(True, False, False))
    notifs = _notifs(conn)

    conn._check_connection_watchdog()

    assert len(notifs) == 1
    assert notifs[0]["type"] == "warning"
    assert "Sensor" in notifs[0]["text"]


def test_watchdog_toast_auto_dismisses_after_10s(tmp_path):
    """The connection warning auto-dismisses after 10 s (#314) rather than
    staying sticky, so it doesn't nag while the user explores the no-device
    sample scan. Still user-dismissible via the close button."""
    conn = _connector(tmp_path, connected=(False, False, False))
    notifs = _notifs(conn)

    conn._check_connection_watchdog()

    assert len(notifs) == 1
    assert notifs[0]["durationMs"] == 10000
    assert notifs[0]["dismissible"] is True


def test_watchdog_respects_min_sensors_config(tmp_path):
    conn = _connector(tmp_path, app_config={"minSensors": 2},
                      connected=(True, True, False))  # only one sensor
    notifs = _notifs(conn)

    conn._check_connection_watchdog()

    assert len(notifs) == 1
    assert notifs[0]["type"] == "warning"
    assert "Sensor" in notifs[0]["text"]


def test_send_bug_report_selects_smtp_when_configured(tmp_path, monkeypatch):
    conn = _connector(tmp_path, app_config={"bug_report_smtp": _FULL_SMTP})
    chosen = []
    monkeypatch.setattr(conn, "_send_bug_report_smtp",
                        lambda *a, **k: chosen.append("smtp"))
    monkeypatch.setattr(conn, "_send_bug_report_fallback",
                        lambda *a, **k: chosen.append("fallback"))

    conn.sendBugReport("E-101")

    assert chosen == ["smtp"]


def test_send_bug_report_falls_back_without_smtp(tmp_path, monkeypatch):
    conn = _connector(tmp_path)  # no bug_report_smtp configured
    chosen = []
    monkeypatch.setattr(conn, "_send_bug_report_smtp",
                        lambda *a, **k: chosen.append("smtp"))
    monkeypatch.setattr(conn, "_send_bug_report_fallback",
                        lambda *a, **k: chosen.append("fallback"))

    conn.sendBugReport("E-101")

    assert chosen == ["fallback"]
