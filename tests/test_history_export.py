"""History export slots — exportScanCsv / exportScansToFolder.

Both slots resolve scans by their unique DB session id (issue #254: a
by-label lookup returns the newest session with that label, so a
duplicate label silently exported a different scan than selected).

The QML-facing slots are fire-and-forget since the GUI-freeze fix (the
work runs on a daemon worker and reports via scanCsvExportFinished /
scansExportFinished); the logic tests exercise the synchronous worker
bodies (_export_scan_csv_sync / _export_scans_sync) directly.
"""

import os
import time
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
    sid = db.create_session(session_label=label, session_start=start,
                            session_notes=None, session_meta={})
    db.close()
    return sid


def test_export_scans_to_folder_writes_each_and_skips_missing(
        tmp_path, monkeypatch):
    db_path = str(tmp_path / "scans.db")
    sid_a = _session(db_path, "scanA", 1.0)
    sid_b = _session(db_path, "scanB", 2.0)
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
    res = c._export_scans_sync(
        [sid_a, sid_b, 9999], str(out_dir))

    assert res["exported"] == 2
    assert res["skipped"] == 1            # 9999 has no DB session
    assert len(calls) == 2
    names = sorted(os.path.basename(p) for p in calls)
    assert names == ["scanA_export.csv", "scanB_export.csv"]


def test_export_scans_to_folder_isolates_per_scan_failure(
        tmp_path, monkeypatch):
    """A scan whose materialize raises is counted skipped, and the batch
    keeps going for the remaining scans."""
    db_path = str(tmp_path / "scans.db")
    sid_a = _session(db_path, "scanA", 1.0)
    sid_b = _session(db_path, "scanB", 2.0)
    c = _connector(tmp_path, scan_db_path=db_path)

    exported = []
    import omotion.SessionPlayback as sp

    def _materialize(*a, **k):
        # a[2] == output_path, ends with "<label>_export.csv"
        if os.path.basename(a[2]) == "scanA_export.csv":
            raise RuntimeError("boom")
        exported.append(a[2])

    monkeypatch.setattr(sp, "materialize_corrected_csv", _materialize)

    res = c._export_scans_sync([sid_a, sid_b], str(tmp_path))

    assert res == {"exported": 1, "skipped": 1}
    assert len(exported) == 1  # scanB still ran after scanA failed
    assert os.path.basename(exported[0]) == "scanB_export.csv"


def test_export_scans_to_folder_empty_inputs(tmp_path):
    c = _connector(tmp_path, scan_db_path=str(tmp_path / "scans.db"))
    assert c._export_scans_sync([], str(tmp_path)) == {
        "exported": 0, "skipped": 0}
    assert c._export_scans_sync([1], "") == {
        "exported": 0, "skipped": 0}


def test_export_scans_to_folder_no_db(tmp_path):
    c = _connector(tmp_path, scan_db_path=None)
    errors = []
    c.errorOccurred.connect(errors.append)
    res = c._export_scans_sync([1], str(tmp_path))
    assert res == {"exported": 0, "skipped": 0}
    assert errors  # errorOccurred was emitted


def test_export_batch_slot_is_async_and_reports_via_signal(
        tmp_path, monkeypatch):
    """The QML-facing batch slot returns immediately, runs on a worker
    thread, and reports via scansExportFinished — resolving by id."""
    db_path = str(tmp_path / "scans.db")
    sid_a = _session(db_path, "scanA", 1.0)
    c = _connector(tmp_path, scan_db_path=db_path)
    import omotion.SessionPlayback as sp
    monkeypatch.setattr(sp, "materialize_corrected_csv",
                        lambda *a, **k: None)

    results = []
    c.scansExportFinished.connect(
        lambda e, s, f: results.append((e, s, f)))
    c.exportScansToFolder([sid_a, 9999], str(tmp_path))  # 9999 missing

    # The worker's emit is queued to the main thread — pump the event
    # loop so it can deliver (exactly what the running app's loop does).
    from PyQt6.QtCore import QCoreApplication
    app = QCoreApplication.instance() or QCoreApplication([])
    deadline = time.time() + 5
    while not results and time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert results == [(1, 1, str(tmp_path))]


def test_export_batch_slot_empty_inputs_signal_immediately(tmp_path):
    c = _connector(tmp_path, scan_db_path=str(tmp_path / "scans.db"))
    results = []
    c.scansExportFinished.connect(
        lambda e, s, f: results.append((e, s, f)))
    c.exportScansToFolder([], str(tmp_path))
    assert results == [(0, 0, str(tmp_path))]


# ── exportScanCsv (single-scan Save As path) ─────────────────────────
# The QML slot is async (emits scanCsvExportFinished); the logic tests
# drive the synchronous worker body _export_scan_csv_sync directly.


def test_export_scan_csv_materializes_the_given_session_id(
        tmp_path, monkeypatch):
    db_path = str(tmp_path / "scans.db")
    sid = _session(db_path, "scanA", 1.0)
    c = _connector(tmp_path, scan_db_path=db_path)

    calls = []
    import omotion.SessionPlayback as sp
    monkeypatch.setattr(
        sp, "materialize_corrected_csv",
        lambda *a, **k: calls.append(a),  # a == (db_path, sid, out_path)
    )

    out = str(tmp_path / "scanA_export.csv")
    assert c._export_scan_csv_sync(sid, out) is True
    assert len(calls) == 1
    assert calls[0][1] == sid
    assert calls[0][2] == out


def test_export_scan_csv_duplicate_labels_export_the_selected_scan(
        tmp_path, monkeypatch):
    """Regression for issue #254: two sessions sharing a label must not
    make the export fall through to the newest one — the id passed from
    the History selection wins."""
    db_path = str(tmp_path / "scans.db")
    older = _session(db_path, "dup_label", 1.0)
    newer = _session(db_path, "dup_label", 2.0)
    assert older != newer
    c = _connector(tmp_path, scan_db_path=db_path)

    exported_ids = []
    import omotion.SessionPlayback as sp
    monkeypatch.setattr(
        sp, "materialize_corrected_csv",
        lambda *a, **k: exported_ids.append(a[1]),  # a[1] == session_id
    )

    assert c._export_scan_csv_sync(older, str(tmp_path / "old.csv")) is True
    assert exported_ids == [older]  # not `newer`, which shares the label


def test_export_scan_csv_missing_session_emits_error(tmp_path, monkeypatch):
    db_path = str(tmp_path / "scans.db")
    _session(db_path, "scanA", 1.0)
    c = _connector(tmp_path, scan_db_path=db_path)

    import omotion.SessionPlayback as sp
    monkeypatch.setattr(
        sp, "materialize_corrected_csv",
        lambda *a, **k: pytest.fail("must not materialize a missing id"),
    )

    errors = []
    c.errorOccurred.connect(errors.append)
    assert c._export_scan_csv_sync(9999, str(tmp_path / "x.csv")) is False
    assert errors  # errorOccurred was emitted


def test_export_scan_csv_slot_invalid_inputs_signal_false(tmp_path):
    """The async slot guards bad inputs and reports failure via signal
    without spawning a worker."""
    c = _connector(tmp_path, scan_db_path=str(tmp_path / "scans.db"))
    results = []
    c.scanCsvExportFinished.connect(lambda ok, p: results.append((ok, p)))
    c.exportScanCsv(-1, str(tmp_path / "x.csv"))
    c.exportScanCsv(1, "")
    assert results == [(False, str(tmp_path / "x.csv")), (False, "")]
