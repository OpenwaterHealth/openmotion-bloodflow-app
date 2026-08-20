"""History data-management connector slots — DB-only, no hardware."""

import datetime
import os
import time
from unittest.mock import MagicMock

import pytest

from motion_connector import (
    MotionConnector,
    _config_name,
    _sdk_flags_from_session,
)
from data_sources import _CameraBuffer
from omotion.ScanDatabase import ScanDatabase

pytestmark = pytest.mark.unit


def _local_display(ts):
    utc_dt = datetime.datetime.strptime(ts, "%Y%m%d_%H%M%S").replace(
        tzinfo=datetime.timezone.utc
    )
    return utc_dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _connector(tmp_path, scan_db_path):
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = scan_db_path
    return MotionConnector(
        interface=iface, app_config={"engineeringMode": False},
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


def test_friendly_ts_converts_utc_to_local_time(monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is unavailable on this platform")

    old_tz = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", "America/New_York")
        time.tzset()

        assert MotionConnector._friendly_ts(
            "20260612_093100"
        ) == "2026-06-12 05:31:00"
    finally:
        if old_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", old_tz)
        time.tzset()

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
    assert top["dateTime"] == _local_display("20260612_093100")


# ── Issue #335 — History duration must be the actual trigger-ON scan time ──
# session_end - session_start brackets the whole pipeline lifetime (pre-trigger
# setup + post-trigger USB drain), ~3 s longer than the laser-on window. The
# completion handler stamps the trigger-ON duration into
# session_meta.actual_duration_sec; _session_to_row must prefer it.


def test_session_to_row_prefers_actual_duration_from_meta(tmp_path):
    c = _connector(tmp_path, str(tmp_path / "scans.db"))
    # Wall-clock window is 123.2 s (renders "2:03"); the actual trigger-ON
    # duration the app recorded is 120.02 s ("2:00").
    session = {
        "id": 1,
        "session_label": "20260708_114526_ow2P964L",
        "session_start": 1000.0,
        "session_end": 1123.2,
        "session_notes": None,
        "session_meta": {
            "subject_id": "ow2P964L",
            "actual_duration_sec": 120.02,
            "sdk_flags": {"reduced_mode": True,
                          "left_camera_mask": 0x66, "right_camera_mask": 0x66},
        },
    }
    row = c._session_to_row(session)
    assert row["durationSec"] == pytest.approx(120.02)


def test_session_to_row_falls_back_to_wall_clock_without_actual_duration(tmp_path):
    """Old rows written before the fix have no actual_duration_sec, so
    _session_to_row keeps the wall-clock delta (backward compatible)."""
    c = _connector(tmp_path, str(tmp_path / "scans.db"))
    session = {
        "id": 1, "session_label": "20260101_000000_s",
        "session_start": 1000.0, "session_end": 1015.0,
        "session_notes": None, "session_meta": {"sdk_flags": {}},
    }
    row = c._session_to_row(session)
    assert row["durationSec"] == 15.0


def test_persist_scan_notes_stamps_actual_duration(tmp_path):
    """The completion handler passes the trigger-ON elapsed to
    _persist_scan_notes, which merges actual_duration_sec into session_meta
    without clobbering the existing meta (sdk_flags, diagnostics)."""
    db_path = str(tmp_path / "scans.db")
    _make_session(db_path, "20260708_114526_ow2P964L", 1000.0, 1123.2,
                  0x66, 0x66, subject="ow2P964L")
    c = _connector(tmp_path, db_path)
    c._scan_notes = "some notes"
    assert c._persist_scan_notes("20260708_114526_ow2P964L",
                                 actual_duration_sec=120.02) is True

    # Read back through the History row: it must now show the actual duration,
    # and the pre-existing sdk_flags must survive the merge.
    row = c.get_scan_sessions()[0]
    assert row["durationSec"] == pytest.approx(120.02)
    assert row["leftMask"] == 0x66 and row["rightMask"] == 0x66
    assert row["notes"] == "some notes"


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


def test_stats_and_delete_without_db_are_safe(tmp_path):
    """No scan DB configured → both slots no-op rather than raise."""
    c = _connector(tmp_path, None)
    assert c.get_session_stats(1) == {"sampleCount": 0}
    assert c.deleteScans([1, 2]) == 0


# ── Issue #175 reopen — replay uses the recorded config, not the data shape ──
# The History list already reads sdk_flags; the plot viewer must too. These
# cover the connector wiring: extracting sdk_flags from the session row, then
# threading it into the PastScanSource the viewer binds to.


def test_sdk_flags_from_session_reads_meta_block(tmp_path):
    db_path = str(tmp_path / "scans.db")
    _make_session(db_path, "20260101_000000_subjA", 100.0, 110.0,
                  left_mask=0xFF, right_mask=0xC3, reduced=False)
    db = ScanDatabase(db_path=db_path)
    try:
        session = db.get_session_by_label("20260101_000000_subjA")
    finally:
        db.close()
    flags = _sdk_flags_from_session(session)
    assert flags["left_camera_mask"] == 0xFF
    assert flags["right_camera_mask"] == 0xC3
    assert flags["reduced_mode"] is False


def test_sdk_flags_from_session_none_when_absent_or_unparseable():
    import json as _json
    assert _sdk_flags_from_session(None) is None
    assert _sdk_flags_from_session({}) is None
    assert _sdk_flags_from_session({"session_meta": {}}) is None
    assert _sdk_flags_from_session({"session_meta": "not json"}) is None
    # A raw JSON string for session_meta is tolerated and parsed.
    raw = {"session_meta": _json.dumps(
        {"sdk_flags": {"left_camera_mask": 0x09, "right_camera_mask": 0x09}})}
    assert _sdk_flags_from_session(raw)["left_camera_mask"] == 0x09


def _middle_four_buffers():
    """middle-4 cams per side (camIds 1,2,5,6 → derived mask 0x66)."""
    buffers = {}
    for side in ("left", "right"):
        for cam_id in (1, 2, 5, 6):
            buf = _CameraBuffer(max_capacity=None)
            buf.append(t=0.0, v=1.0, frame_id=0)
            buffers[(side, cam_id, "bfi")] = buf
    return buffers


def test_load_past_scan_slot_lays_grid_from_recorded_config(tmp_path):
    """The GUI-thread slot must thread the scan's recorded sdk_flags into the
    PastScanSource so an All/All scan whose data is only middle-4 still reports
    All/All masks — the viewer then renders all 16 configured cells."""
    c = _connector(tmp_path, str(tmp_path / "scans.db"))
    c._on_past_scan_buffers_ready(
        c._past_scan_load_seq, "20260101_000000_subjA", 1,
        _middle_four_buffers(), "db",
        {"left_camera_mask": 0xFF, "right_camera_mask": 0xFF,
         "reduced_mode": False},
    )
    src = c.currentScanSource
    assert src is not None
    assert src.leftMask == 0xFF
    assert src.rightMask == 0xFF


def test_load_past_scan_slot_without_flags_falls_back_to_derived(tmp_path):
    """Older scans deliver no flags (None) — the slot still binds a source,
    derived from the data (backward compatibility)."""
    c = _connector(tmp_path, str(tmp_path / "scans.db"))
    c._on_past_scan_buffers_ready(
        c._past_scan_load_seq, "20260101_000000_subjA", 1,
        _middle_four_buffers(), "db", None,
    )
    src = c.currentScanSource
    assert src is not None
    assert src.leftMask == 0x66
    assert src.rightMask == 0x66


# ── Issue #245 — viewer "Viewing" badge: thread the scan's identity to source ──
# The viewer reads userLabel/dateTime off the bound source to render its badge.
# The connector must (a) compute those in the worker from the same session row
# the History list shows, and (b) thread them through the GUI-thread slot into
# the PastScanSource.


def test_load_past_scan_slot_threads_display_meta_to_source(tmp_path):
    """The GUI-thread slot must pass the worker's display_meta into the
    PastScanSource so the viewer badge can name the scan (issue #245)."""
    c = _connector(tmp_path, str(tmp_path / "scans.db"))
    c._on_past_scan_buffers_ready(
        c._past_scan_load_seq, "20260623_111935_PatientA", 1,
        _middle_four_buffers(), "db", None,
        {"userLabel": "Patient A", "dateTime": "2026-06-23 11:19:35"},
    )
    src = c.currentScanSource
    assert src is not None
    assert src.userLabel == "Patient A"
    assert src.dateTime == "2026-06-23 11:19:35"


def test_load_past_scan_slot_missing_display_meta_leaves_label_blank(tmp_path):
    """No display_meta (older worker path / failed lookup) → source label is
    blank, so the viewer simply hides the badge. Backward compatible with the
    existing 6-arg slot calls."""
    c = _connector(tmp_path, str(tmp_path / "scans.db"))
    c._on_past_scan_buffers_ready(
        c._past_scan_load_seq, "20260101_000000_subjA", 1,
        _middle_four_buffers(), "db", None,
    )
    src = c.currentScanSource
    assert src is not None
    assert src.userLabel == ""
    assert src.dateTime == ""


def test_load_past_scan_worker_emits_display_meta_from_session(tmp_path):
    """The worker derives the badge's userLabel/dateTime from the same session
    row the History list renders (prefers session_meta.subject_id over the label
    suffix), so the badge can never disagree with the clicked row."""
    db_path = str(tmp_path / "scans.db")
    sid = _make_session(
        db_path, "20260623_111935_PatientA", 100.0, 110.0,
        left_mask=0xFF, right_mask=0xFF, reduced=False,
        subject="Patient A",   # subject_id differs from the label suffix
    )
    _insert_rows(db_path, sid, 3)
    c = _connector(tmp_path, db_path)

    captured = []
    c._pastScanBuffersReady.connect(lambda *a: captured.append(a))
    c._load_past_scan_worker(
        c._past_scan_load_seq, sid, "20260623_111935_PatientA", db_path,
        None)

    assert captured, "worker did not emit _pastScanBuffersReady"
    display_meta = captured[-1][-1]
    assert display_meta["userLabel"] == "Patient A"
    assert display_meta["dateTime"] == _local_display("20260623_111935")
