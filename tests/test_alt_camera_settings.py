"""Alternative camera settings (#446) — validation, register writes, and the
scan-start apply/restore logic.

The exposure register counts whole 9 µs rows (HTS 432 px / 48 MHz VT_PIXCLK,
per the OX02C10 HTS/VTS/EXP calc sheet), so only multiples of 9 µs in the
99–2196 µs window are valid. Analog gain is real-gain code /16: writing
0x3508 = N, 0x3509 = 0 gives exactly N×. Camera registers persist until the
camera loses power, so disabling the feature must actively restore firmware
defaults on the next scan start (the persisted dirty flag).
"""

from unittest.mock import MagicMock

import pytest

from motion_config import (
    FW_DEFAULT_CAMERA_SETTINGS,
    apply_camera_settings,
    camera_settings_from_config,
)
from motion_connector import MotionConnector

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_config_writes(monkeypatch):
    """_save_app_config must not clobber the repo's real app_config.local.json
    (save_overrides writes to app_paths.local_config_path, not tmp_path)."""
    from utils import config_store
    monkeypatch.setattr(config_store, "save_overrides", lambda cur, base: None)


FW_GAINS = [16, 4, 2, 1, 1, 2, 4, 16]


# ── camera_settings_from_config ──────────────────────────────────────────────

class TestValidation:
    def test_fw_defaults_validate(self):
        s, reason = camera_settings_from_config(648, FW_GAINS)
        assert reason == ""
        assert s.exposure_rows == 72
        assert s.exposure_us == 648
        assert s.gains == tuple(FW_GAINS)

    def test_range_endpoints(self):
        s, _ = camera_settings_from_config(99, FW_GAINS)
        assert s.exposure_rows == 11
        s, _ = camera_settings_from_config(2196, FW_GAINS)
        assert s.exposure_rows == 244

    def test_hand_edited_exposure_snaps_to_nearest_row(self):
        s, reason = camera_settings_from_config(650, FW_GAINS)
        assert reason == ""
        assert s.exposure_rows == 72  # 650/9 = 72.2 → 72 rows = 648 µs

    @pytest.mark.parametrize("bad", [90, 2300, 0, -9, None, "abc", float("nan")])
    def test_exposure_out_of_window_or_garbage_rejected(self, bad):
        s, reason = camera_settings_from_config(bad, FW_GAINS)
        assert s is None and reason

    @pytest.mark.parametrize("bad_gains", [
        None,
        [],
        [1] * 7,
        [1] * 9,
        [16, 4, 2, 1, 1, 2, 4, 3],       # 3 is not a valid analog gain
        [16, 4, 2, 1, 1, 2, 4, 1.5],     # fractional
        [16, 4, 2, 1, 1, 2, 4, True],    # bool must not pass as 1
        [16, 4, 2, 1, 1, 2, 4, "8"],     # string
    ])
    def test_bad_gains_rejected(self, bad_gains):
        s, reason = camera_settings_from_config(648, bad_gains)
        assert s is None and reason

    def test_integral_floats_accepted(self):
        # A JSON round-trip through a hand editor can float-ify the ints.
        s, reason = camera_settings_from_config(648.0, [16.0, 4, 2, 1, 1, 2, 4, 16])
        assert reason == ""
        assert s.gains == tuple(FW_GAINS)


# ── apply_camera_settings ────────────────────────────────────────────────────

class FakeSensor:
    """Records switch_camera / camera_i2c_write calls for assertions."""

    def __init__(self, fail_regs=(), raise_on_switch=()):
        self.calls = []                       # ("switch", cam) / (cam, reg, val)
        self._cur = None
        self._fail_regs = set(fail_regs)      # regs whose write returns False
        self._raise_on_switch = set(raise_on_switch)

    def switch_camera(self, cam_id):
        if cam_id in self._raise_on_switch:
            raise RuntimeError(f"switch failed for cam {cam_id}")
        self._cur = cam_id
        self.calls.append(("switch", cam_id))

    def camera_i2c_write(self, packet):
        self.calls.append((self._cur, packet.register_address, packet.data))
        return packet.register_address not in self._fail_regs

    def writes_for(self, cam_id):
        return [(reg, val) for cam, reg, val in
                (c for c in self.calls if c[0] != "switch") if cam == cam_id]


class TestApplyCameraSettings:
    def test_writes_masked_cameras_only(self):
        s, _ = camera_settings_from_config(1098, [1, 2, 4, 8, 16, 8, 4, 2])
        sensor = FakeSensor()
        failures = apply_camera_settings(sensor, 0x05, s)  # cams 0 and 2
        assert failures == []
        # 122 rows = 0x007A; camera 3 (id 2) gain 4
        assert sensor.writes_for(0) == [
            (0x3501, 0x00), (0x3502, 0x7A), (0x3508, 1), (0x3509, 0x00)]
        assert sensor.writes_for(2) == [
            (0x3501, 0x00), (0x3502, 0x7A), (0x3508, 4), (0x3509, 0x00)]
        assert sensor.writes_for(1) == []
        # Mux is re-selected before each camera's writes.
        assert sensor.calls[0] == ("switch", 0)
        assert ("switch", 2) in sensor.calls

    def test_failed_write_recorded_and_rest_continue(self):
        sensor = FakeSensor(fail_regs={0x3508})
        failures = apply_camera_settings(sensor, 0x03, FW_DEFAULT_CAMERA_SETTINGS)
        assert len(failures) == 2
        assert all("0x3508" in f for f in failures)
        # Exposure still written on both despite the gain-write failures.
        assert (0x3502, 72) in sensor.writes_for(0)
        assert (0x3502, 72) in sensor.writes_for(1)

    def test_switch_exception_is_contained(self):
        sensor = FakeSensor(raise_on_switch={0})
        failures = apply_camera_settings(sensor, 0x03, FW_DEFAULT_CAMERA_SETTINGS)
        assert len(failures) == 1 and "camera 1" in failures[0]
        assert sensor.writes_for(1) != []  # cam 1 still fully written


