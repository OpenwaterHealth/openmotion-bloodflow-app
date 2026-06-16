"""History data-management connector slots — DB-only, no hardware."""

from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector, _config_name
from omotion.ScanDatabase import ScanDatabase

pytestmark = pytest.mark.unit


def _connector(tmp_path, scan_db_path):
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = scan_db_path
    return MotionConnector(
        interface=iface, app_config={"developerMode": False},
        data_dir=str(tmp_path), config_dir="config",
    )


def _make_session(db_path, label, start, end, left_mask, right_mask,
                  reduced=True, notes=None, subject=None, operator="ethan"):
    # Default the subject_id to the label's trailing user-label segment so
    # rows carry distinct userLabels (matches how scans are really named).
    if subject is None:
        parts = label.split("_", 2)
        subject = parts[2] if len(parts) > 2 else ""
    db = ScanDatabase(db_path=db_path)
    sid = db.create_session(
        session_label=label, session_start=start, session_end=end,
        session_notes=notes,
        session_meta={
            "scan_id": label.rsplit("_", 1)[0], "subject_id": subject,
            "operator": operator, "duration_sec": (end - start) if end else 0,
            "sdk_flags": {
                "reduced_mode": reduced,
                "left_camera_mask": left_mask,
                "right_camera_mask": right_mask,
            },
        },
    )
    db.close()
    return sid


def test_config_name_known_and_unknown():
    assert _config_name(0x5A) == "Near"
    assert _config_name(0xC3) == "Far"
    assert _config_name(0x00) == "None"
    assert _config_name(0x12) == "0x12"   # unmapped -> hex
    assert _config_name(-1) == "—"        # unknown
    assert _config_name(None) == "—"      # missing


def test_get_scan_sessions_skips_malformed_row_keeps_rest(tmp_path):
    """One unparseable session must not blank the whole History view."""
    db_path = str(tmp_path / "scans.db")
    _make_session(db_path, "20260612_092000_subjA", 100.0, 105.0, 0xC3, 0xC3)
    c = _connector(tmp_path, db_path)

    real = MotionConnector._session_to_row

    def flaky(self, s):
        if s.get("session_label", "").endswith("boom"):
            raise ValueError("malformed row")
        return real(self, s)

    db = ScanDatabase(db_path=db_path)
    db.create_session(session_label="20260612_093000_boom",
                      session_start=200.0, session_end=205.0,
                      session_notes=None, session_meta={})
    db.close()

    c._session_to_row = flaky.__get__(c, MotionConnector)
    rows = c.get_scan_sessions()
    labels = [r["label"] for r in rows]
    assert "20260612_092000_subjA" in labels
    assert "20260612_093000_boom" not in labels


def test_get_scan_sessions_rows_and_sort(tmp_path):
    db_path = str(tmp_path / "scans.db")
    _make_session(db_path, "20260612_092000_subjA", 100.0, 105.0, 0xC3, 0xC3)
    _make_session(db_path, "20260612_093100_subjB", 200.0, 215.0, 0x5A, 0x66)
    c = _connector(tmp_path, db_path)

    rows = c.get_scan_sessions()
    assert [r["userLabel"] for r in rows] == ["subjB", "subjA"]  # newest first
    top = rows[0]
    assert top["configL"] == "Near" and top["configR"] == "Middle"
    assert top["durationSec"] == 15.0
    assert top["leftMask"] == 0x5A and top["rightMask"] == 0x66
    assert top["interrupted"] is False
    assert top["dateTime"] == "2026-06-12 09:31:00"


def test_get_scan_sessions_interrupted_open_session(tmp_path):
    db_path = str(tmp_path / "scans.db")
    _make_session(db_path, "20260612_100000_subjC", 300.0, None, 0xFF, 0xFF)
    c = _connector(tmp_path, db_path)
    row = c.get_scan_sessions()[0]
    assert row["interrupted"] is True
    assert row["durationSec"] == -1.0


def test_get_scan_sessions_empty_without_db(tmp_path):
    c = _connector(tmp_path, None)
    assert c.get_scan_sessions() == []


def _insert_rows(db_path, session_id, n):
    db = ScanDatabase(db_path=db_path)
    for i in range(n):
        db.insert_session_data(
            session_id=session_id, cam_id=0, side=0,
            timestamp_s=float(i), frame_id=i, bfi=1.0, bvi=2.0,
        )
    db.close()


def test_get_session_stats_counts_rows(tmp_path):
    db_path = str(tmp_path / "scans.db")
    sid = _make_session(
        db_path, "20260612_092000_subjA", 100.0, 105.0, 0xC3, 0xC3)
    _insert_rows(db_path, sid, 7)
    c = _connector(tmp_path, db_path)
    assert c.get_session_stats(sid)["sampleCount"] == 7


def test_delete_scans_removes_session_and_cascades(tmp_path):
    db_path = str(tmp_path / "scans.db")
    keep = _make_session(
        db_path, "20260612_092000_keep", 100.0, 105.0, 0xC3, 0xC3)
    drop = _make_session(
        db_path, "20260612_093000_drop", 200.0, 205.0, 0x5A, 0x5A)
    _insert_rows(db_path, drop, 5)
    c = _connector(tmp_path, db_path)

    removed = c.deleteScans([drop])
    assert removed == 1
    remaining = [r["sessionId"] for r in c.get_scan_sessions()]
    assert remaining == [keep]
    # session_data cascade-deleted with the session
    assert c.get_session_stats(drop)["sampleCount"] == 0


def test_delete_scans_empty_list_is_noop(tmp_path):
    db_path = str(tmp_path / "scans.db")
    _make_session(db_path, "20260612_092000_keep", 100.0, 105.0, 0xC3, 0xC3)
    c = _connector(tmp_path, db_path)
    assert c.deleteScans([]) == 0
    assert len(c.get_scan_sessions()) == 1
