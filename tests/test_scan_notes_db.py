"""Scan notes — persisted to the scan DB session, not a notes.txt file."""

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
        interface=iface, app_config={"developerMode": False},
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


class TestPersistScanNotes:
    def test_persist_writes_notes_to_db_session(self, tmp_path):
        db_path = str(tmp_path / "scans.db")
        _session(db_path, LABEL)
        c = _connector(tmp_path, scan_db_path=db_path)
        c._scan_notes = ("subject reports headache\n---\n"
                         "Scan completed — duration: 00:05:00")

        assert c._persist_scan_notes(LABEL) is True
        assert _db_notes(db_path, LABEL) == c._scan_notes.strip()

    def test_persist_returns_false_when_session_missing(self, tmp_path):
        db_path = str(tmp_path / "scans.db")
        _session(db_path, "20260101_000000_other")
        c = _connector(tmp_path, scan_db_path=db_path)
        c._scan_notes = "orphan notes"
        assert c._persist_scan_notes(LABEL) is False

    def test_persist_returns_false_without_db_path(self, tmp_path):
        c = _connector(tmp_path, scan_db_path=None)
        c._scan_notes = "notes"
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
        assert list(tmp_path.glob("*_notes.txt")) == []

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
        assert list(tmp_path.glob("*_notes.txt")) == []


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
        (tmp_path / f"{LABEL}_notes.txt").write_text("legacy file notes\n",
                                                     encoding="utf-8")
        c = _connector(tmp_path, scan_db_path=None)

        details = c.get_scan_details(LABEL)
        assert details["notes"] == "legacy file notes\n"

    def test_db_notes_win_over_legacy_file(self, tmp_path):
        db_path = str(tmp_path / "scans.db")
        _session(db_path, LABEL, notes="db notes")
        (tmp_path / f"{LABEL}_notes.txt").write_text("stale file notes\n",
                                                     encoding="utf-8")
        c = _connector(tmp_path, scan_db_path=db_path)

        details = c.get_scan_details(LABEL)
        assert details["notes"] == "db notes"
