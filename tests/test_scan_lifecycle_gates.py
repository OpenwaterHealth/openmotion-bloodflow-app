from unittest.mock import MagicMock

import pytest

from motion_connector import MOTIONConnector

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

    c = MOTIONConnector(
        interface=iface,
        app_config={"developerMode": False},
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


def test_start_capture_refuses_while_scan_workflow_running(connector, tmp_path):
    connector._scan_workflow.running = True

    ok = connector.startCapture("subject", 5, 0x66, 0x66, str(tmp_path), False)

    assert ok is False
    connector._interface.start_scan.assert_not_called()
