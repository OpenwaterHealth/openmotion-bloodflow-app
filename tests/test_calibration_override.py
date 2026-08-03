"""Unit tests for the calibration pre-write gate (#426) and the
disconnected-target guards (#362). No hardware — mirrors the fixture
pattern in test_calibration_outcome_mapping.py.

The gate handler deliberately blocks the SDK's worker thread until the
operator answers, so the tests here answer from a separate thread the way
the GUI would.
"""
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.unit


@pytest.fixture
def connector():
    from motion_connector import MotionConnector
    fake_iface = MagicMock()
    fake_iface.is_device_connected.return_value = (True, True, True)
    c = MotionConnector(
        interface=fake_iface,
        app_config={"engineeringMode": True},
        data_dir=".",
        config_dir="config",
    )
    return c


def _row(cam_id=0, side="left", mean=32.0, contrast=0.08, bfi=0.4, bvi=1.1,
         dark=1.0, mean_test="FAIL", contrast_test="PASS", bfi_test="PASS",
         bvi_test="PASS", dark_test="PASS"):
    return SimpleNamespace(
        camera_index=cam_id, side=side, cam_id=cam_id, mean=mean,
        avg_contrast=contrast, bfi=bfi, bvi=bvi, dark=dark,
        mean_test=mean_test, contrast_test=contrast_test,
        bfi_test=bfi_test, bvi_test=bvi_test, dark_test=dark_test,
    )


def _answer_from_another_thread(connector, approve, delay=0.05):
    """Answer the gate the way the GUI thread would — from off the worker
    thread that is blocked inside _on_calibration_gate."""
    def _run():
        # Wait until the handler has actually parked before answering,
        # otherwise the test races the Event.
        for _ in range(200):
            if connector.calibrationOverridePending:
                break
            threading.Event().wait(0.005)
        if approve:
            connector.acceptCalibrationOverride()
        else:
            connector.dismissCalibrationOverride()
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# ── the gate itself ───────────────────────────────────────────────────────

def test_gate_returns_true_when_operator_approves(connector):
    spy = []
    connector.calibrationOverrideRequested.connect(lambda: spy.append(1))
    _answer_from_another_thread(connector, approve=True)

    answer = connector._on_calibration_gate([_row()])

    assert answer is True
    assert spy == [1], "modal was never raised"
    assert connector.calibrationOverridePending is False


def test_gate_returns_false_when_operator_declines(connector):
    _answer_from_another_thread(connector, approve=False)
    assert connector._on_calibration_gate([_row()]) is False
    assert connector.calibrationOverridePending is False


def test_gate_declines_when_nobody_answers(connector, monkeypatch):
    """An unattended run must not park a worker thread forever — and the
    safe default is to leave the console alone."""
    monkeypatch.setattr(connector, "_GATE_ANSWER_TIMEOUT_SEC", 0.1)
    assert connector._on_calibration_gate([_row()]) is False


def test_cancel_releases_a_blocked_gate(connector):
    """cancel_calibration only sets the SDK's stop event, which a worker
    parked on the gate cannot observe — Cancel must unblock it directly."""
    connector._calibration_status = "running"

    def _cancel_soon():
        for _ in range(200):
            if connector.calibrationOverridePending:
                break
            threading.Event().wait(0.005)
        connector.cancelCalibration()
    threading.Thread(target=_cancel_soon, daemon=True).start()

    assert connector._on_calibration_gate([_row()]) is False
    connector._interface.cancel_calibration.assert_called_once()


def test_gate_writes_nothing_itself(connector):
    """The whole point of the gate: the console is untouched while the
    question is open, and the app never writes — the SDK does, after."""
    _answer_from_another_thread(connector, approve=True)
    connector._on_calibration_gate([_row()])
    connector._interface.write_calibration.assert_not_called()


def test_answering_when_no_gate_is_open_is_a_no_op(connector):
    connector.acceptCalibrationOverride()
    connector.dismissCalibrationOverride()
    assert connector.calibrationOverridePending is False


def test_gate_handler_is_passed_to_the_sdk(connector):
    connector._consoleConnected = True
    connector._leftSensorConnected = True
    connector._rightSensorConnected = True

    connector.runCalibration("both")

    kwargs = connector._interface.start_calibration.call_args.kwargs
    assert kwargs["on_confirm_fn"] == connector._on_calibration_gate