# ── connector scan-start apply/restore ───────────────────────────────────────

def _connector(tmp_path, app_config):
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = None  # MagicMock path would break the audit log
    c = MotionConnector(
        interface=iface, app_config=app_config,
        data_dir=str(tmp_path), config_dir="config",
    )
    # Swap the fakes in AFTER construction — connect_signals() wires Qt
    # signals on the handles, which the plain FakeSensor doesn't have.
    iface.left = FakeSensor()
    iface.right = FakeSensor()
    c._leftSensorConnected = True
    c._rightSensorConnected = True
    return c


class TestConnectorApply:
    def test_disabled_and_clean_is_a_no_op(self, tmp_path):
        c = _connector(tmp_path, {"altCameraSettingsEnabled": False})
        c._apply_alt_camera_settings(0xFF, 0xFF)
        assert c._interface.left.calls == []
        assert c._interface.right.calls == []
        assert c._app_config.get("altCameraSettingsDirty") is not True

    def test_enabled_writes_both_sides_and_sets_dirty(self, tmp_path):
        c = _connector(tmp_path, {
            "altCameraSettingsEnabled": True,
            "altCameraExposureUs": 999,
            "altCameraGains": [8] * 8,
        })
        c._apply_alt_camera_settings(0x01, 0x80)
        assert c._interface.left.writes_for(0) == [
            (0x3501, 0x00), (0x3502, 111), (0x3508, 8), (0x3509, 0x00)]
        assert c._interface.right.writes_for(7) == [
            (0x3501, 0x00), (0x3502, 111), (0x3508, 8), (0x3509, 0x00)]
        assert c._app_config["altCameraSettingsDirty"] is True

    def test_disconnected_side_is_skipped(self, tmp_path):
        c = _connector(tmp_path, {
            "altCameraSettingsEnabled": True,
            "altCameraExposureUs": 648,
            "altCameraGains": FW_GAINS,
        })
        c._rightSensorConnected = False
        c._apply_alt_camera_settings(0x01, 0x01)
        assert c._interface.left.calls != []
        assert c._interface.right.calls == []

    def test_invalid_config_writes_nothing_and_stays_clean(self, tmp_path):
        c = _connector(tmp_path, {
            "altCameraSettingsEnabled": True,
            "altCameraExposureUs": 648,
            "altCameraGains": [16, 4, 2, 1, 1, 2, 4, 3],
        })
        c._apply_alt_camera_settings(0xFF, 0xFF)
        assert c._interface.left.calls == []
        assert c._app_config.get("altCameraSettingsDirty") is not True

    def test_disable_after_enable_restores_fw_defaults_once(self, tmp_path):
        c = _connector(tmp_path, {
            "altCameraSettingsEnabled": False,
            "altCameraSettingsDirty": True,
        })
        c._apply_alt_camera_settings(0x01, 0x00)
        # FW defaults: 72 rows / per-position ladder (camera 1 → 16×).
        assert c._interface.left.writes_for(0) == [
            (0x3501, 0x00), (0x3502, 72), (0x3508, 16), (0x3509, 0x00)]
        assert c._app_config["altCameraSettingsDirty"] is False
        # Second scan with the toggle still off: nothing left to restore.
        c._interface.left.calls.clear()
        c._apply_alt_camera_settings(0x01, 0x00)
        assert c._interface.left.calls == []

    def test_failed_restore_keeps_dirty_for_retry(self, tmp_path):
        c = _connector(tmp_path, {
            "altCameraSettingsEnabled": False,
            "altCameraSettingsDirty": True,
        })
        c._interface.left = FakeSensor(fail_regs={0x3502})
        c._apply_alt_camera_settings(0x01, 0x00)
        assert c._app_config["altCameraSettingsDirty"] is True


# ── QML bridge (2026-08-10 crash regression) ─────────────────────────────────

class TestQmlBridge:
    def test_qjsvalue_gains_array_unwrapped_and_persistable(self, tmp_path):
        """A QML JS array reaches setConfig as a QJSValue (not a list);
        unwrapped it must store as a plain list that json can dump (the
        crash path was save_overrides -> json.dump) and that the
        scan-start validation accepts."""
        import json as _json

        from PyQt6.QtCore import QCoreApplication
        from PyQt6.QtQml import QJSEngine

        if QCoreApplication.instance() is None:
            # Keep a module-level ref so the app object outlives the test.
            global _qt_app
            _qt_app = QCoreApplication([])

        c = _connector(tmp_path, {
            "altCameraSettingsEnabled": True,
            "altCameraExposureUs": 747,
        })
        engine = QJSEngine()
        arr = engine.newArray(8)
        for i, g in enumerate([16, 4, 2, 1, 1, 2, 4, 8]):
            arr.setProperty(i, g)

        c.setConfig("altCameraGains", arr)

        stored = c._app_config["altCameraGains"]
        assert isinstance(stored, list)
        _json.dumps(stored)  # must not raise
        settings, reason = camera_settings_from_config(747, stored)
        assert reason == ""
        assert settings.gains == (16, 4, 2, 1, 1, 2, 4, 8)
