"""Unit tests for the connector's calibration/test outcome mapping.
No hardware — mirrors the fixture pattern in test_test_scan_flow.py."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.unit


@pytest.fixture
def connector(monkeypatch):
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


def _result(**kw):
    base = dict(ok=True, passed=True, canceled=False, error="",
                csv_path="c.csv", json_path="c.json", rows=[])
    base.update(kw)
    return SimpleNamespace(**base)


def test_outcome_field_wins_when_present(connector):
    connector._on_calibration_complete(
        _result(outcome="timed_out",
                error="calibration exceeded max_duration_sec=600",
                ok=False, passed=False, canceled=True))
    assert connector._calibration_status == "timed_out"
    assert "max_duration_sec" in connector.calibrationFailureReason


def test_error_outcome_surfaces_reason(connector):
    connector._on_calibration_complete(
        _result(outcome="error", ok=False, passed=False,
                error="flash phase failed: no sensor"))
    assert connector._calibration_status == "error"
    assert connector.calibrationFailureReason == "flash phase failed: no sensor"


def test_canceled_outcome(connector):
    connector._on_calibration_complete(
        _result(outcome="canceled", ok=False, passed=False,
                canceled=True, error="canceled during calibration scan"))
    assert connector._calibration_status == "canceled"


def test_compat_shim_old_sdk_no_outcome_attr(connector):
    # Pre-outcome SDK: canceled -> "canceled", plain error -> "error".
    connector._on_calibration_complete(
        _result(ok=False, passed=False, canceled=True, error="canceled"))
    assert connector._calibration_status == "canceled"
    connector._on_calibration_complete(
        _result(ok=False, passed=False, error="DegenerateCalibrationError: x"))
    assert connector._calibration_status == "error"


def test_passed_and_failed_unchanged(connector):
    connector._on_calibration_complete(_result(passed=True))
    assert connector._calibration_status == "passed"
    connector._on_calibration_complete(_result(passed=False))
    assert connector._calibration_status == "failed"


def _test_result(**kw):
    base = dict(ok=True, passed=True, canceled=False, error="",
                csv_path="t.csv", json_path="t.json", rows=[],
                test_scan_left_path="", test_scan_right_path="",
                started_timestamp="20260728_000000")
    base.update(kw)
    return SimpleNamespace(**base)


def test_test_scan_pass_status_is_passed_not_done(connector):
    connector._on_test_scan_complete(_test_result(passed=True))
    assert connector._test_scan_status == "passed"


def test_test_scan_error_outcome(connector):
    connector._on_test_scan_complete(
        _test_result(outcome="error", ok=False, passed=False,
                     error="flash phase failed: x"))
    assert connector._test_scan_status == "error"
    assert connector.testScanFailureReason == "flash phase failed: x"


def test_cancel_calibration_calls_sdk_nonblocking(connector):
    connector._calibration_status = "running"
    connector.cancelCalibration()
    connector._interface.cancel_calibration.assert_called_once_with(join_timeout=0)


def test_cancel_is_noop_when_idle(connector):
    connector.cancelCalibration()
    connector._interface.cancel_calibration.assert_not_called()


def test_failed_with_rollback_failure_surfaces_reason(connector):
    connector._app_config["engineeringMode"] = False
    connector._on_calibration_complete(
        _result(passed=False, error="rollback failed: usb gone"))
    assert connector._calibration_status == "failed"
    assert "rollback failed: usb gone" in connector.calibrationFailureReason


def test_outcome_enum_object_uses_value(connector):
    class _FakeEnum:
        value = "timed_out"
    connector._on_calibration_complete(
        _result(outcome=_FakeEnum(), ok=False, passed=False, canceled=True,
                error="calibration exceeded max_duration_sec=600"))
    assert connector._calibration_status == "timed_out"
