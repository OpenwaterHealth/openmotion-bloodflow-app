"""Unit tests for the test-scan slot path (#132). No hardware — fakes
the interface so we exercise just the connector's state machine + the
result-to-rows translation."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# Repo root holds motion_connector.py — make it importable without
# turning the project into an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.unit


@pytest.fixture
def connector(monkeypatch):
    """Build a MotionConnector against a fake MotionInterface.

    Uses pytest.mark.unit so the autouse HIL fixtures in conftest.py
    short-circuit and no app launch / panel-button calibration happens.
    """
    from motion_connector import MotionConnector

    fake_iface = MagicMock()
    fake_iface.console = MagicMock()
    fake_iface.left = MagicMock()
    fake_iface.right = MagicMock()
    fake_iface.is_device_connected.return_value = (True, True, True)
    fake_iface.start_test_scan.return_value = True
    fake_iface.start_calibration.return_value = True
    fake_iface.scan_workflow = MagicMock()

    c = MotionConnector(
        interface=fake_iface,
        app_config={"developerMode": False},
        data_dir=".",
        config_dir="config",
    )
    c._consoleConnected = True
    c._leftSensorConnected = True
    c._rightSensorConnected = True
    return c


def test_initial_test_scan_state_is_idle(connector):
    assert connector.testScanRunning is False
    assert connector.testScanStatus == ""
    assert connector.testScanFailureReason == ""
    assert connector.testScanRows == []


def test_run_test_scan_refused_when_console_disconnected(connector):
    connector._consoleConnected = False
    seen = []
    connector.captureLog.connect(lambda m: seen.append(m))
    connector.runTestScan("both")
    assert connector._test_scan_status == ""  # unchanged
    assert any("console not connected" in m for m in seen)


def test_run_test_scan_refused_when_calibration_running(connector):
    connector._calibration_status = "running"
    seen = []
    connector.captureLog.connect(lambda m: seen.append(m))
    connector.runTestScan("both")
    assert connector._test_scan_status == ""


def test_run_test_scan_starts_workflow(connector):
    connector.runTestScan("both")
    assert connector._test_scan_status == "running"
    connector._interface.start_test_scan.assert_called_once()


def test_on_test_scan_complete_passes_builds_rows(connector):
    """Synthesise a passing TestScanResult and confirm row dicts + status."""
    from omotion.CalibrationWorkflow import (
        CalibrationResultRow,
        TestScanResult,
    )

    rows = [
        CalibrationResultRow(
            camera_index=0, side="left", cam_id=0,
            mean=120.0, avg_contrast=0.30, bfi=0.0, bvi=4.5,
            dark=1.0, mean_test="PASS", contrast_test="PASS",
            bfi_test="PASS", bvi_test="PASS", dark_test="PASS",
            security_id="", hwid="",
        ),
    ]
    res = TestScanResult(
        ok=True, passed=True, canceled=False, error="",
        csv_path="/tmp/x.csv", json_path="/tmp/x.json",
        rows=rows, test_scan_left_path="", test_scan_right_path="",
        started_timestamp="20260521_000000",
    )
    connector._on_test_scan_complete(res)
    assert connector._test_scan_status == "done"
    assert len(connector._test_scan_rows) == 1
    row = connector._test_scan_rows[0]
    assert row["side"] == "left"
    assert row["cam"] == 1
    assert row["light_mean"] == 120.0
    assert row["mean_pf"] == "PASS"
    assert row["dark_pf"] == "PASS"
    assert row["overall"] == "PASS"


def test_on_test_scan_complete_dev_mode_failure_reason(connector):
    from omotion.CalibrationWorkflow import (
        CalibrationResultRow,
        TestScanResult,
    )

    connector._app_config["developerMode"] = True
    rows = [
        CalibrationResultRow(
            camera_index=0, side="left", cam_id=0,
            mean=120.0, avg_contrast=0.30, bfi=0.0, bvi=4.5,
            dark=10.0,
            mean_test="PASS", contrast_test="PASS",
            bfi_test="PASS", bvi_test="PASS",
            dark_test="FAIL",
            security_id="", hwid="",
        ),
    ]
    res = TestScanResult(
        ok=True, passed=False, canceled=False, error="",
        csv_path="/tmp/x.csv", json_path="/tmp/x.json",
        rows=rows, test_scan_left_path="", test_scan_right_path="",
        started_timestamp="20260521_000000",
    )
    connector._on_test_scan_complete(res)
    assert connector._test_scan_status == "failed"
    assert connector._test_scan_rows[0]["overall"] == "FAIL"
    assert connector._test_scan_failure_reason.startswith("too much ambient light")
