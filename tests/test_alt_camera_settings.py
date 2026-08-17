"""Alternative camera settings (#446) — validation, register writes, and the
scan-start apply/restore logic.

The exposure register counts whole 9 µs rows (HTS 432 px / 48 MHz VT_PIXCLK,
per the OX02C10 HTS/VTS/EXP calc sheet), so only multiples of 9 µs in the
99–2196 µs window are valid. Analog gain is real-gain code /16: writing
0x3508 = N, 0x3509 = 0 gives exactly N×. Camera registers persist until the
camera loses power, so disabling the feature must actively restore firmware
defaults on the next scan start (the persisted dirty flag).
"""

import json

from unittest.mock import MagicMock

import pytest

from motion_config import (
    DARK_RATE_LOWER_LIMIT_US,
    DARK_SKIP_CLEAN_EXPOSURE_MAX_US,
    DARK_SKIP_DELAY_US,
    DARK_SKIP_HIGH_EXPOSURE_US,
    DEFAULT_TRIGGER_OVERRIDES,
    FW_DEFAULT_CAMERA_SETTINGS,
    FW_DEFAULT_EXPOSURE_US,
    apply_camera_settings,
    camera_settings_from_config,
    laser_pulse_width_from_config,
    ta_pulse_width_write,
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


# ── Alternative laser pulse width (#449) ─────────────────────────────────────

class TestLaserPulseWidthValidation:
    @pytest.mark.parametrize("value,expected", [
        (500, 500), (100, 100), (2200, 2200),
        (20, 20),          # short-pulse entry / hardware floor
        (750, 750),        # off the UI's 100 µs grid — still valid (hand-edit)
        (500.0, 500),      # JSON round-trip float
    ])
    def test_valid(self, value, expected):
        width, reason = laser_pulse_width_from_config(value)
        assert reason == ""
        assert width == expected

    @pytest.mark.parametrize("bad", [
        19, 2300, 0, -100, None, "abc", True, 750.5, float("nan"),
    ])
    def test_rejected(self, bad):
        width, reason = laser_pulse_width_from_config(bad)
        assert width is None and reason


class TestAltLaserPulseWidth:
    """The connector overrides LaserPulseWidthUsec in the resolved trigger
    config on every setTrigger push while enabled — and only that key."""

    def _trigger_setup(self, tmp_path, app_config):
        from omotion.config import DEFAULT_TRIGGER_CONFIG
        c = _connector(tmp_path, app_config)
        c._interface.resolve_trigger_config.side_effect = (
            lambda d: {**DEFAULT_TRIGGER_CONFIG, **d})
        sent = {}

        def _capture(data=None):
            sent.clear()
            sent.update(data)
            return dict(data)

        c._interface.console.set_trigger_json.side_effect = _capture
        return c, sent

    def test_disabled_passes_sdk_default_through(self, tmp_path):
        c, sent = self._trigger_setup(tmp_path, {})
        assert c.setTrigger(json.dumps({"TriggerStatus": 2})) is True
        assert sent["LaserPulseWidthUsec"] == 500

    def test_enabled_overrides_only_the_laser_width(self, tmp_path):
        c, sent = self._trigger_setup(tmp_path, {
            "altLaserPulseWidthEnabled": True,
            "altLaserPulseWidthUsec": 1500,
        })
        assert c.setTrigger(json.dumps({"TriggerStatus": 2})) is True
        assert sent["LaserPulseWidthUsec"] == 1500
        # Everything else in the resolved config is untouched — notably the
        # camera FSIN pulse width and the pulse delay.
        assert sent["TriggerPulseWidthUsec"] == 500
        assert sent["LaserPulseDelayUsec"] == 100
        assert sent["TriggerStatus"] == 2

    def test_hand_edited_off_grid_value_applies(self, tmp_path):
        c, sent = self._trigger_setup(tmp_path, {
            "altLaserPulseWidthEnabled": True,
            "altLaserPulseWidthUsec": 750,
        })
        c.setTrigger(json.dumps({"TriggerStatus": 2}))
        assert sent["LaserPulseWidthUsec"] == 750

    def test_invalid_width_keeps_resolved_config(self, tmp_path):
        c, sent = self._trigger_setup(tmp_path, {
            "altLaserPulseWidthEnabled": True,
            "altLaserPulseWidthUsec": 2300,
        })
        c.setTrigger(json.dumps({"TriggerStatus": 2}))
        assert sent["LaserPulseWidthUsec"] == 500

    def test_plain_clinical_build_ignores_override(self, tmp_path):
        """#43/#234 fail-closed pattern: a hand-set config key must never
        change laser emission for a clinical user."""
        c, sent = self._trigger_setup(tmp_path, {
            "altLaserPulseWidthEnabled": True,
            "altLaserPulseWidthUsec": 1500,
            "clinicalMode": True,
            "engineeringMode": False,
        })
        c.setTrigger(json.dumps({"TriggerStatus": 2}))
        assert sent["LaserPulseWidthUsec"] == 500

    def test_engineering_unlock_allows_override_on_clinical(self, tmp_path):
        c, sent = self._trigger_setup(tmp_path, {
            "altLaserPulseWidthEnabled": True,
            "altLaserPulseWidthUsec": 300,
            "clinicalMode": True,
            "engineeringMode": True,
        })
        c.setTrigger(json.dumps({"TriggerStatus": 2}))
        assert sent["LaserPulseWidthUsec"] == 300


# ── TA_PULSE_WIDTH register write (the ACTUAL optical pulse, #449) ───────────

class TestTaPulseWidthWrite:
    def test_alternative_width_encodes_ticks_little_endian(self):
        kwargs, ticks = ta_pulse_width_write(800)
        assert ticks == 2500  # 800 µs / 0.32 µs per tick
        assert kwargs["data"] == bytearray((2500).to_bytes(3, "little"))
        # TA driver location per fpga_model.json.
        assert kwargs["mux_index"] == 1
        assert kwargs["channel"] == 4
        assert kwargs["device_addr"] == 0x41
        assert kwargs["reg_addr"] == 0

    def test_ticks_track_the_032us_scale(self):
        for us in (100, 500, 1000, 2200):
            _, ticks = ta_pulse_width_write(us)
            assert abs(ticks - us / 0.32) <= 1

    def test_short_pulse_stays_above_the_driver_underflow(self):
        """20 µs = 62 ticks, safely above the 55-tick compare underflow in
        driver_control.v (`pulse_count > pulse_width - 55`), below which the
        TA drive would never be cleared. The clamp is belt-and-braces: the
        validator already floors the config at 20 µs."""
        from motion_config import TA_PULSE_MIN_TICKS

        _, ticks = ta_pulse_width_write(20)
        assert ticks == 62
        assert ticks > TA_PULSE_MIN_TICKS

        _, clamped = ta_pulse_width_write(1)
        assert clamped == TA_PULSE_MIN_TICKS

    def test_baseline_matches_bundled_laser_params(self):
        """None → the laser_params.json TA_PULSE_WIDTH bytes (the restore
        value): [27, 6, 0] LE = 1563 ticks ≈ 500.2 µs — the deployed value
        that measures as the ~494 µs optical pulse."""
        kwargs, ticks = ta_pulse_width_write(None)
        assert kwargs["data"] == bytearray([27, 6, 0])
        assert ticks == 1563


class TestConnectorTaPulseWidth:
    def _ta_setup(self, tmp_path, app_config, write_ok=True):
        c = _connector(tmp_path, app_config)
        c._interface.console.write_i2c_packet = MagicMock(return_value=write_ok)
        return c, c._interface.console.write_i2c_packet

    def test_disabled_and_clean_writes_nothing(self, tmp_path):
        c, write = self._ta_setup(tmp_path, {})
        c._apply_alt_ta_pulse_width()
        write.assert_not_called()

    def test_enabled_writes_ticks_and_sets_dirty(self, tmp_path):
        c, write = self._ta_setup(tmp_path, {
            "altLaserPulseWidthEnabled": True,
            "altLaserPulseWidthUsec": 800,
        })
        c._apply_alt_ta_pulse_width()
        write.assert_called_once()
        kwargs = write.call_args.kwargs
        assert kwargs["channel"] == 4
        assert kwargs["data"] == bytearray((2500).to_bytes(3, "little"))
        assert c._app_config["altLaserPulseWidthDirty"] is True

    def test_disable_after_enable_restores_baseline_once(self, tmp_path):
        c, write = self._ta_setup(tmp_path, {
            "altLaserPulseWidthEnabled": False,
            "altLaserPulseWidthDirty": True,
        })
        c._apply_alt_ta_pulse_width()
        write.assert_called_once()
        assert write.call_args.kwargs["data"] == bytearray([27, 6, 0])
        assert c._app_config["altLaserPulseWidthDirty"] is False
        write.reset_mock()
        c._apply_alt_ta_pulse_width()  # second scan: nothing left to restore
        write.assert_not_called()

    def test_failed_restore_keeps_dirty_for_retry(self, tmp_path):
        c, write = self._ta_setup(tmp_path, {
            "altLaserPulseWidthEnabled": False,
            "altLaserPulseWidthDirty": True,
        }, write_ok=False)
        c._apply_alt_ta_pulse_width()
        assert c._app_config["altLaserPulseWidthDirty"] is True

    def test_invalid_width_writes_nothing(self, tmp_path):
        c, write = self._ta_setup(tmp_path, {
            "altLaserPulseWidthEnabled": True,
            "altLaserPulseWidthUsec": 2300,
        })
        c._apply_alt_ta_pulse_width()
        write.assert_not_called()

    def test_plain_clinical_build_writes_nothing(self, tmp_path):
        c, write = self._ta_setup(tmp_path, {
            "altLaserPulseWidthEnabled": True,
            "altLaserPulseWidthUsec": 800,
            "clinicalMode": True,
            "engineeringMode": False,
        })
        c._apply_alt_ta_pulse_width()
        write.assert_not_called()


# ── Dark-frame skip delay (pinned 1800 µs, every build — #449) ───────────────

class TestDarkSkipConstant:
    """Reverted from 2400 to 1800 on 2026-08-17: 2400 bought clean darks at
    engineering-only high exposures by requiring a fleet-wide safety-config
    change, and a console that didn't get it latches its interlock on the
    first dark frame — no laser at all. Clean darks up to 1700 us beat that."""

    def test_pinned_skip_clears_stock_console_rate_ll(self):
        # THE constraint that drove the revert: the post-dark inter-pulse gap
        # (period - skip) must clear the console's RATE_LL floor or the safety
        # FPGAs latch. Stock units ship 22500 us and real hardware has been
        # observed at 23125 us — both must pass on an unprovisioned console.
        period_us = 1_000_000.0 / 40
        gap_us = period_us - DARK_SKIP_DELAY_US
        assert gap_us >= DARK_RATE_LOWER_LIMIT_US
        assert gap_us >= 23125    # observed stock console (raw 72266)
        assert gap_us >= 22500    # laser_params.json shipped default

    def test_pinned_skip_keeps_the_default_exposure_dark_clean(self):
        # The displaced pulse starts at delay(100) + skip; every exposure at
        # or below the clean ceiling must end before it. 648 us (firmware
        # default, and all a clinical build ever runs) has ~1250 us of room.
        assert 100 + DARK_SKIP_DELAY_US > DARK_SKIP_CLEAN_EXPOSURE_MAX_US
        assert 100 + DARK_SKIP_DELAY_US > FW_DEFAULT_EXPOSURE_US

    def test_high_exposure_alternative_is_documented_and_needs_provisioning(self):
        # The 2400 us escape hatch stays in the module as a named constant so
        # the UI warning and the docs can point at it — but taking it REQUIRES
        # dropping the console's RATE_LL below the gap it produces, which is
        # under every stock floor. That precondition is the whole reason it
        # isn't the default.
        period_us = 1_000_000.0 / 40
        assert DARK_SKIP_HIGH_EXPOSURE_US > DARK_SKIP_DELAY_US
        assert 100 + DARK_SKIP_HIGH_EXPOSURE_US >= 2196   # clears the dropdown
        assert period_us - DARK_SKIP_HIGH_EXPOSURE_US == 22600
        assert period_us - DARK_SKIP_HIGH_EXPOSURE_US < 23125

    def test_override_dict_pins_exactly_the_skip_key(self):
        assert DEFAULT_TRIGGER_OVERRIDES == {
            "LaserPulseSkipDelayUsec": DARK_SKIP_DELAY_US}


class TestDarkSkipWiring:
    """The pin rides MotionInterface's default_trigger_config (main.py) so
    the SDK's own trigger re-send at scan start carries it too — a
    connector-side patch after resolve is exactly what ScanWorkflow
    silently reverted (how the 2026-08-10 dark contamination shipped)."""

    def test_sdk_merge_resolves_to_the_pinned_skip(self):
        # The real merge the MotionInterface constructor performs.
        from omotion.config import merge_trigger_config
        resolved = merge_trigger_config(DEFAULT_TRIGGER_OVERRIDES)
        assert resolved["LaserPulseSkipDelayUsec"] == DARK_SKIP_DELAY_US

    def _trigger_setup(self, tmp_path, app_config):
        # Interface stub mirrors main.py's wiring: the override is part of
        # the resolved default, not patched in by the connector.
        from omotion.config import DEFAULT_TRIGGER_CONFIG
        c = _connector(tmp_path, app_config)
        c._interface.resolve_trigger_config.side_effect = (
            lambda d: {**DEFAULT_TRIGGER_CONFIG,
                       **DEFAULT_TRIGGER_OVERRIDES, **d})
        sent = {}
        c._interface.console.set_trigger_json.side_effect = (
            lambda data=None: (sent.clear(), sent.update(data), dict(data))[-1])
        return c, sent

    def test_every_trigger_push_carries_the_pinned_skip(self, tmp_path):
        c, sent = self._trigger_setup(tmp_path, {})
        c.setTrigger(json.dumps({"TriggerStatus": 2}))
        assert sent["LaserPulseSkipDelayUsec"] == DARK_SKIP_DELAY_US

    def test_clinical_build_carries_it_too(self, tmp_path):
        # Deliberately unconditional: the SDK's 1800 default was an
        # uncalibrated guess, not a clinical value anyone tuned — nothing
        # anywhere preserves it.
        c, sent = self._trigger_setup(tmp_path, {
            "clinicalMode": True,
            "engineeringMode": False,
        })
        c.setTrigger(json.dumps({"TriggerStatus": 2}))
        assert sent["LaserPulseSkipDelayUsec"] == DARK_SKIP_DELAY_US


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
