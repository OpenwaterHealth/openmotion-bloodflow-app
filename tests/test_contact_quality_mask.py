"""Unit tests for issue: cameras set to "None" (deselected from the
configured scan mask) must not be required to pass the contact-quality
check. No hardware — fakes the interface and runs the CQ worker thread
synchronously so we can assert on the exact mask handed to the SDK."""
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.unit


class _SyncThread:
    """Stand-in for threading.Thread that runs the target synchronously on
    start(), so the CQ worker's SDK call can be asserted on without racing
    a real background thread."""

    def __init__(self, target=None, args=(), kwargs=None, **_kw):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)

    def join(self, *a, **kw):
        pass


@pytest.fixture
def connector(monkeypatch):
    from motion_connector import MotionConnector

    fake_iface = MagicMock()
    fake_iface.console = MagicMock()
    fake_iface.left = MagicMock()
    fake_iface.right = MagicMock()
    fake_iface.is_device_connected.return_value = (True, True, True)
    fake_iface.scan_workflow = MagicMock()
    fake_iface.scan_workflow.running = False
    fake_iface.scan_workflow.config_running = False
    fake_iface.contact_quality_workflow = MagicMock()
    # A bare MagicMock result crashes _on_cq_result_ready's per-camera loop
    # (result.per_camera.get(...) returns a truthy Mock instead of None, so
    # every unset field then hits real arithmetic/formatting against a
    # Mock). _SyncThread runs the CQ worker (and its signal emit) inline
    # on the test thread, so give it a minimal real-shaped result instead.
    fake_iface.contact_quality_workflow.check.return_value = SimpleNamespace(
        per_camera={}, passed=True,
    )

    c = MotionConnector(
        interface=fake_iface,
        app_config={"engineeringMode": False},
        data_dir=".",
        config_dir="config",
    )
    c._consoleConnected = True
    c._leftSensorConnected = True
    c._rightSensorConnected = True

    monkeypatch.setattr("motion_connector.threading.Thread", _SyncThread)
    return c


def test_run_contact_quality_check_uses_configured_mask_not_all_cameras(connector):
    """A camera the user set to "None" (bit cleared in the configured scan
    mask) must be excluded from the SDK's evaluation, not silently probed
    against all 8 cameras regardless of the user's selection."""
    # 0x99 = "Outer" pattern: cameras 1,2,7,8 selected, 3-6 set to "None".
    connector.runContactQualityCheck(0x99, 0x00)

    connector._interface.contact_quality_workflow.check.assert_called_once()
    _, kwargs = connector._interface.contact_quality_workflow.check.call_args
    assert kwargs["left_camera_mask"] == 0x99
    assert kwargs["right_camera_mask"] == 0x00


def test_run_contact_quality_check_still_excludes_disconnected_side(connector):
    """A requested mask on a side whose sensor isn't connected must not
    reach the SDK — connection state still wins over the requested mask."""
    connector._rightSensorConnected = False

    connector.runContactQualityCheck(0xFF, 0xFF)

    _, kwargs = connector._interface.contact_quality_workflow.check.call_args
    assert kwargs["left_camera_mask"] == 0xFF
    assert kwargs["right_camera_mask"] == 0x00
