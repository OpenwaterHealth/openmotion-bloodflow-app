# tests/test_firmware_update_detection.py
"""developerMode-gated firmware update detection on the connector."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import motion_connector
from motion_connector import MotionConnector
from omotion.firmware_update import FirmwareKind, LatestInfo

pytestmark = pytest.mark.unit


def _connector(tmp_path, dev_mode):
    iface = MagicMock()
    iface.is_device_connected.return_value = (False, False, False)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = str(tmp_path / "scans.db")
    iface.get_sdk_version.return_value = "9.9.9"
    return MotionConnector(
        interface=iface, app_config={"developerMode": dev_mode},
        data_dir=str(tmp_path), config_dir="config",
    )


def test_no_check_when_developer_mode_off(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(motion_connector, "check_latest", lambda k: called.append(k))
    c = _connector(tmp_path, dev_mode=False)
    c._firmware_versions["left"] = "v1.0.0"
    c._maybe_check_firmware_update("left")
    assert called == [], "must not hit GitHub when developerMode is off"


def test_worker_flags_update_and_emits(tmp_path, monkeypatch):
    monkeypatch.setattr(
        motion_connector, "check_latest",
        lambda k: LatestInfo(FirmwareKind.SENSOR, "1.5.0", "motion-sensor-fw.bin"),
    )
    c = _connector(tmp_path, dev_mode=True)
    c._firmware_versions["left"] = "v1.0.0"
    c._firmware_versions["right"] = "v1.5.0"  # already current
    events = []
    c.firmwareUpdateAvailable.connect(lambda d, cur, lat: events.append((d, cur, lat)))

    c._firmware_check_worker(FirmwareKind.SENSOR)

    assert c._firmware_update_available["left"] is True
    assert c._firmware_update_available["right"] is False
    assert c.leftSensorFirmwareLatest == "1.5.0"
    assert ("left", "v1.0.0", "1.5.0") in events


def test_worker_none_result_is_soft(tmp_path, monkeypatch):
    monkeypatch.setattr(motion_connector, "check_latest", lambda k: None)
    c = _connector(tmp_path, dev_mode=True)
    c._firmware_versions["console"] = "v1.0.0"
    c._firmware_check_worker(FirmwareKind.CONSOLE)
    assert c._firmware_update_available["console"] is False
    assert FirmwareKind.CONSOLE not in c._firmware_checking_kinds  # cleared for retry
