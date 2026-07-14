"""Issue #352: a disconnected sensor's camera mask must be recorded as
0x00 ("None"), not the UI's pending selection.

The UI deliberately keeps showing the mask a disconnected side *would*
use if the sensor were plugged in before scanning (and zeroing UI state
would regress the #127 preserve-selection-across-power-cycle behavior),
so the gate lives at scan start: ``startCapture`` must zero the mask for
any side whose sensor isn't connected — mirroring the existing pattern
in ``runContactQualityCheck`` and the calibration/test flows. The SDK
records the ScanRequest masks verbatim into ``session_meta.sdk_flags``,
which History and the replay grid trust (per #175), so an ungated mask
shows a phantom config and empty panes for hardware that was never there.

These tests mock the hardware seam (``interface.start_scan``) and assert
on the captured ``ScanRequest`` — no hardware, no app launch.
"""

from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector

pytestmark = pytest.mark.unit


def _make_connector(tmp_path, *, left_connected, right_connected):
    iface = MagicMock()
    iface.console = MagicMock()
    iface.left = MagicMock()
    iface.right = MagicMock()
    iface.is_device_connected.return_value = (
        True, left_connected, right_connected
    )
    iface.scan_workflow = MagicMock()
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.start_scan.return_value = True
    # LiveScanSource treats scan_db_path as an optional path string; a
    # bare MagicMock attribute would leak into os.path calls.
    iface.scan_db_path = None

    c = MotionConnector(
        interface=iface,
        app_config={"engineeringMode": False},
        data_dir=str(tmp_path),
        config_dir="config",
    )
    c._consoleConnected = True
    c._leftSensorConnected = left_connected
    c._rightSensorConnected = right_connected
    return c


def _captured_request(connector, left_mask, right_mask):
    ok = connector.startCapture("subj", 5, left_mask, right_mask, False)
    assert ok is True
    connector._interface.start_scan.assert_called_once()
    return connector._interface.start_scan.call_args.args[0]


def test_disconnected_left_mask_recorded_as_none(tmp_path):
    """Right-only scan (the #352 repro): the Left UI default (0x99
    "Outer") must not reach the ScanRequest — History would show a
    phantom Left config and replay would lay out empty Left panes."""
    connector = _make_connector(
        tmp_path, left_connected=False, right_connected=True
    )
    req = _captured_request(connector, 0x99, 0xFF)
    assert req.left_camera_mask == 0x00
    assert req.right_camera_mask == 0xFF


def test_disconnected_right_mask_recorded_as_none(tmp_path):
    """Mirror of the repro: left-only scan zeroes the right mask."""
    connector = _make_connector(
        tmp_path, left_connected=True, right_connected=False
    )
    req = _captured_request(connector, 0x66, 0xC3)
    assert req.left_camera_mask == 0x66
    assert req.right_camera_mask == 0x00


def test_both_connected_masks_pass_through_unchanged(tmp_path):
    """Guard against over-gating: with both sensors connected the user's
    configured masks reach the SDK untouched."""
    connector = _make_connector(
        tmp_path, left_connected=True, right_connected=True
    )
    req = _captured_request(connector, 0x99, 0xC3)
    assert req.left_camera_mask == 0x99
    assert req.right_camera_mask == 0xC3
