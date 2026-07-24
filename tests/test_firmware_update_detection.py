# tests/test_firmware_update_detection.py
"""engineeringMode-gated firmware update detection on the connector."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import motion_connector
from motion_connector import MotionConnector
from omotion.firmware_update import FirmwareKind, LatestInfo

pytestmark = pytest.mark.unit


def _connector(tmp_path, dev_mode=False, clinical=False):
    iface = MagicMock()
    iface.is_device_connected.return_value = (False, False, False)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = str(tmp_path / "scans.db")
    iface.get_sdk_version.return_value = "9.9.9"
    return MotionConnector(
        interface=iface,
        app_config={"engineeringMode": dev_mode, "clinicalMode": clinical},
        data_dir=str(tmp_path), config_dir="config",
    )


def test_no_check_in_clinical_mode(tmp_path, monkeypatch):
    """Clinical build must never spawn a firmware check — even with
    engineering mode unlocked (#386)."""
    monkeypatch.setattr(motion_connector, "check_latest", lambda k, **_: None)
    c = _connector(tmp_path, dev_mode=True, clinical=True)   # eng ON, clinical
    c._firmware_versions["left"] = "v1.0.0"
    spawned = []
    monkeypatch.setattr(
        motion_connector.threading, "Thread",
        lambda **k: MagicMock(start=lambda: spawned.append(1)))
    c._maybe_check_firmware_update("left")
    assert spawned == [], "clinical build must not check firmware, even with eng mode on"


def test_check_runs_in_research_without_eng(tmp_path, monkeypatch):
    """Research build checks firmware on the stable channel with no
    engineering mode required (#386)."""
    monkeypatch.setattr(motion_connector, "check_latest", lambda k, **_: None)
    c = _connector(tmp_path, dev_mode=False, clinical=False)  # research, eng OFF
    c._firmware_versions["left"] = "v1.0.0"
    spawned = []
    monkeypatch.setattr(
        motion_connector.threading, "Thread",
        lambda **k: MagicMock(start=lambda: spawned.append(k.get("args"))))
    c._maybe_check_firmware_update("left")
    assert spawned, "research build must check firmware even with eng mode off"


def test_worker_flags_update_and_emits(tmp_path, monkeypatch):
    monkeypatch.setattr(
        motion_connector, "check_latest",
        lambda k, **_: LatestInfo(FirmwareKind.SENSOR, "1.5.0", "motion-sensor-fw.bin"),
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
    monkeypatch.setattr(motion_connector, "check_latest", lambda k, **_: None)
    c = _connector(tmp_path, dev_mode=True)
    c._firmware_versions["console"] = "v1.0.0"
    c._firmware_check_worker(FirmwareKind.CONSOLE)
    assert c._firmware_update_available["console"] is False
    assert FirmwareKind.CONSOLE not in c._firmware_checking_kinds  # cleared for retry
