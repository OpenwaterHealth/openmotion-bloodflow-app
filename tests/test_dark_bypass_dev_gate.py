"""Issue #345: the dark-correction bypass must be engineering-gated.

The persisted ``darkCorrectionBypass`` toggle only takes effect while
``engineeringMode`` is also true at scan start (fail closed for clinical
use, same pattern as writeRawCsv / telemetry in issue #43). These tests
mock the hardware seam (``interface.start_scan``) and assert on the
captured ``ScanRequest`` — no hardware, no app launch.
"""

from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector

pytestmark = pytest.mark.unit


def _make_connector(tmp_path, app_config):
    iface = MagicMock()
    iface.console = MagicMock()
    iface.left = MagicMock()
    iface.right = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow = MagicMock()
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.start_scan.return_value = True
    # LiveScanSource treats scan_db_path as an optional path string; a
    # bare MagicMock attribute would leak into os.path calls.
    iface.scan_db_path = None

    c = MotionConnector(
        interface=iface,
        app_config=app_config,
        data_dir=str(tmp_path),
        config_dir="config",
    )
    c._consoleConnected = True
    c._leftSensorConnected = True
    c._rightSensorConnected = True
    return c


def _captured_request(connector):
    ok = connector.startCapture("subj", 5, 0x66, 0x66, False)
    assert ok is True
    connector._interface.start_scan.assert_called_once()
    return connector._interface.start_scan.call_args.args[0]


@pytest.mark.parametrize(
    "config, expected",
    [
        ({"engineeringMode": True, "darkCorrectionBypass": True}, True),
        ({"engineeringMode": True, "darkCorrectionBypass": False}, False),
        ({"engineeringMode": True}, False),                   # key missing
        ({"engineeringMode": False, "darkCorrectionBypass": True}, False),
        ({"darkCorrectionBypass": True}, False),              # eng key missing
        ({}, False),
    ],
)
def test_dark_bypass_requires_engineering_mode(tmp_path, config, expected):
    connector = _make_connector(tmp_path, dict(config))
    req = _captured_request(connector)
    assert req.dark_correction_bypass is expected
