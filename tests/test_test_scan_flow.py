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
    """Build a MOTIONConnector against a fake MotionInterface.

    Uses pytest.mark.unit so the autouse HIL fixtures in conftest.py
    short-circuit and no app launch / panel-button calibration happens.
    """
    from motion_connector import MOTIONConnector

    fake_iface = MagicMock()
    fake_iface.console = MagicMock()
    fake_iface.left = MagicMock()
    fake_iface.right = MagicMock()
    fake_iface.is_device_connected.return_value = (True, True, True)
    fake_iface.start_test_scan.return_value = True
    fake_iface.start_calibration.return_value = True
    fake_iface.scan_workflow = MagicMock()

    c = MOTIONConnector(
        interface=fake_iface,
        app_config={"developerMode": False},
        output_path=".",
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
