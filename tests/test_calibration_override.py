"""Unit tests for the below-threshold calibration override (#426) and the
disconnected-target guards (#362). No hardware — mirrors the fixture
pattern in test_calibration_outcome_mapping.py."""
import sys
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


def _cal():
    return SimpleNamespace(c_min=[[0]], c_max=[[1]], i_min=[[2]], i_max=[[3]],
                           source="console")


def _failed(rows, calibration=None, **kw):
    base = dict(ok=True, passed=False, canceled=False, error="",
                csv_path="c.csv", json_path="c.json", rows=rows,
                outcome="failed", calibration=calibration)
    base.update(kw)
    return SimpleNamespace(**base)


# ── eligibility ───────────────────────────────────────────────────────────

def test_low_mean_failure_offers_override(connector):
    spy = []
    connector.calibrationOverrideRequested.connect(lambda: spy.append(1))
    connector._on_calibration_complete(_failed([_row()], _cal()))
    assert connector._calibration_status == "failed"
    assert connector.calibrationOverridePending is True
    assert spy == [1]


def test_ambient_dark_failure_blocks_override(connector):
    """A dark FAIL is room light leaking in, not a dim laser — never
    overridable, even when the mean is also low."""
    spy = []
    connector.calibrationOverrideRequested.connect(lambda: spy.append(1))
    connector._on_calibration_complete(
        _failed([_row(dark_test="FAIL")], _cal()))
    assert connector._calibration_status == "failed"
    assert connector.calibrationOverridePending is False
    assert spy == []


def test_one_dark_failure_among_many_blocks_override(connector):
    rows = [_row(cam_id=0), _row(cam_id=1), _row(cam_id=2, dark_test="FAIL")]
    connector._on_calibration_complete(_failed(rows, _cal()))
    assert connector.calibrationOverridePending is False


def test_contrast_bfi_bvi_failures_are_overridable(connector):
    """Scope decision on #426: any failing test except ambient-dark. A dim
    laser drags the light-derived metrics down along with mean."""
    rows = [_row(mean_test="PASS", contrast_test="FAIL",
                 bfi_test="FAIL", bvi_test="FAIL")]
    connector._on_calibration_complete(_failed(rows, _cal()))
    assert connector.calibrationOverridePending is True


def test_no_calibration_object_means_no_offer(connector):
    """Nothing to write back, so nothing to offer."""
    connector._on_calibration_complete(_failed([_row()], None))
    assert connector.calibrationOverridePending is False


def test_passed_run_offers_nothing(connector):
    connector._on_calibration_complete(
        _failed([_row(mean_test="PASS")], _cal(), passed=True,
                outcome="passed"))
    assert connector._calibration_status == "passed"
    assert connector.calibrationOverridePending is False


def test_canceled_run_offers_nothing(connector):
    connector._on_calibration_complete(
        _failed([_row()], _cal(), ok=False, canceled=True,
                outcome="canceled", error="canceled"))
    assert connector.calibrationOverridePending is False


# ── accept / discard ──────────────────────────────────────────────────────

def test_accept_writes_calibration_and_marks_overridden(connector):
    cal = _cal()
    connector._on_calibration_complete(_failed([_row()], cal))
    connector.acceptCalibrationOverride()

    connector._interface.write_calibration.assert_called_once_with(
        cal.c_min, cal.c_max, cal.i_min, cal.i_max)
    # Never "passed" — the run did not meet its thresholds (#412's premise).
    assert connector._calibration_status == "overridden"
    assert "below threshold" in connector.calibrationFailureReason
    assert "L1" in connector.calibrationFailureReason
    assert connector.calibrationOverridePending is False


def test_accept_write_failure_keeps_failed_and_says_so(connector):
    connector._interface.write_calibration.side_effect = RuntimeError("usb")
    connector._on_calibration_complete(_failed([_row()], _cal()))
    connector.acceptCalibrationOverride()

    assert connector._calibration_status == "failed"
    assert "override write failed" in connector.calibrationFailureReason
    assert "previous calibration" in connector.calibrationFailureReason
    assert connector.calibrationOverridePending is False


def test_discard_leaves_console_untouched(connector):
    connector._on_calibration_complete(_failed([_row()], _cal()))
    connector.dismissCalibrationOverride()

    connector._interface.write_calibration.assert_not_called()
    assert connector._calibration_status == "failed"
    assert connector.calibrationOverridePending is False


def test_accept_is_idempotent_with_nothing_pending(connector):
    connector.acceptCalibrationOverride()
    connector.dismissCalibrationOverride()
    connector._interface.write_calibration.assert_not_called()


def test_new_result_clears_a_stale_pending_override(connector):
    connector._on_calibration_complete(_failed([_row()], _cal()))
    assert connector.calibrationOverridePending is True
    connector._on_calibration_complete(
        _failed([_row(mean_test="PASS")], _cal(), passed=True,
                outcome="passed"))
    assert connector.calibrationOverridePending is False


# ── override table formatting ─────────────────────────────────────────────

def test_override_rows_pair_measurements_with_their_limits(connector):
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
    connector._on_calibration_complete(
        _failed([_row(cam_id=1, side="right", mean=32.4)], _cal()))

    row = connector.calibrationOverrideRows[0]
    assert row["cam"] == 2 and row["side"] == "R"
    assert row["mean"] == "32.40"
    assert row["meanLimit"] == "≥ 40.00"
    assert row["meanFail"] is True
    assert row["contrastFail"] is False
    # Two-sided band renders as a range; one-sided as ≥.
    assert row["bfiLimit"] == "0.000–5.000"
    assert row["bviLimit"] == "≥ 0.000"


def test_override_rows_render_nan_and_missing_limits(connector):
    connector._calibration_thresholds = None
    connector._on_calibration_complete(
        _failed([_row(mean=float("nan"))], _cal()))
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
