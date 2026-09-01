"""Unit tests for hardware serial-number caching in MotionConnector (#529).

The connector caches each device's programmed serial number on connect via
_log_device_stats and clears it on disconnect, surfacing the values to the
Settings -> About card. Hardware seam is mocked.
"""
import json
from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector

pytestmark = pytest.mark.unit


def _connector(tmp_path):
    iface = MagicMock()
    iface.is_device_connected.return_value = (False, False, False)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = str(tmp_path / "scans.db")
    iface.get_sdk_version.return_value = "9.9.9"
    c = MotionConnector(
        interface=iface,
        app_config={"engineeringMode": False},
        data_dir=str(tmp_path),
        config_dir="config",
    )
    c._save_app_config = MagicMock()
    return c


def _handle(name, serial):
    h = MagicMock()
    h.name = name
    h.get_hardware_id.return_value = "DEADBEEF"
    h.get_version.return_value = "v1.0.0"
    h.read_serial_number.return_value = serial
    return h


def test_serials_empty_before_connect(tmp_path):
    c = _connector(tmp_path)
    assert c.consoleSerialNumber == ""
    assert c.leftSensorSerialNumber == ""
    assert c.rightSensorSerialNumber == ""


def test_console_serial_cached_notified_and_audited(tmp_path):
    c = _connector(tmp_path)
    c._interface.console = _handle("console", "WWW04Q40005")
    fired = []
    c.deviceIdentityChanged.connect(lambda: fired.append(True))

    c._log_device_stats("console")

    assert c.consoleSerialNumber == "WWW04Q40005"
    assert fired, "deviceIdentityChanged should fire so QML rebinds"
    stats = [e for e in c.auditLogEntries()
             if e["event_type"] == "device_stats"]
    assert json.loads(stats[0]["details"])["serial"] == "WWW04Q40005"


def test_sensor_serials_cached_per_side(tmp_path):
    c = _connector(tmp_path)
    c._interface.left = _handle("left", "QWW04Q10003")
    c._interface.right = _handle("right", "QWW04Q10004")

    c._log_device_stats("left")
    c._log_device_stats("right")

    assert c.leftSensorSerialNumber == "QWW04Q10003"
    assert c.rightSensorSerialNumber == "QWW04Q10004"
    assert c.consoleSerialNumber == ""


def test_unprogrammed_serial_reads_as_empty(tmp_path):
    c = _connector(tmp_path)
    c._interface.right = _handle("right", None)

    c._log_device_stats("right")

    assert c.rightSensorSerialNumber == ""


def test_serial_read_failure_is_soft(tmp_path):
    c = _connector(tmp_path)
    h = _handle("console", "X")
    h.read_serial_number.side_effect = RuntimeError("boom")
    c._interface.console = h

    c._log_device_stats("console")  # must not raise

    assert c.consoleSerialNumber == ""
    assert c.consoleFirmwareVersion == "v1.0.0"


def test_connect_caches_then_disconnect_clears(tmp_path):
    from omotion import ConnectionState
    c = _connector(tmp_path)
    handle = _handle("left", "QWW04Q10003")
    c._interface.left = handle
    fired = []
    c.deviceIdentityChanged.connect(lambda: fired.append(True))

    c._on_handle_state_changed_impl(
        handle, ConnectionState.DISCONNECTED, ConnectionState.CONNECTED, "found"
    )
    assert c.leftSensorSerialNumber == "QWW04Q10003"
    n = len(fired)

    c._on_handle_state_changed_impl(
        handle, ConnectionState.CONNECTED, ConnectionState.DISCONNECTED, "lost"
    )
    assert c.leftSensorSerialNumber == ""
    assert len(fired) > n, "disconnect must notify so the row goes blank"