# ── result mapping ────────────────────────────────────────────────────────

def _failed(rows, **kw):
    base = dict(ok=True, passed=False, canceled=False, error="",
                csv_path="c.csv", json_path="c.json", rows=rows,
                outcome="failed", calibration=None,
                consented_below_threshold=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_consented_run_reports_overridden_not_failed(connector):
    """A consented write really is on the console — a bare "Failed" would
    read as though nothing was kept."""
    connector._on_calibration_complete(
        _failed([_row()], consented_below_threshold=True))
    assert connector._calibration_status == "overridden"


def test_unconsented_failure_stays_failed(connector):
    connector._on_calibration_complete(_failed([_row()]))
    assert connector._calibration_status == "failed"


def test_declined_gate_surfaces_as_canceled_with_reason(connector):
    """The SDK reports a declined gate as a cancel carrying the reason."""
    connector._on_calibration_complete(_failed(
        [_row()], ok=False, canceled=True, outcome="canceled",
        error="declined at pre-write gate: calibration scan below "
              "threshold on L1"))
    assert connector._calibration_status == "canceled"
    assert "pre-write gate" in connector.calibrationFailureReason


def test_passed_run_is_untouched_by_the_gate_mapping(connector):
    connector._on_calibration_complete(
        _failed([_row(mean_test="PASS")], passed=True, outcome="passed"))
    assert connector._calibration_status == "passed"


# ── gate table formatting ─────────────────────────────────────────────────

def test_gate_rows_pair_measurements_with_their_limits(connector):
    from omotion import CalibrationThresholds
    connector._calibration_thresholds = CalibrationThresholds(
        min_mean_per_camera=[40.0] * 8,
        min_contrast_per_camera=[0.06] * 8,
        min_bfi_per_camera=[0.0] * 8,
        min_bvi_per_camera=[0.0] * 8,
        max_bfi_per_camera=[5.0] * 8,
        max_bvi_per_camera=None,
        max_dark_per_camera=[3.0] * 8,
    )
    _answer_from_another_thread(connector, approve=False)
    connector._on_calibration_gate(
        [_row(cam_id=1, side="right", mean=32.4)])

    row = connector.calibrationOverrideRows[0]
    assert row["cam"] == 2 and row["side"] == "R"
    assert row["mean"] == "32.40"
    assert row["meanLimit"] == "≥ 40.00"
    assert row["meanFail"] is True
    assert row["contrastFail"] is False
    # Two-sided band renders as a range; one-sided as ≥.
    assert row["bfiLimit"] == "0.000–5.000"
    assert row["bviLimit"] == "≥ 0.000"


def test_gate_rows_render_nan_and_missing_limits(connector):
    connector._calibration_thresholds = None
    _answer_from_another_thread(connector, approve=False)
    connector._on_calibration_gate([_row(mean=float("nan"))])
    row = connector.calibrationOverrideRows[0]
    assert row["mean"] == "--"
    assert row["meanLimit"] == "--"


# ── #362: disconnected calibration targets ────────────────────────────────

def test_calibrate_disconnected_target_reports_persistently(connector):
    """#362 — the refusal must reach the persistent status label, not just
    the transient capture log the Settings operator never sees."""
    connector._consoleConnected = True
    connector._leftSensorConnected = False
    connector._rightSensorConnected = True

    connector.runCalibration("left")

    assert connector._calibration_status == "error"
    assert connector.calibrationFailureReason == "left sensor not connected"
    connector._interface.start_calibration.assert_not_called()


def test_test_scan_disconnected_target_reports_persistently(connector):
    connector._consoleConnected = True
    connector._leftSensorConnected = True
    connector._rightSensorConnected = False

    connector.runTestScan("right")

    assert connector._test_scan_status == "error"
    assert connector.testScanFailureReason == "right sensor not connected"


def test_calibrate_connected_target_still_starts(connector):
    connector._consoleConnected = True
    connector._leftSensorConnected = False
    connector._rightSensorConnected = True

    connector.runCalibration("right")

    assert connector._interface.start_calibration.called
