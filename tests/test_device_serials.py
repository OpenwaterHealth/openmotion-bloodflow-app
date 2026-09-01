"""Unit tests for hardware serial-number caching in MotionConnector (#529).

The connector caches each device's programmed serial number (and, for a
sensor, its 8 camera security UIDs) on connect via _log_device_stats, re-reads
them after the sensor-init ID-cache fill, and clears them on disconnect,
surfacing the values to the Settings -> About card. Hardware seam is mocked.
"""
import json
from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector

pytestmark = pytest.mark.unit

UIDS = [f"0x00000000000{i}" for i in range(1, 9)]


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


def _handle(name, serial, uids=None):
    h = MagicMock()
    h.name = name
    h.get_hardware_id.return_value = "DEADBEEF"
    h.get_version.return_value = "v1.0.0"
    h.read_serial_number.return_value = serial
    if uids is not None:
        h.get_cached_camera_security_uid.side_effect = lambda cam: uids[cam]
    return h


def test_identity_empty_before_connect(tmp_path):
    c = _connector(tmp_path)
    assert c.consoleSerialNumber == ""
    assert c.leftSensorSerialNumber == ""
    assert c.rightSensorSerialNumber == ""
    assert c.leftSensorCameraUids == []
    assert c.rightSensorCameraUids == []


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


def test_sensor_serial_and_camera_uids_cached(tmp_path):
    c = _connector(tmp_path)
    c._interface.left = _handle("left", "QWW04Q10003", UIDS)

    c._log_device_stats("left")

    assert c.leftSensorSerialNumber == "QWW04Q10003"
    assert c.leftSensorCameraUids == UIDS
    assert c.rightSensorCameraUids == []


def test_unprogrammed_serial_and_absent_camera_read_as_empty(tmp_path):
    c = _connector(tmp_path)
    uids = list(UIDS)
    uids[2] = "0x000000000000"  # six zero bytes = camera absent
    c._interface.right = _handle("right", None, uids)

    c._log_device_stats("right")

    assert c.rightSensorSerialNumber == ""
    got = c.rightSensorCameraUids
    assert got[2] == ""
    assert got[0] == UIDS[0] and len(got) == 8


def test_all_zero_uids_collapse_to_empty_list(tmp_path):
    # Cameras still powered off at connect: every UID reads back as zeros.
    c = _connector(tmp_path)
    c._interface.left = _handle("left", "QWW04Q10003", ["0x000000000000"] * 8)

    c._log_device_stats("left")

    assert c.leftSensorCameraUids == []


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
    handle = _handle("left", "QWW04Q10003", UIDS)
    c._interface.left = handle
    fired = []
    c.deviceIdentityChanged.connect(lambda: fired.append(True))

    c._on_handle_state_changed_impl(
        handle, ConnectionState.DISCONNECTED, ConnectionState.CONNECTED, "found"
    )
    assert c.leftSensorSerialNumber == "QWW04Q10003"
    assert c.leftSensorCameraUids == UIDS
    n = len(fired)

    c._on_handle_state_changed_impl(
        handle, ConnectionState.CONNECTED, ConnectionState.DISCONNECTED, "lost"
    )
    assert c.leftSensorSerialNumber == ""
    assert c.leftSensorCameraUids == []
    assert len(fired) > n, "disconnect must notify so the row goes blank"


def test_sensor_init_rereads_uids_after_cache_fill(tmp_path):
    # At connect the cameras may be off (zero UIDs); _run_sensor_init powers
    # them, refills the SDK cache, and must re-read so the About card shows
    # real UIDs without a reconnect.
    c = _connector(tmp_path)
    c._interface.is_device_connected.return_value = (True, True, False)
    h = _handle("left", "QWW04Q10003", ["0x000000000000"] * 8)
    h.is_connected.return_value = True
    h.enable_camera_power.return_value = True
    c._interface.left = h
    c._leftSensorConnected = True
    c._log_device_stats("left")
    assert c.leftSensorCameraUids == []

    h.get_cached_camera_security_uid.side_effect = lambda cam: UIDS[cam]
    c._run_sensor_init("left")

    assert c.leftSensorCameraUids == UIDS
    assert c.leftSensorSerialNumber == "QWW04Q10003"
