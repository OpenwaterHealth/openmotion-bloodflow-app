"""exportScansToFolder — batch-export several scans into one folder."""

import os
from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector
from omotion.ScanDatabase import ScanDatabase

pytestmark = pytest.mark.unit


def _connector(tmp_path, scan_db_path=None):
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = scan_db_path  # MagicMock default would be truthy
    return MotionConnector(
        interface=iface, app_config={"engineeringMode": False},
        data_dir=str(tmp_path), config_dir="config",
    )


def _session(db_path, label, start):
    db = ScanDatabase(db_path=db_path)
    db.create_session(session_label=label, session_start=start,
                      session_notes=None, session_meta={})
    db.close()


def test_export_scans_to_folder_writes_each_and_skips_missing(
        tmp_path, monkeypatch):
    db_path = str(tmp_path / "scans.db")
    _session(db_path, "scanA", 1.0)
    _session(db_path, "scanB", 2.0)
    c = _connector(tmp_path, scan_db_path=db_path)

    # Mock the heavy materialize step; capture its output paths instead.
    calls = []
    import omotion.SessionPlayback as sp
    monkeypatch.setattr(
        sp, "materialize_corrected_csv",
        lambda *a, **k: calls.append(a[2]),  # a[2] == output_path
    )

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    res = c.exportScansToFolder(
        ["scanA", "scanB", "missing"], str(out_dir))

    assert res["exported"] == 2
    assert res["skipped"] == 1            # 'missing' has no DB session
    assert len(calls) == 2
    names = sorted(os.path.basename(p) for p in calls)
    assert names == ["scanA_export.csv", "scanB_export.csv"]


def test_export_scans_to_folder_isolates_per_label_failure(
        tmp_path, monkeypatch):
    """A scan whose materialize raises is counted skipped, and the batch
    keeps going for the remaining scans."""
    db_path = str(tmp_path / "scans.db")
    _session(db_path, "scanA", 1.0)
    _session(db_path, "scanB", 2.0)
    c = _connector(tmp_path, scan_db_path=db_path)

    exported = []
    import omotion.SessionPlayback as sp

    def _materialize(*a, **k):
        # a[2] == output_path, ends with "<label>_export.csv"
        if os.path.basename(a[2]) == "scanA_export.csv":
            raise RuntimeError("boom")
        exported.append(a[2])

    monkeypatch.setattr(sp, "materialize_corrected_csv", _materialize)

    res = c.exportScansToFolder(["scanA", "scanB"], str(tmp_path))

    assert res == {"exported": 1, "skipped": 1}
    assert len(exported) == 1  # scanB still ran after scanA failed
    assert os.path.basename(exported[0]) == "scanB_export.csv"


def test_export_scans_to_folder_empty_inputs(tmp_path):
    c = _connector(tmp_path, scan_db_path=str(tmp_path / "scans.db"))
    assert c.exportScansToFolder([], str(tmp_path)) == {
        "exported": 0, "skipped": 0}
    assert c.exportScansToFolder(["x"], "") == {
        "exported": 0, "skipped": 0}


def test_export_scans_to_folder_no_db(tmp_path):
    c = _connector(tmp_path, scan_db_path=None)
    errors = []
    c.errorOccurred.connect(errors.append)
    res = c.exportScansToFolder(["scanA"], str(tmp_path))
    assert res == {"exported": 0, "skipped": 0}
    assert errors  # errorOccurred was emitted
