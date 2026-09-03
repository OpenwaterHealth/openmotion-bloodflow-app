# tests/test_scan_outcome.py
from types import SimpleNamespace

import pytest
from omotion.pipeline.batch import TerminalDarkResult

from motion_connector import ScanOutcome, classify_scan_outcome, _ScanOutcomeSink

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


# ── scan_ended audit termination record (issue #535) ─────────────────────
# The audit event must name WHY a scan ended, independent of the data
# verdict above: every app-side abort cancels the SDK scan, so `canceled`
# alone cannot tell a fault from an operator Stop.

from motion_connector import audit_scan_ended_details  # noqa: E402

_AUDIT_KEYS = {"outcome", "data", "error_code", "reason"}


def test_audit_e303_after_data_is_aborted_not_ok():
    # Bench case from #535: stall watchdog fired after the first interval
    # closed → classifier says "ok"; the audit record must say aborted.
    d = audit_scan_ended_details(
        outcome=ScanOutcome("ok", "", ""), canceled=True,
        abort_code="E-303", abort_reason="no camera frames for 4 s",
    )
    assert d["outcome"] == "aborted"
    assert d["error_code"] == "E-303"
    assert d["reason"] == "no camera frames for 4 s"
    assert d["data"] == "ok"
    assert set(d) == _AUDIT_KEYS


def test_audit_e304_before_data_is_aborted_not_skipped():
    d = audit_scan_ended_details(
        outcome=ScanOutcome("skipped", "", ""), canceled=True,
        abort_code="E-304", abort_reason="left disconnected mid-scan",
    )
    assert d["outcome"] == "aborted"
    assert d["error_code"] == "E-304"
    assert d["data"] == "skipped"


def test_audit_operator_stop_before_data_is_stopped():
    # Same classifier verdict as the E-304 case above, no fault → the two
    # are now distinguishable in the audit trail.
    d = audit_scan_ended_details(
        outcome=ScanOutcome("skipped", "", ""), canceled=True,
        abort_code=None,
    )
    assert d["outcome"] == "stopped"
    assert d["error_code"] is None
    assert d["reason"] is None
    assert d["data"] == "skipped"


def test_audit_clean_completion_is_ok():
    d = audit_scan_ended_details(
        outcome=ScanOutcome("ok", "", ""), canceled=False, abort_code=None,
    )
    assert d == {"outcome": "ok", "data": "ok",
                 "error_code": None, "reason": None}


def test_audit_classifier_flagged_interruption_without_code_is_aborted():
    # SDK tore the scan down before any app-side abort path claimed it:
    # classify_scan_outcome flags the unexpected end → aborted, no code,
    # the classifier's message as the reason.
    out = classify_scan_outcome(
        final_frames=0, terminal_dark_missing=False,
        canceled=False, disable_laser=False,
    )
    d = audit_scan_ended_details(outcome=out, canceled=False, abort_code=None)
    assert d["outcome"] == "aborted"
    assert d["error_code"] is None
    assert d["reason"] == out.message
    assert d["data"] == "empty"


def test_audit_classification_failure_still_records_termination():
    d = audit_scan_ended_details(outcome=None, canceled=True, abort_code=None)
    assert d["outcome"] == "stopped"
    assert d["data"] is None
    d = audit_scan_ended_details(outcome=None, canceled=True,
                                 abort_code="E-202", abort_reason="trip")
    assert d["outcome"] == "aborted"
    assert d["error_code"] == "E-202"


def test_audit_empty_abort_reason_is_null():
    d = audit_scan_ended_details(
        outcome=ScanOutcome("ok", "", ""), canceled=True,
        abort_code="E-303", abort_reason="",
    )
    assert d["reason"] is None
