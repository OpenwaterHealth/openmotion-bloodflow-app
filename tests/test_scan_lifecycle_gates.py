from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector

pytestmark = pytest.mark.unit


@pytest.fixture
def connector(tmp_path):
    iface = MagicMock()
    iface.console = MagicMock()
    iface.left = MagicMock()
    iface.right = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow = MagicMock()
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.start_configure_camera_sensors.return_value = True
    iface.start_scan.return_value = True

    c = MotionConnector(
        interface=iface,
        app_config={"engineeringMode": False},
        data_dir=str(tmp_path),
        config_dir="config",
    )
    c._consoleConnected = True
    c._leftSensorConnected = True
    c._rightSensorConnected = True
    return c


def test_start_configure_refuses_while_scan_workflow_running(connector):
    connector._scan_workflow.running = True
    seen = []
    connector.configFinished.connect(lambda ok, err: seen.append((ok, err)))

    ok = connector.startConfigureCameraSensors(0x66, 0x66)

    assert ok is False
    connector._interface.start_configure_camera_sensors.assert_not_called()
    assert seen == [(False, "Scan already running")]


def test_start_capture_refuses_while_scan_workflow_running(connector):
    connector._scan_workflow.running = True

    ok = connector.startCapture("subject", 5, 0x66, 0x66, False)

    assert ok is False
    connector._interface.start_scan.assert_not_called()


def test_is_pipeline_idle_mirrors_ensure_idle(connector):
    """QML scan-start gate polls isPipelineIdle(); it must flip false for
    every busy state _ensure_idle refuses on, and true once clear. The
    workflow.running case is the one that bit in the field: the pre-scan
    CQ check's worker keeps unwinding ~2 s after results are displayed."""
    assert connector.isPipelineIdle() is True

    connector._scan_workflow.running = True
    assert connector.isPipelineIdle() is False
    connector._scan_workflow.running = False

    connector._cq_quick_running = True
    assert connector.isPipelineIdle() is False
    connector._cq_quick_running = False

    connector._capture_running = True
    assert connector.isPipelineIdle() is False
    connector._capture_running = False

    connector._config_running = True
    assert connector.isPipelineIdle() is False
    connector._config_running = False

    assert connector.isPipelineIdle() is True


def test_is_config_in_flight_tracks_both_config_flags(connector):
    """Issue #283: a camera configuration (FPGA flash) can hold the
    pipeline for ~50 s. QML's start gate stretches its wait deadline
    while — and only while — a configuration is actually draining, so
    the probe must reflect both the connector-local flag and the SDK
    workflow's config_running."""
    assert connector.isConfigInFlight() is False

    connector._config_running = True
    assert connector.isConfigInFlight() is True
    connector._config_running = False

    connector._scan_workflow.config_running = True
    assert connector.isConfigInFlight() is True
    connector._scan_workflow.config_running = False

    # Other busy states are NOT a config in flight — the gate keeps its
    # short deadline for those.
    connector._capture_running = True
    assert connector.isConfigInFlight() is False
    connector._capture_running = False

    connector._cq_quick_running = True
    assert connector.isConfigInFlight() is False
    connector._cq_quick_running = False


def test_start_capture_refused_while_config_running(connector):
    """Issue #283's failure mode at the Python seam: a scan started while
    the FPGA flash is still in flight must be refused (the QML gate is
    what waits it out — the connector itself never queues)."""
    connector._config_running = True

    ok = connector.startCapture("subject", 5, 0x66, 0x66, False)

    assert ok is False
    connector._interface.start_scan.assert_not_called()


def test_start_configure_refused_while_config_running(connector):
    """A second configure while one is draining is refused with the
    canonical message QML used to surface raw to the user (issue #283)."""
    connector._config_running = True
    seen = []
    connector.configFinished.connect(lambda ok, err: seen.append((ok, err)))

    ok = connector.startConfigureCameraSensors(0x66, 0x66)

    assert ok is False
    connector._interface.start_configure_camera_sensors.assert_not_called()
    assert seen == [(False, "Camera configuration already in progress")]
