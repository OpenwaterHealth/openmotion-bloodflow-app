from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector

pytestmark = pytest.mark.unit


@pytest.fixture
def connector(tmp_path):
    iface = MagicMock()
    iface.scan_db_path = None  # a MagicMock path becomes a DB file in cwd (#620)
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


# --- #342: camera power-on refusal at configure time -> E-105 ---------------

from types import SimpleNamespace  # noqa: E402

from motion_connector import _camera_power_failure_side  # noqa: E402

# Exact shape of the SDK error (ScanWorkflow.start_configure_camera_sensors
# wraps the power-on RuntimeError in "Error setting camera power for ...").
_SDK_POWER_ERR = ("Error setting camera power for right: Failed to power on "
                  "cameras on right (mask 0x99).")


@pytest.mark.parametrize("err, side", [
    (_SDK_POWER_ERR, "right"),
    ("Failed to power on cameras on left (mask 0xFF).", "left"),
    # The wrapper prefix alone also wraps comm exceptions: not a power fault.
    ("Error setting camera power for left: USB timeout", None),
    ("Empty camera masks (left & right)", None),
    ("Canceled", None),
    ("", None),
    (None, None),
])
def test_camera_power_failure_side(err, side):
    assert _camera_power_failure_side(err) == side


def _finish_config(connector, ok, error):
    critical, finished = [], []
    connector.criticalErrorRaised.connect(
        lambda code, *rest: critical.append((code, rest[-1])))
    connector.configFinished.connect(lambda o, e: finished.append((o, e)))
    connector._config_running = True
    connector._on_config_finished(SimpleNamespace(ok=ok, error=error))
    return critical, finished


def test_config_power_on_refusal_raises_e105(connector):
    critical, finished = _finish_config(connector, False, _SDK_POWER_ERR)

    assert [c[0] for c in critical] == ["E-105"]
    assert critical[0][1].startswith("right sensor")
    # The scan flow still gets its failure so the runner unwinds.
    assert finished == [(False, _SDK_POWER_ERR)]


def test_config_other_failure_does_not_raise_e105(connector):
    critical, finished = _finish_config(
        connector, False, "FPGA program failed on left camera 3")

    assert critical == []
    assert finished == [(False, "FPGA program failed on left camera 3")]


def test_config_success_does_not_raise(connector):
    critical, finished = _finish_config(connector, True, "")

    assert critical == []
    assert finished == [(True, "")]
