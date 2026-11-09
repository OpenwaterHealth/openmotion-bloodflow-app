"""Scan notes — persisted to the scan DB session, not a notes.txt file."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from motion_connector import (
    MotionConnector, _CompletionSink, _scan_identity_note_lines,
)
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


SCAN_ID = "20261013_162530"
SCAN_LABEL = f"{SCAN_ID}_subj"
APP_VERSION = "1.6.0-dev.1"
SDK_VERSION = "1.14.0"


def _start_scan(tmp_path, left_mask=0x66, right_mask=0x66, *,
                right_connected=True, serials=None, firmware=None):
    """Start a scan on the mocked interface. Returns the connector, the
    completion sink startCapture handed the SDK, and the scan DB path."""
    db_path = str(tmp_path / "scans.db")
    c = _connector(tmp_path, scan_db_path=db_path)
    c._interface.is_device_connected.return_value = (
        True, True, right_connected)
    c._consoleConnected = c._leftSensorConnected = True
    c._rightSensorConnected = right_connected
    c._device_serials.update(serials or {})
    c._firmware_versions.update(firmware or {})
    c._app_version = APP_VERSION
    c._interface.get_sdk_version.return_value = SDK_VERSION
    c._interface.start_scan.return_value = True
    assert c.startCapture("subj", 5, left_mask, right_mask, False) is True
    req = c._interface.start_scan.call_args.args[0]
    (sink,) = [s for s in req.sinks if isinstance(s, _CompletionSink)]
    return c, sink, db_path


def _stop_scan(tmp_path, keep_session):
    """Start a scan and stop it through the completion sink. Returns one
    entry per Notes modal offer (scanNotesReady)."""
    c, sink, db_path = _start_scan(tmp_path)
    if keep_session:  # ScanDBSink keeps the session of a scan with data
        _session(db_path, SCAN_LABEL)
    offers = []
    c.scanNotesReady.connect(lambda: offers.append(True))
    c._capture_stop.set()
    sink._on_complete_cb(SimpleNamespace(scan_id=SCAN_ID))
    return offers


class TestScanEndNotesModal:
    def test_scan_with_data_offers_notes(self, tmp_path):
        offers = _stop_scan(tmp_path, keep_session=True)
        assert offers == [True]

    def test_scan_without_data_does_not_offer_notes(self, tmp_path):
        """#725: stopped before any data was recorded, so ScanDBSink
        deleted the session and there is nowhere to store a note."""
        offers = _stop_scan(tmp_path, keep_session=False)
        assert offers == []


SERIALS = {"console": "WWW04Q40005", "left": "QWW04Q10003",
           "right": "QWW04Q10004"}
FIRMWARE = {"console": "v1.8.3", "left": "v1.8.4", "right": "v1.8.5"}
FOOTER_LINES = ["Console SN: WWW04Q40005",
                "Left Sensor SN: QWW04Q10003",
                "Right Sensor SN: QWW04Q10004",
                "Scan configuration: FFFF",
                f"App version: {APP_VERSION}",
                f"SDK version: {SDK_VERSION}",
                "Console FW: v1.8.3",
                "Left Sensor FW: v1.8.4",
                "Right Sensor FW: v1.8.5"]
FOOTER = "".join("\n" + line for line in FOOTER_LINES)


def _footer(serials=SERIALS, firmware=FIRMWARE, left=0xFF, right=0xFF,
            app_version=APP_VERSION, sdk_version=SDK_VERSION):
    return _scan_identity_note_lines(serials, firmware, left, right,
                                     app_version=app_version,
                                     sdk_version=sdk_version)


def _line(footer, prefix):
    (line,) = [ln for ln in footer.splitlines() if ln.startswith(prefix)]
    return line


class TestScanIdentityFooter:
    """#759: the end-of-scan footer names the hardware, the cameras and
    the software versions."""

    def test_footer_lines(self):
        assert _footer() == FOOTER

    @pytest.mark.parametrize("left, right, config", [
        (0xFF, 0xFF, "FFFF"),
        (0xFF, 0x00, "FF00"),
        (0x00, 0xFF, "00FF"),
        (0x5A, 0x0F, "5A0F"),
    ])
    def test_configuration_is_left_then_right_mask(self, left, right,
                                                   config):
        assert _line(_footer(left=left, right=right),
                     "Scan configuration:") == f"Scan configuration: {config}"

    @pytest.mark.parametrize("empty", ["", "   "])
    def test_empty_values(self, empty):
        blank = {"console": empty, "left": empty, "right": empty}
        footer = _footer(serials=blank, firmware=blank, app_version=empty,
                         sdk_version=empty)
        assert footer.splitlines()[1:] == [
            "Console SN: Not programmed",
            "Left Sensor SN: Not programmed",
            "Right Sensor SN: Not programmed",
            "Scan configuration: FFFF",
            "App version: unknown",
            "SDK version: unknown",
            "Console FW: unknown",
            "Left Sensor FW: unknown",
            "Right Sensor FW: unknown",
        ]

    def test_non_string_sdk_version_reads_unknown(self):
        assert _line(_footer(sdk_version=None),
                     "SDK version:") == "SDK version: unknown"

    def test_unconnected_sensor_reads_not_connected(self):
        footer = _footer(serials={**SERIALS, "right": None},
                         firmware={**FIRMWARE, "right": None}, right=0x00)
        assert _line(footer, "Right Sensor SN:") == (
            "Right Sensor SN: Not connected")
        assert _line(footer, "Right Sensor FW:") == (
            "Right Sensor FW: Not connected")

    def test_scan_end_note_carries_the_footer(self, tmp_path):
        c, sink, db_path = _start_scan(tmp_path, 0xFF, 0xFF,
                                       serials=SERIALS, firmware=FIRMWARE)
        _session(db_path, SCAN_LABEL)
        # A mid-scan disconnect clears a device's cached serial and
        # firmware; the note still names what the scan started on.
        c._device_serials.update(console="", left="", right="")
        c._firmware_versions.update(console="", left="", right="")

        sink._on_complete_cb(SimpleNamespace(scan_id=SCAN_ID))

        lines = c.scanNotes.strip().splitlines()
        assert lines[0] == "---"
        assert lines[1].startswith("Scan completed — duration: ")
        assert lines[2:] == FOOTER_LINES
        assert _db_notes(db_path, SCAN_LABEL) == c.scanNotes.strip()

    def test_side_without_a_sensor(self, tmp_path):
        """The UI's pending right selection never reached the scan (#352),
        so the note must not claim it."""
        c, sink, db_path = _start_scan(tmp_path, 0xFF, 0xFF,
                                       right_connected=False,
                                       serials=SERIALS, firmware=FIRMWARE)
        _session(db_path, SCAN_LABEL)

        sink._on_complete_cb(SimpleNamespace(scan_id=SCAN_ID))

        notes = c.scanNotes
        assert _line(notes, "Right Sensor SN:") == (
            "Right Sensor SN: Not connected")
        assert _line(notes, "Right Sensor FW:") == (
            "Right Sensor FW: Not connected")
        assert _line(notes, "Scan configuration:") == (
            "Scan configuration: FF00")

    def test_operator_notes_stay_above_the_footer(self, tmp_path):
        c, sink, db_path = _start_scan(tmp_path, 0xFF, 0xFF,
                                       serials=SERIALS, firmware=FIRMWARE)
        _session(db_path, SCAN_LABEL)
        c._scan_notes = "subject moved at 2 min\n"

        sink._on_complete_cb(SimpleNamespace(scan_id=SCAN_ID))

        notes = c.scanNotes
        assert notes.startswith("subject moved at 2 min\n---\nScan completed")
        assert notes.endswith(FOOTER)


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
