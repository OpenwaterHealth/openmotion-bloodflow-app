"""Unit tests for per-device firmware version caching in MotionConnector.

The connector caches each device's firmware version on connect (via
_log_device_stats) and clears it on disconnect, surfacing the values to the
Settings -> System Information card. These tests mock the hardware seam.
"""
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
    return MotionConnector(
        interface=iface,
        app_config={"developerMode": False},
        data_dir=str(tmp_path),
        config_dir="config",
    )


def _handle(name, version):
    h = MagicMock()
    h.name = name
    h.get_hardware_id.return_value = "DEADBEEF"
    h.get_version.return_value = version
    return h


def test_firmware_versions_empty_before_connect(tmp_path):
    c = _connector(tmp_path)
    assert c.consoleFirmwareVersion == ""
    assert c.leftSensorFirmwareVersion == ""
    assert c.rightSensorFirmwareVersion == ""


def test_log_device_stats_caches_version_and_notifies(tmp_path):
    c = _connector(tmp_path)
    c._interface.left = _handle("left", "v2.3.4")
    fired = []
    c.firmwareVersionsChanged.connect(lambda: fired.append(True))

    c._log_device_stats("left")

    assert c.leftSensorFirmwareVersion == "v2.3.4"
    assert fired, "firmwareVersionsChanged should fire so QML rebinds"


def test_connect_caches_then_disconnect_clears(tmp_path):
    from omotion import ConnectionState
    c = _connector(tmp_path)
    handle = _handle("console", "v1.0.0")
    # _log_device_stats resolves the handle via getattr(self._interface, name).
    c._interface.console = handle

    c._on_handle_state_changed_impl(
        handle, ConnectionState.DISCONNECTED, ConnectionState.CONNECTED, "found"
    )
    assert c.consoleFirmwareVersion == "v1.0.0"

    c._on_handle_state_changed_impl(
        handle, ConnectionState.CONNECTED, ConnectionState.DISCONNECTED, "lost"
    )
    assert c.consoleFirmwareVersion == ""
