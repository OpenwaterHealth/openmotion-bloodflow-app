# tests/test_scan_outcome.py
import pytest

from scan_outcome import ScanOutcome, classify_scan_outcome

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
