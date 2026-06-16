"""Connector glue for critical errors: signal payload + bug-report routing."""

from unittest.mock import MagicMock

import pytest

import error_codes
from motion_connector import MotionConnector

pytestmark = pytest.mark.unit


def _connector(tmp_path, app_config=None):
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = None
    cfg = {"developerMode": False}
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
