"""loadPastScan / get_scan_details — resolve DB sessions by unique id.

Regression for issue #254 (same family as the export fix in PR #296):
``get_session_by_label`` returns the newest session with that label, so
duplicate labels (a persistent user-label token across scans, imported
DBs) made the by-label call sites silently load a different scan than
the one selected in History.
"""

import json
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
    # explicit: MagicMock default would be truthy
    iface.scan_db_path = scan_db_path
    return MotionConnector(
        interface=iface, app_config={"engineeringMode": False},
        data_dir=str(tmp_path), config_dir="config",
    )


def _session(db_path, label, start, notes=None, meta=None):
    db = ScanDatabase(db_path=db_path)
    sid = db.create_session(session_label=label, session_start=start,
                            session_notes=notes, session_meta=meta or {})
    db.close()
    return sid


DUP_LABEL = "20260610_163915_owDUP"


# ── get_scan_details ─────────────────────────────────────────────────


def test_get_scan_details_by_id_wins_over_duplicate_labels(tmp_path):
    """Two sessions sharing a label: session_id must select the exact
    session, not the newest one with that label."""
    db_path = str(tmp_path / "scans.db")
    older = _session(db_path, DUP_LABEL, 1.0, notes="older notes")
    newer = _session(db_path, DUP_LABEL, 2.0, notes="newer notes")
    assert older != newer
    c = _connector(tmp_path, scan_db_path=db_path)

    details = c.get_scan_details(DUP_LABEL, session_id=older)
    assert details["notes"] == "older notes"


def test_get_scan_details_without_id_keeps_by_label_fallback(tmp_path):
    """No session_id → the legacy by-label lookup still works (used by
    callers that only have a label, e.g. CSV-era scans)."""
    db_path = str(tmp_path / "scans.db")
    _session(db_path, DUP_LABEL, 1.0, notes="only notes")
    c = _connector(tmp_path, scan_db_path=db_path)

    details = c.get_scan_details(DUP_LABEL)
    assert details["notes"] == "only notes"


# ── loadPastScan worker ──────────────────────────────────────────────


def _worker_emissions(c):
    """Collect _pastScanBuffersReady emissions (direct connection — the
    worker is called synchronously on the test thread)."""
    emitted = []
    c._pastScanBuffersReady.connect(
        lambda *args: emitted.append(args))
    return emitted


def test_worker_loads_the_selected_session_not_newest_dup(tmp_path):
    """Regression for issue #254: with a duplicate label, the worker
    must resolve the id it was given — proven by the recorded sdk_flags
    (per-session meta) it emits, which differ between the two dups."""
    db_path = str(tmp_path / "scans.db")
    older = _session(
        db_path, DUP_LABEL, 1.0,
        meta={"sdk_flags": {"left_camera_mask": 0x11,
                            "right_camera_mask": 0x22}})
    _session(
        db_path, DUP_LABEL, 2.0,
        meta={"sdk_flags": {"left_camera_mask": 0xEE,
                            "right_camera_mask": 0xFF}})
    c = _connector(tmp_path, scan_db_path=db_path)
    emitted = _worker_emissions(c)

    c._load_past_scan_worker(1, older, DUP_LABEL, db_path, None)

    assert len(emitted) == 1
    seq, label, sid, buffers, _tag, flags, _meta = emitted[0]
    assert sid == older
    assert label == DUP_LABEL
    assert buffers is not None
    assert flags["left_camera_mask"] == 0x11  # older's meta, not newer's


def test_worker_missing_id_emits_failure(tmp_path):
    db_path = str(tmp_path / "scans.db")
    _session(db_path, DUP_LABEL, 1.0)
    c = _connector(tmp_path, scan_db_path=db_path)
    emitted = _worker_emissions(c)

    c._load_past_scan_worker(1, 9999, DUP_LABEL, db_path, None)

    assert len(emitted) == 1
    _seq, _label, sid, buffers, *_ = emitted[0]
    assert sid == -1
    assert buffers is None


# ── loadPastScan slot (GUI-thread half) ──────────────────────────────


def test_load_past_scan_rejects_invalid_id(tmp_path, monkeypatch):
    """A negative id must fail fast: finished(False), no worker thread."""
    db_path = str(tmp_path / "scans.db")
    c = _connector(tmp_path, scan_db_path=db_path)

    import threading

    def _no_thread(*a, **k):
        pytest.fail("no worker thread may start for an invalid id")
    monkeypatch.setattr(threading, "Thread", _no_thread)

    finished = []
    c.pastScanLoadFinished.connect(lambda label, ok: finished.append(
        (label, ok)))
    c.loadPastScan(-1, DUP_LABEL)
    assert finished == [(DUP_LABEL, False)]


def test_load_past_scan_audits_label_and_id(tmp_path, monkeypatch):
    db_path = str(tmp_path / "scans.db")
    sid = _session(db_path, DUP_LABEL, 1.0)
    c = _connector(tmp_path, scan_db_path=db_path)

    # Don't actually spawn the load; the audit fires before it.
    started = []
    import threading

    class _FakeThread:
        def __init__(self, *a, **k):
            started.append(k.get("args"))

        def start(self):
            pass
    monkeypatch.setattr(threading, "Thread", _FakeThread)

    c.loadPastScan(sid, DUP_LABEL)

    ev = [e for e in c.auditLogEntries()
          if e["event_type"] == "scan_viewed"]
    assert ev
    details = json.loads(ev[0]["details"])
    assert details["label"] == DUP_LABEL
    assert details["session_id"] == sid
    # The worker was handed the id (second arg after seq).
    assert started and started[0][1] == sid
