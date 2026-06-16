"""Unit tests for audit-log instrumentation in MotionConnector."""
import json
from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector

pytestmark = pytest.mark.unit


def _connector(tmp_path, scan_db_path=None, app_config=None):
    iface = MagicMock()
    iface.is_device_connected.return_value = (False, False, False)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = scan_db_path
    iface.get_sdk_version.return_value = "9.9.9"
    return MotionConnector(
        interface=iface,
        app_config=app_config or {"developerMode": False},
        data_dir=str(tmp_path),
        config_dir="config",
    )


def _types(c):
    return [e["event_type"] for e in c.auditLogEntries()]


def test_construct_logs_startup_and_system_info(tmp_path):
    c = _connector(tmp_path, scan_db_path=str(tmp_path / "scans.db"))
    t = _types(c)
    assert "system_startup" in t
    assert "system_info" in t


def test_shutdown_logs_system_shutdown(tmp_path):
    # shutdown() also closes the audit handle, so read the committed
    # rows straight from the DB file rather than via the live connector.
    import sqlite3
    db = str(tmp_path / "scans.db")
    c = _connector(tmp_path, scan_db_path=db)
    c.shutdown()
    conn = sqlite3.connect(db)
    types = [r[0] for r in conn.execute("SELECT event_type FROM logs")]
    conn.close()
    assert "system_shutdown" in types


def test_record_viewed_and_entries(tmp_path):
    c = _connector(tmp_path, scan_db_path=str(tmp_path / "scans.db"))
    c.recordAuditLogViewed()
    assert "audit_log_viewed" in _types(c)


def test_export_csv_writes_file_and_logs_export(tmp_path):
    import os
    c = _connector(tmp_path, scan_db_path=str(tmp_path / "scans.db"))
    dest = str(tmp_path / "audit.csv")
    out = c.exportAuditLogCsv(dest)
    assert out == dest
    assert os.path.exists(dest)
    assert "audit_log_exported" in _types(c)


def test_export_csv_strips_file_url(tmp_path):
    import os
    c = _connector(tmp_path, scan_db_path=str(tmp_path / "scans.db"))
    dest = str(tmp_path / "audit2.csv")
    out = c.exportAuditLogCsv("file:///" + dest.replace("\\", "/"))
    assert out                     # returned path must be non-empty
    assert os.path.exists(out)


def test_no_db_path_noop(tmp_path):
    # With no scan_db_path the audit log is a no-op; the connector must
    # still construct and the slots must return gracefully (no raise).
    import os
    c = _connector(tmp_path, scan_db_path=None)
    assert c.auditLogEntries() == []
    c.recordAuditLogViewed()       # must not raise
    dest = str(tmp_path / "out.csv")
    c.exportAuditLogCsv(dest)      # no raise; nothing written when disabled
    assert not os.path.exists(dest)


def test_disconnect_logs_device_disconnected(tmp_path):
    from omotion import ConnectionState
    c = _connector(tmp_path, scan_db_path=str(tmp_path / "scans.db"))
    handle = MagicMock()
    handle.name = "left"
    c._on_handle_state_changed_impl(
        handle, ConnectionState.CONNECTED, ConnectionState.DISCONNECTED,
        "unplugged",
    )
    assert "device_disconnected" in _types(c)


def test_connect_logs_device_connected_and_stats(tmp_path):
    from omotion import ConnectionState
    c = _connector(tmp_path, scan_db_path=str(tmp_path / "scans.db"))
    handle = MagicMock()
    handle.name = "console"
    handle.get_hardware_id.return_value = "DEADBEEF"
    handle.get_version.return_value = "1.0.0"
    c._on_handle_state_changed_impl(
        handle, ConnectionState.DISCONNECTED, ConnectionState.CONNECTED,
        "found",
    )
    t = _types(c)
    assert "device_connected" in t
    assert "device_stats" in t


def test_run_calibration_logs_started(tmp_path):
    c = _connector(tmp_path, scan_db_path=str(tmp_path / "scans.db"))
    c._consoleConnected = True
    c._leftSensorConnected = True
    c._interface.start_calibration.return_value = True
    c.runCalibration("left")
    ev = [e for e in c.auditLogEntries()
          if e["event_type"] == "calibration_started"]
    assert ev
    assert json.loads(ev[0]["details"])["target"] == "left"


def test_calibration_complete_logs_ended(tmp_path):
    c = _connector(tmp_path, scan_db_path=str(tmp_path / "scans.db"))
    c._calibration_t0 = None
    result = MagicMock()
    result.canceled = False
    result.ok = True
    result.passed = True
    result.csv_path = "cal.csv"
    c._on_calibration_complete(result)
    ev = [e for e in c.auditLogEntries()
          if e["event_type"] == "calibration_ended"]
    assert ev
    assert json.loads(ev[0]["details"])["outcome"] == "passed"
