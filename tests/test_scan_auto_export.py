"""Scan-end CSV auto-export (#598).

The Research-only Settings switch ``autoExportCsv`` writes the same file as
History → Export CSV (``<label>_export.csv``, corrected per-cam format) into
``data/`` when each scan ends. It is a PREFERENCE key, so it persists in the
scans.db settings table across launches, and the scan-end gate re-checks
``clinicalMode`` so a clinical build never exports.
"""

import os
from unittest.mock import MagicMock

import pytest

from config import app_config
from motion_connector import MotionConnector
from omotion.ScanDatabase import ScanDatabase
from utils.settings_store import SettingsStore

pytestmark = pytest.mark.unit


def _connector(tmp_path, scan_db_path=None, settings_store=None,
               baseline_config=None, **cfg):
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = scan_db_path  # MagicMock default would be truthy
    config = {"engineeringMode": False, "clinicalMode": False}
    config.update(cfg)
    return MotionConnector(
        interface=iface, app_config=config,
        data_dir=str(tmp_path), config_dir="config",
        settings_store=settings_store, baseline_config=baseline_config,
    )


def _session(db_path, label):
    db = ScanDatabase(db_path=db_path)
    sid = db.create_session(session_label=label, session_start=1.0,
                            session_notes=None, session_meta={})
    db.close()
    return sid


@pytest.fixture
def materialize(monkeypatch):
    """Replace the heavy materialize step: record (sid, path) and write a
    stub file so the rename step has something to move."""
    calls = []
    import omotion.SessionPlayback as sp

    def _fake(db_path, sid, out_path, **kw):
        calls.append((sid, out_path, kw))
        with open(out_path, "w") as fh:
            fh.write("stub\n")
        return out_path

    monkeypatch.setattr(sp, "materialize_corrected_csv", _fake)
    return calls


def test_key_is_a_persisted_preference_defaulting_off():
    assert app_config.APP_CONFIG["autoExportCsv"] is False
    assert app_config.tier_of("autoExportCsv") == app_config.PREFERENCE
    assert "autoExportCsv" in app_config.PERSISTED_KEYS


def test_toggle_survives_relaunch_via_settings_table(tmp_path):
    path = tmp_path / "scans.db"
    c = _connector(tmp_path, settings_store=SettingsStore(path),
                   baseline_config=app_config.compiled_config())
    c.saveConfigs({"autoExportCsv": True})
    assert SettingsStore(path).load().get("autoExportCsv") is True
    # Back at the compiled default → the row is dropped, not stored False.
    c.saveConfigs({"autoExportCsv": False})
    assert "autoExportCsv" not in SettingsStore(path).load()


def test_exports_finished_scan_into_data_dir(tmp_path, materialize):
    db_path = str(tmp_path / "scans.db")
    sid = _session(db_path, "20260923_120000_S1")
    c = _connector(tmp_path, scan_db_path=db_path, autoExportCsv=True)

    t = c._maybe_auto_export_scan_csv("20260923_120000_S1")
    assert t is not None
    t.join(5)

    expected = os.path.join(c._data_root, "20260923_120000_S1_export.csv")
    assert len(materialize) == 1
    assert materialize[0][0] == sid
    assert materialize[0][2] == {"include_quality": True}  # same as History
    assert os.path.isfile(expected)
    assert not os.path.exists(expected + ".partial")


@pytest.mark.parametrize("cfg", [
    {"autoExportCsv": False},
    # A persisted true must never export on a clinical build, which has no
    # switch to turn it off — engineering unlock included.
    {"autoExportCsv": True, "clinicalMode": True},
    {"autoExportCsv": True, "clinicalMode": True, "engineeringMode": True},
])
def test_no_export_when_disabled_or_clinical(tmp_path, materialize, cfg):
    db_path = str(tmp_path / "scans.db")
    _session(db_path, "scanA")
    c = _connector(tmp_path, scan_db_path=db_path, **cfg)
    assert c._maybe_auto_export_scan_csv("scanA") is None
    assert materialize == []


def test_no_export_without_session_label(tmp_path, materialize):
    c = _connector(tmp_path, scan_db_path=str(tmp_path / "scans.db"),
                   autoExportCsv=True)
    assert c._maybe_auto_export_scan_csv("") is None


def test_empty_scan_is_skipped_silently(tmp_path, materialize):
    """ScanDBSink deletes the row of a scan that saved nothing; the
    scan-outcome toast already reports it, so no export and no toast."""
    db_path = str(tmp_path / "scans.db")
    _session(db_path, "other")
    c = _connector(tmp_path, scan_db_path=db_path, autoExportCsv=True)
    ok, detail = c._auto_export_scan_csv_sync(
        "gone", os.path.join(c._data_root, "gone_export.csv"))
    assert (ok, detail) == (False, "")
    assert materialize == []


def test_failure_leaves_no_partial_file(tmp_path, monkeypatch):
    db_path = str(tmp_path / "scans.db")
    _session(db_path, "scanA")
    c = _connector(tmp_path, scan_db_path=db_path, autoExportCsv=True)
    import omotion.SessionPlayback as sp

    def _boom(db_path, sid, out_path, **kw):
        with open(out_path, "w") as fh:
            fh.write("half a file")
        raise RuntimeError("disk full")

    monkeypatch.setattr(sp, "materialize_corrected_csv", _boom)
    out = os.path.join(c._data_root, "scanA_export.csv")
    ok, detail = c._auto_export_scan_csv_sync("scanA", out)
    assert ok is False and "disk full" in detail
    assert not os.path.exists(out)
    assert not os.path.exists(out + ".partial")


def test_no_scan_db_reports_failure(tmp_path):
    c = _connector(tmp_path, scan_db_path=None, autoExportCsv=True)
    ok, detail = c._auto_export_scan_csv_sync(
        "scanA", str(tmp_path / "scanA_export.csv"))
    assert ok is False and detail
