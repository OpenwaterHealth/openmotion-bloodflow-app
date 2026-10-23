"""Scan notes — persisted to the scan DB session, not a notes.txt file."""

import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector, _CompletionSink
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


def _session(db_path, label, start=1.0, notes=None):
    db = ScanDatabase(db_path=db_path)
    sid = db.create_session(session_label=label, session_start=start,
                            session_notes=notes, session_meta={})
    db.close()
    return sid


def _db_notes(db_path, label):
    db = ScanDatabase(db_path=db_path)
    try:
        session = db.get_session_by_label(label)
        return session["session_notes"] if session else None
    finally:
        db.close()


LABEL = "20260610_163915_ow3SW4HD"


def _raise_locked(*_args, **_kwargs):
    raise sqlite3.OperationalError("database is locked")


def _toasts(c):
    """Record every toast the connector raises as (type, text)."""
    seen = []
    c.notificationRequested.connect(
        lambda payload: seen.append((payload["type"], payload["text"]))
    )
    return seen


class TestPersistScanNotes:
    def test_persist_writes_notes_to_db_session(self, tmp_path):
        db_path = str(tmp_path / "scans.db")
        _session(db_path, LABEL)
        c = _connector(tmp_path, scan_db_path=db_path)
        c._scan_notes = ("subject reports headache\n---\n"
                         "Scan completed — duration: 00:05:00")

        assert c._persist_scan_notes(LABEL) is True
        assert _db_notes(db_path, LABEL) == c._scan_notes.strip()

    def test_persist_returns_none_when_session_missing(self, tmp_path):
        """None, not False: ScanDBSink deleted the session (the scan saved
        nothing), which is not a write failure (#725)."""
        db_path = str(tmp_path / "scans.db")
        _session(db_path, "20260101_000000_other")
        c = _connector(tmp_path, scan_db_path=db_path)
        c._scan_notes = "orphan notes"
        assert c._persist_scan_notes(LABEL) is None
        assert _db_notes(db_path, "20260101_000000_other") is None

    def test_persist_returns_none_without_label(self, tmp_path):
        c = _connector(tmp_path, scan_db_path=str(tmp_path / "scans.db"))
        c._scan_notes = "notes"
        assert c._persist_scan_notes("") is None

    def test_persist_returns_false_without_db_path(self, tmp_path):
        c = _connector(tmp_path, scan_db_path=None)
        c._scan_notes = "notes"
        assert c._persist_scan_notes(LABEL) is False

    def test_persist_returns_false_when_db_write_fails(self, tmp_path,
                                                       monkeypatch):
        db_path = str(tmp_path / "scans.db")
        _session(db_path, LABEL)
        c = _connector(tmp_path, scan_db_path=db_path)
        c._scan_notes = "notes"
        monkeypatch.setattr(ScanDatabase, "update_session", _raise_locked)
        assert c._persist_scan_notes(LABEL) is False


class TestScanNotesSetter:
    def test_setter_persists_to_db_and_writes_no_file(self, tmp_path):
        """Editing notes in the modal after a scan must update the DB
        session and must NOT create any *_notes.txt file."""
        db_path = str(tmp_path / "scans.db")
        _session(db_path, LABEL)
        c = _connector(tmp_path, scan_db_path=db_path)
        c._scan_notes_session_label = LABEL

        c.scanNotes = "edited after scan"

        assert _db_notes(db_path, LABEL) == "edited after scan"
        assert list((tmp_path / "data").glob("*_notes.txt")) == []

    def test_setter_clears_notes_in_db(self, tmp_path):
        db_path = str(tmp_path / "scans.db")
        _session(db_path, LABEL, notes="old text")
        c = _connector(tmp_path, scan_db_path=db_path)
        c._scan_notes_session_label = LABEL

        c.scanNotes = ""

        assert _db_notes(db_path, LABEL) == ""

    def test_setter_without_session_is_memory_only(self, tmp_path):
        """Typing notes before any scan completes keeps them in memory."""
        c = _connector(tmp_path, scan_db_path=None)
        c.scanNotes = "pre-scan notes"
        assert c.scanNotes == "pre-scan notes"
        assert list((tmp_path / "data").glob("*_notes.txt")) == []


