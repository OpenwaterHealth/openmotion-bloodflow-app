# tests/test_firmware_update_orchestration.py
"""Guard logic for startFirmwareUpdate + disconnect suppression during flash."""
from unittest.mock import MagicMock

import pytest

import motion_connector
from motion_connector import (
    MotionConnector, RUNNING, READY, DISCONNECTED,
)
from omotion import ConnectionState

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_network_check(monkeypatch):
    # Unit tests must not hit GitHub. _maybe_check_firmware_update may spawn a
    # background thread; stub check_latest so it returns immediately with no emit.
    monkeypatch.setattr(motion_connector, "check_latest", lambda kind: None)


def _connector(tmp_path, dev_mode=True):
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = str(tmp_path / "scans.db")
    iface.get_sdk_version.return_value = "9.9.9"
    return MotionConnector(
        interface=iface, app_config={"developerMode": dev_mode},
        data_dir=str(tmp_path), config_dir="config",
    )


def _finished(c):
    out = []
    c.firmwareUpdateFinished.connect(lambda d, ok, m: out.append((d, ok, m)))
    return out


def test_refuses_when_not_developer_mode(tmp_path):
    c = _connector(tmp_path, dev_mode=False)
    c._firmware_update_available["left"] = True
    assert c.startFirmwareUpdate("left") is False


def test_refuses_during_scan(tmp_path):
    c = _connector(tmp_path)
    c._firmware_update_available["left"] = True
    c._state = RUNNING
    out = _finished(c)
    assert c.startFirmwareUpdate("left") is False
    assert out and out[0][1] is False and "scan" in out[0][2].lower()


def test_refuses_when_no_update_available(tmp_path):
    c = _connector(tmp_path)
    c._state = READY
    c._firmware_update_available["left"] = False
    assert c.startFirmwareUpdate("left") is False


def test_refuses_second_concurrent_update(tmp_path):
    c = _connector(tmp_path)
    c._state = READY
    c._firmware_update_available["left"] = True
    c._firmware_update_in_progress = "console"   # already flashing
    out = _finished(c)
    assert c.startFirmwareUpdate("left") is False
    assert out and "in progress" in out[0][2].lower()


def test_disconnect_suppressed_during_flash(tmp_path):
    c = _connector(tmp_path)
    handle = MagicMock(); handle.name = "console"
    handle.get_hardware_id.return_value = "DEAD"
    handle.get_version.return_value = "v1.0.0"
    c._interface.console = handle
    # cache a version, then start "flashing"
    c._log_device_stats("console")
    c._firmware_update_in_progress = "console"

    c._on_handle_state_changed_impl(
        handle, ConnectionState.CONNECTED, ConnectionState.DISCONNECTED, "dfu")

    assert c.consoleFirmwareVersion == "v1.0.0", "expected DFU drop must not clear version"
