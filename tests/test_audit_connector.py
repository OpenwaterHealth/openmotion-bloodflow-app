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
    # _log_device_stats resolves the handle via getattr(self._interface,
    # name) — in production that IS the same object the callback receives.
    # Wire the mock interface so the test exercises that real resolution
    # and can assert the captured IDs.
    c._interface.console = handle
    c._on_handle_state_changed_impl(
        handle, ConnectionState.DISCONNECTED, ConnectionState.CONNECTED,
        "found",
    )
    t = _types(c)
    assert "device_connected" in t
    assert "device_stats" in t
    stats = [e for e in c.auditLogEntries()
             if e["event_type"] == "device_stats"]
    assert stats
    d = json.loads(stats[0]["details"])
    assert d["hardware_id"] == "DEADBEEF"
    assert d["firmware_version"] == "1.0.0"


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


def test_set_config_logs_settings_changed(tmp_path):
    c = _connector(tmp_path, scan_db_path=str(tmp_path / "scans.db"))
    c._save_app_config = lambda: None      # don't touch the real config
    c.setConfig("plotWindowSec", 30)
    ev = [e for e in c.auditLogEntries()
          if e["event_type"] == "settings_changed"]
    assert ev
    d = json.loads(ev[0]["details"])
    assert d["changes"]["plotWindowSec"]["new"] == 30


def test_save_configs_logs_only_changed_keys(tmp_path):
    c = _connector(tmp_path, scan_db_path=str(tmp_path / "scans.db"))
    c._save_app_config = lambda: None
    c._app_config["plotWindowSec"] = 15
    c.saveConfigs({"plotWindowSec": 15, "bfiMax": 12.0})
    ev = [e for e in c.auditLogEntries()
          if e["event_type"] == "settings_changed"]
    assert ev
    d = json.loads(ev[-1]["details"])["changes"]
    assert "bfiMax" in d
    assert "plotWindowSec" not in d


def test_delete_scans_logs_scan_deleted(tmp_path):
    from omotion.ScanDatabase import ScanDatabase
    db_path = str(tmp_path / "scans.db")
    db = ScanDatabase(db_path=db_path)
    sid = db.create_session(session_label="L1", session_start=1.0,
                            session_meta={})
    db.close()
    c = _connector(tmp_path, scan_db_path=db_path)
    c.deleteScans([sid])
    ev = [e for e in c.auditLogEntries()
          if e["event_type"] == "scan_deleted"]
    assert ev
    assert json.loads(ev[0]["details"])["count"] == 1


def test_load_past_scan_logs_scan_viewed(tmp_path):
    db_path = str(tmp_path / "scans.db")
    c = _connector(tmp_path, scan_db_path=db_path)
    c.loadPastScan("20260101_000000_owABC")
    ev = [e for e in c.auditLogEntries()
          if e["event_type"] == "scan_viewed"]
    assert ev
    assert json.loads(ev[0]["details"])["label"] == "20260101_000000_owABC"


def test_prepare_debug_bundle_creates_zip_and_logs(tmp_path):
    import os
    import zipfile
    db = str(tmp_path / "scans.db")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "ow-bloodflowapp-x.log").write_text("hello", encoding="utf-8")
    c = _connector(tmp_path, scan_db_path=db)
    # Don't spawn a real file-explorer process during the test.
    c._reveal_in_explorer = lambda p: None
    path = c.prepareDebugLogBundle()
    assert path and os.path.exists(path)
    assert path.endswith(".zip")
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
    assert any(n.startswith("logs/") for n in names)
    assert "system_info.txt" in names
    assert "debug_bundle_created" in _types(c)


def test_prepare_debug_bundle_failure_returns_empty(tmp_path, monkeypatch):
    # If the bundle build raises, the slot must return "" and NOT log a
    # debug_bundle_created event (fail-soft, mirrors the other slots).
    import debug_bundle
    c = _connector(tmp_path, scan_db_path=str(tmp_path / "scans.db"))
    c._reveal_in_explorer = lambda p: None

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(debug_bundle, "build_debug_bundle", boom)
    assert c.prepareDebugLogBundle() == ""
    assert "debug_bundle_created" not in _types(c)