class TestSaveScanNotesToast:
    """The Notes modal's save reports what actually happened to the note:
    "Note saved." only once it is in the scan's DB session (#725)."""

    def test_stored_note_toasts_saved(self, tmp_path):
        db_path = str(tmp_path / "scans.db")
        _session(db_path, LABEL)
        c = _connector(tmp_path, scan_db_path=db_path)
        c._scan_notes_session_label = LABEL
        toasts = _toasts(c)

        c.saveScanNotes("subject moved at 00:02")

        assert _db_notes(db_path, LABEL) == "subject moved at 00:02"
        assert toasts == [("success", "Note saved.")]

    def test_deleted_session_is_an_error_not_saved(self, tmp_path):
        """The #725 sequence: the label points at a session ScanDBSink
        deleted as empty. Nothing is stored, so no success toast."""
        db_path = str(tmp_path / "scans.db")
        _session(db_path, "20260101_000000_other")
        c = _connector(tmp_path, scan_db_path=db_path)
        c._scan_notes_session_label = LABEL
        toasts = _toasts(c)

        c.saveScanNotes("note for a scan that was not saved")

        assert _db_notes(db_path, LABEL) is None
        assert len(toasts) == 1
        assert toasts[0][0] == "error"
        assert "not saved" in toasts[0][1]

    def test_db_write_failure_is_an_error(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "scans.db")
        _session(db_path, LABEL, notes="before")
        c = _connector(tmp_path, scan_db_path=db_path)
        c._scan_notes_session_label = LABEL
        monkeypatch.setattr(ScanDatabase, "update_session", _raise_locked)
        toasts = _toasts(c)

        c.saveScanNotes("after")

        assert _db_notes(db_path, LABEL) == "before"
        assert len(toasts) == 1
        assert toasts[0][0] == "error"
        assert "not saved" in toasts[0][1]

    def test_mid_scan_note_is_held_not_reported_saved(self, tmp_path):
        """During a scan the note waits in the buffer for the scan end to
        store it; the toast must not claim it is already saved."""
        c = _connector(tmp_path, scan_db_path=str(tmp_path / "scans.db"))
        c._capture_running = True
        toasts = _toasts(c)

        c.saveScanNotes("[00:01:23 / 14:32:05] - cough")

        assert c.scanNotes == "[00:01:23 / 14:32:05] - cough"
        assert len(toasts) == 1
        assert toasts[0][0] == "success"
        assert toasts[0][1] != "Note saved."
        assert "when the scan ends" in toasts[0][1]

    def test_note_with_no_scan_is_an_error(self, tmp_path):
        c = _connector(tmp_path, scan_db_path=str(tmp_path / "scans.db"))
        toasts = _toasts(c)

        c.saveScanNotes("typed before any scan")

        assert len(toasts) == 1
        assert toasts[0][0] == "error"
        assert "no saved scan" in toasts[0][1]

    def test_empty_modal_with_no_scan_raises_no_toast(self, tmp_path):
        """Nothing typed, nothing lost: closing an empty modal is silent."""
        c = _connector(tmp_path, scan_db_path=str(tmp_path / "scans.db"))
        toasts = _toasts(c)

        c.saveScanNotes("  \n")

        assert toasts == []


SCAN_ID = "20261013_162530"
SUBJECT = "ow5GUBRO"
SCAN_LABEL = f"{SCAN_ID}_{SUBJECT}"


def _scan_connector(tmp_path, db_path):
    """A connector that has started a scan against a mocked interface,
    plus the completion sink startCapture handed to the SDK (same pattern
    as tests/test_live_contact_quality_adapter.py)."""
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.start_scan.return_value = True
    iface.scan_db_path = db_path
    c = MotionConnector(
        interface=iface, app_config={"engineeringMode": False},
        data_dir=str(tmp_path), config_dir="config",
    )
    c._consoleConnected = True
    c._leftSensorConnected = True
    c._rightSensorConnected = True
    assert c.startCapture(SUBJECT, 5, 0x66, 0x66, False) is True
    req = iface.start_scan.call_args.args[0]
    (sink,) = [s for s in req.sinks if isinstance(s, _CompletionSink)]
    return c, sink


