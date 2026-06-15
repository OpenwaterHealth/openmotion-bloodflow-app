# tests/test_scan_outcome.py
from types import SimpleNamespace

import pytest
from omotion.pipeline.batch import TerminalDarkResult

from scan_outcome import ScanOutcome, classify_scan_outcome, _ScanOutcomeSink

pytestmark = pytest.mark.unit


def test_clean_scan_no_alert():
    out = classify_scan_outcome(
        final_frames=5000, terminal_dark_missing=False,
        canceled=False, disable_laser=False,
    )
    assert out.kind == "ok"
    assert out.severity == ""
    assert out.message == ""


def test_clean_user_cancel_no_alert():
    out = classify_scan_outcome(
        final_frames=800, terminal_dark_missing=False,
        canceled=True, disable_laser=False,
    )
    assert out.kind == "ok"


def test_disconnect_after_data_is_partial_warning():
    out = classify_scan_outcome(
        final_frames=800, terminal_dark_missing=True,
        canceled=False, disable_laser=False,
    )
    assert out.kind == "partial"
    assert out.severity == "warning"
    assert "partial" in out.message.lower()


def test_disconnect_before_any_data_is_empty_error():
    out = classify_scan_outcome(
        final_frames=0, terminal_dark_missing=True,
        canceled=False, disable_laser=False,
    )
    assert out.kind == "empty"
    assert out.severity == "error"
    assert "not saved" in out.message.lower()


def test_empty_user_cancel_is_silent():
    out = classify_scan_outcome(
        final_frames=0, terminal_dark_missing=False,
        canceled=True, disable_laser=False,
    )
    assert out.kind == "skipped"
    assert out.severity == ""


def test_laser_disabled_scan_never_alerts():
    out = classify_scan_outcome(
        final_frames=0, terminal_dark_missing=True,
        canceled=False, disable_laser=True,
    )
    assert out.kind == "skipped"
    assert out.severity == ""


def test_partial_suppressed_when_user_canceled():
    # A user-initiated stop that happens to miss a terminal dark on the
    # final partial interval is still a normal cancel, not an alarm.
    out = classify_scan_outcome(
        final_frames=800, terminal_dark_missing=True,
        canceled=True, disable_laser=False,
    )
    assert out.kind == "ok"


def test_sink_counts_final_frames():
    sink = _ScanOutcomeSink()
    sink.on_scan_start(meta=None)
    sink.consume("final", SimpleNamespace(frames=[object(), object()]))
    sink.consume("final", SimpleNamespace(frames=[object()]))
    assert sink.final_frames == 3
    assert sink.terminal_dark_missing is False


def test_sink_flags_missing_terminal_dark():
    sink = _ScanOutcomeSink()
    sink.on_scan_start(meta=None)
    sink.consume("diagnostics", TerminalDarkResult(
        side="left", cam_id=0, abs_frame_id=339,
        u1=171.6, threshold=133.0, found=False, identified_by="content",
    ))
    assert sink.terminal_dark_missing is True


def test_sink_ignores_present_terminal_dark():
    sink = _ScanOutcomeSink()
    sink.on_scan_start(meta=None)
    sink.consume("diagnostics", TerminalDarkResult(
        side="left", cam_id=0, abs_frame_id=49,
        u1=127.3, threshold=133.0, found=True, identified_by="fsync",
    ))
    assert sink.terminal_dark_missing is False


def test_sink_resets_on_scan_start():
    sink = _ScanOutcomeSink()
    sink.final_frames = 99
    sink.terminal_dark_missing = True
    sink.on_scan_start(meta=None)
    assert sink.final_frames == 0
    assert sink.terminal_dark_missing is False
