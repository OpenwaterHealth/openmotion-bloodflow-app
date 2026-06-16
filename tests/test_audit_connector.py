"""Unit tests for audit-log instrumentation in MotionConnector."""
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
    assert os.path.exists(out)