def _stop_scan(c, sink):
    """What an operator Stop ends in: the stop event is set and the SDK
    calls the completion sink. Calls the callback directly so a failure
    raises instead of being logged by _CompletionSink.on_complete."""
    meta = SimpleNamespace(scan_id=SCAN_ID)
    sink.on_scan_start(meta)
    c._capture_stop.set()
    sink._on_complete_cb(meta)


class TestScanEndNotes:
    def test_saved_scan_offers_notes_and_stores_footer(self, tmp_path):
        db_path = str(tmp_path / "scans.db")
        c, sink = _scan_connector(tmp_path, db_path)
        # ScanDBSink created the session at scan start and kept it.
        _session(db_path, SCAN_LABEL)
        ready = []
        c.scanNotesReady.connect(lambda: ready.append(True))
        c.scanNotes = "baseline"

        _stop_scan(c, sink)

        assert ready == [True]
        assert c._scan_notes_session_label == SCAN_LABEL
        stored = _db_notes(db_path, SCAN_LABEL)
        assert stored.startswith("baseline\n---\nScan stopped")

    def test_empty_scan_does_not_offer_notes(self, tmp_path):
        """#725: ScanDBSink deleted the empty session, so there is nothing
        to attach a note to. No Notes modal, no session label to save
        into, no footer for a scan that does not exist."""
        db_path = str(tmp_path / "scans.db")
        c, sink = _scan_connector(tmp_path, db_path)
        ready = []
        c.scanNotesReady.connect(lambda: ready.append(True))
        toasts = _toasts(c)

        _stop_scan(c, sink)

        assert ready == []
        assert c._scan_notes_session_label == ""
        assert c.scanNotes == ""
        assert toasts == []

    def test_empty_scan_reports_lost_mid_scan_notes(self, tmp_path):
        db_path = str(tmp_path / "scans.db")
        c, sink = _scan_connector(tmp_path, db_path)
        ready = []
        c.scanNotesReady.connect(lambda: ready.append(True))
        c.saveScanNotes("[00:00:01 / 09:25:31] - started")
        toasts = _toasts(c)

        _stop_scan(c, sink)

        assert ready == []
        # The operator's text stays in the buffer (no footer), so the
        # Notes button still shows it, but nothing claims it was stored.
        assert c.scanNotes == "[00:00:01 / 09:25:31] - started"
        assert len(toasts) == 1
        assert toasts[0][0] == "error"
        assert "not saved" in toasts[0][1]

    def test_saving_after_an_empty_scan_never_reports_saved(self, tmp_path):
        """The ticket's end-to-end sequence: Start, Stop within a second,
        then save a note anyway (Notes button)."""
        db_path = str(tmp_path / "scans.db")
        c, sink = _scan_connector(tmp_path, db_path)
        _stop_scan(c, sink)
        toasts = _toasts(c)

        c.saveScanNotes("note after the empty scan")

        assert ("success", "Note saved.") not in toasts
        assert [t for t, _ in toasts] == ["error"]
        assert _db_notes(db_path, SCAN_LABEL) is None


class TestGetScanDetailsNotes:
    def test_details_read_notes_from_db(self, tmp_path):
        db_path = str(tmp_path / "scans.db")
        _session(db_path, LABEL, notes="db-stored notes")
        c = _connector(tmp_path, scan_db_path=db_path)

        details = c.get_scan_details(LABEL)
        assert details["notes"] == "db-stored notes"

    def test_details_fall_back_to_legacy_notes_txt(self, tmp_path):
        """Scans from before the DB migration only have a notes.txt —
        History must still show their notes."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / f"{LABEL}_notes.txt").write_text("legacy file notes\n",
                                                      encoding="utf-8")
        c = _connector(tmp_path, scan_db_path=None)

        details = c.get_scan_details(LABEL)
        assert details["notes"] == "legacy file notes\n"

    def test_db_notes_win_over_legacy_file(self, tmp_path):
        db_path = str(tmp_path / "scans.db")
        _session(db_path, LABEL, notes="db notes")
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / f"{LABEL}_notes.txt").write_text("stale file notes\n",
                                                      encoding="utf-8")
        c = _connector(tmp_path, scan_db_path=db_path)

        details = c.get_scan_details(LABEL)
        assert details["notes"] == "db notes"
