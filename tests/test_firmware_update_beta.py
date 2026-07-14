"""downloadBetaFirmware flows into the SDK check + a toggle re-checks."""
from unittest.mock import MagicMock

import pytest

import motion_connector
from motion_connector import MotionConnector
from omotion.firmware_update import FirmwareKind, LatestInfo

pytestmark = pytest.mark.unit


def _connector(tmp_path, dev_mode=True, beta=False):
    iface = MagicMock()
    iface.is_device_connected.return_value = (False, False, False)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = str(tmp_path / "scans.db")
    iface.get_sdk_version.return_value = "9.9.9"
    return MotionConnector(
        interface=iface,
        app_config={"engineeringMode": dev_mode, "downloadBetaFirmware": beta},
        data_dir=str(tmp_path), config_dir="config",
    )


def test_beta_flag_passed_to_check_latest(tmp_path, monkeypatch):
    seen = {}

    def fake_check(kind, *, include_prerelease=False):
        seen["beta"] = include_prerelease
        return None

    monkeypatch.setattr(motion_connector, "check_latest", fake_check)
    c = _connector(tmp_path, beta=True)
    c._firmware_versions["left"] = "1.8.0"
    c._firmware_check_worker(FirmwareKind.SENSOR)
    assert seen["beta"] is True


def test_beta_uses_release_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(
        motion_connector, "check_latest",
        lambda kind, *, include_prerelease=False:
            LatestInfo(FirmwareKind.SENSOR, "1.8.1-dev.5", "motion-sensor-fw.bin"),
    )
    c = _connector(tmp_path, beta=True)
    # base tag 1.8.1-rc.0 differs from latest 1.8.1-dev.5 -> available
    c._firmware_versions["left"] = "1.8.1-rc.0-2-gf09e8dc-dirty"
    c._firmware_check_worker(FirmwareKind.SENSOR)
    assert c._firmware_update_available["left"] is True


def test_toggle_invalidates_and_rechecks(tmp_path):
    c = _connector(tmp_path, beta=False)
    c._firmware_versions["left"] = "1.8.0"
    c._firmware_latest_by_kind["sensor"] = "1.8.0"
    c._firmware_update_available["left"] = True
    called = []
    c._maybe_check_firmware_update = lambda name: called.append(name)

    c.setConfig("downloadBetaFirmware", True)

    assert c._firmware_latest_by_kind == {}      # cache invalidated
    assert "left" in called                      # re-check for the connected device
    assert c._firmware_update_available["left"] is False


def test_check_worker_discards_superseded_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        motion_connector, "check_latest",
        lambda kind, **_: LatestInfo(FirmwareKind.SENSOR, "1.8.1-dev.5", "motion-sensor-fw.bin"),
    )
    c = _connector(tmp_path, beta=True)
    c._firmware_versions["left"] = "1.8.0"
    c._firmware_check_generation = 5
    c._firmware_check_worker(FirmwareKind.SENSOR, generation=4)   # stale generation
    assert c._firmware_latest_by_kind == {}                       # result discarded
