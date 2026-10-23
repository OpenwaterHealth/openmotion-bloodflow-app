"""Long-exposure dark-frame skip (#747).

At the default 1800 µs skip a scheduled dark's displaced laser pulse starts
at 100 + 1800 = 1900 µs, so an alternative exposure past ~1700 µs re-catches
it (bench 2026-10-21, 2196 µs exposure: darks 207-1017 DN against a
127-130 DN laser-off frame). Those runs, and their preflight signal-quality
check, move to a 2400 µs skip through ScanRequest.trigger_config. That
shortens the post-dark gap to 22600 µs, under the 23125 µs RATE_LL floor
real consoles hold (and only 100 µs over the laser_params stock 22500), so
both safety FPGAs' RATE_LL is widened to 22000 µs first and restored once
no run needs it, with the same persisted dirty flag
pattern as the PULSE_WIDTH_UL ceiling (#483). Everything at or under 1700 µs
stays at 1800 with no safety writes.
"""

import threading

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from motion_config import (
    DARK_SKIP_CLEAN_EXPOSURE_MAX_US,
    DARK_SKIP_DELAY_US,
    DARK_SKIP_HIGH_EXPOSURE_US,
    DARK_SKIP_SAFETY_RATE_LL_US,
    DEFAULT_TRIGGER_OVERRIDES,
    LASER_PULSE_DELAY_US,
    dark_skip_delay_us,
    safety_rate_ll_writes,
)
from motion_connector import MotionConnector, _TriggerOverrideScanWorkflow

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_config_writes(monkeypatch):
    """_save_app_config persists nothing here (no settings store, #546)."""
    from utils import config_store
    monkeypatch.setattr(config_store, "persistable_diff", lambda cur, base: ({}, []))


LONG_SKIP = {"LaserPulseSkipDelayUsec": DARK_SKIP_HIGH_EXPOSURE_US}
FLOOR_BYTES = (68750).to_bytes(4, "little")     # 22000 µs / 0.32 µs per tick
STOCK_BYTES = bytes([169, 18, 1, 0])            # laser_params.json ≈ 22500 µs
RATE_LL_OFFSET = 0x08

LONG_EXPOSURE = {
    "altCameraSettingsEnabled": True,
    "altCameraExposureUs": 2196,
    "altCameraGains": [16] * 8,
}
SHORT_EXPOSURE = {
    "altCameraSettingsEnabled": True,
    "altCameraExposureUs": 1692,               # 188 rows, the last clean one
    "altCameraGains": [16] * 8,
}


# ── dark_skip_delay_us ───────────────────────────────────────────────────────

class TestDarkSkipDecision:
    def test_nothing_active_keeps_the_default(self):
        assert dark_skip_delay_us() == DARK_SKIP_DELAY_US

    @pytest.mark.parametrize("exposure_us, expected", [
        (648, DARK_SKIP_DELAY_US),             # firmware default
        (1692, DARK_SKIP_DELAY_US),            # last 9 µs row under 1700
        (1700, DARK_SKIP_DELAY_US),            # the clean ceiling itself
        (1701, DARK_SKIP_HIGH_EXPOSURE_US),
        (2196, DARK_SKIP_HIGH_EXPOSURE_US),    # top of the dropdown
    ])
    def test_exposure(self, exposure_us, expected):
        assert dark_skip_delay_us(exposure_us=exposure_us) == expected

    @pytest.mark.parametrize("width_us, expected", [
        (500, DARK_SKIP_DELAY_US),
        (1600, DARK_SKIP_DELAY_US),            # 100 + 1600 = 1700
        (1601, DARK_SKIP_HIGH_EXPOSURE_US),
        (2200, DARK_SKIP_HIGH_EXPOSURE_US),
    ])
    def test_delay_plus_pulse_width(self, width_us, expected):
        assert dark_skip_delay_us(pulse_width_us=width_us) == expected

    def test_either_setting_is_enough(self):
        assert dark_skip_delay_us(648, 2000) == DARK_SKIP_HIGH_EXPOSURE_US
        assert dark_skip_delay_us(2196, 500) == DARK_SKIP_HIGH_EXPOSURE_US

    def test_pulse_delay_matches_the_sdk_default(self):
        # The app overrides only the skip, so the SDK default delay is the
        # one every scan resolves to.
        from omotion.config import merge_trigger_config
        resolved = merge_trigger_config(DEFAULT_TRIGGER_OVERRIDES)
        assert resolved["LaserPulseDelayUsec"] == LASER_PULSE_DELAY_US

    def test_long_skip_clears_the_dropdown_and_the_widened_floor(self):
        period_us = 1_000_000.0 / 40
        # The displaced pulse starts after the longest exposure ends...
        assert LASER_PULSE_DELAY_US + DARK_SKIP_HIGH_EXPOSURE_US > 2196
        # ...and the post-dark gap it leaves (22600 µs) clears the floor the
        # run writes, but not the 23125 µs real consoles hold in flash (and
        # the laser_params stock 22500 by only 100 µs).
        gap_us = period_us - DARK_SKIP_HIGH_EXPOSURE_US
        assert gap_us > DARK_SKIP_SAFETY_RATE_LL_US
        assert gap_us < 23125


# ── safety_rate_ll_writes ────────────────────────────────────────────────────

class TestSafetyRateLlWrites:
    def test_floor_targets_both_safety_fpgas(self):
        writes = safety_rate_ll_writes(DARK_SKIP_SAFETY_RATE_LL_US)
        assert [w[0] for w in writes] == ["EE_RATE_LL", "OPT_RATE_LL"]
        for (name, kwargs, ticks, source), channel in zip(writes, (6, 7)):
            assert ticks == 68750
            assert kwargs["data"] == bytearray(FLOOR_BYTES)
            # Safety EE / OPT per fpga_model.json.
            assert kwargs["mux_index"] == 1
            assert kwargs["channel"] == channel
            assert kwargs["device_addr"] == 0x41
            assert kwargs["reg_addr"] == RATE_LL_OFFSET
            assert "floor" in source

    def test_restore_without_user_config_is_the_stock_22500us(self):
        for name, kwargs, ticks, source in safety_rate_ll_writes(None):
            assert kwargs["data"] == bytearray(STOCK_BYTES)
            assert ticks == 70313
            assert "laser_params" in source

    def test_restore_prefers_the_console_user_config(self):
        # The bench console holds 23125 µs in flash (raw 72266).
        ee, opt = safety_rate_ll_writes(
            None, user_config={"EE_RATE_LL": 23125, "OPT_RATE_LL": 23125.0})
        assert ee[2] == 72266
        assert ee[1]["data"] == bytearray((72266).to_bytes(4, "little"))
        assert opt[2] == 72266
        assert "user-config" in ee[3] and "user-config" in opt[3]

    def test_restore_ignores_garbage_user_config_values(self):
        writes = safety_rate_ll_writes(
            None, user_config={"EE_RATE_LL": "abc", "OPT_RATE_LL": False})
        for name, kwargs, ticks, source in writes:
            assert ticks == 70313
            assert "laser_params" in source


# ── connector: widen / restore + the per-run override ────────────────────────

class FakeSensor:
    """Accepts the alternative camera register writes."""

    def __init__(self):
        self.writes = 0

    def switch_camera(self, cam_id):
        pass

    def camera_i2c_write(self, packet):
        self.writes += 1
        return True


def _connector(tmp_path, app_config, write_ok=True):
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = None  # MagicMock path would break the audit log
    iface.start_scan.return_value = True
    c = MotionConnector(
        interface=iface, app_config=app_config,
        data_dir=str(tmp_path), config_dir="config",
    )
    # Swapped in after construction: connect_signals() wires Qt signals on
    # the handles, which the plain FakeSensor doesn't have.
    iface.left = FakeSensor()
    iface.right = FakeSensor()
    iface.console.write_i2c_packet = MagicMock(return_value=write_ok)
    iface.console.read_config = MagicMock(return_value=None)
    c._consoleConnected = True
    c._leftSensorConnected = True
    c._rightSensorConnected = True
    return c


def _rate_ll_writes(c):
    """(channel, bytes) of every RATE_LL write so far."""
    return [(k.kwargs["channel"], bytes(k.kwargs["data"]))
            for k in c._interface.console.write_i2c_packet.call_args_list
            if k.kwargs["reg_addr"] == RATE_LL_OFFSET]


class TestConnectorLongDarkSkip:
    def test_short_exposure_is_unchanged(self, tmp_path):
        c = _connector(tmp_path, dict(SHORT_EXPOSURE))
        assert c._sync_alt_scan_settings(0xFF, 0xFF) is None
        assert _rate_ll_writes(c) == []
        assert c._app_config.get("altLaserSafetyRateFloorDirty") is not True

    def test_nothing_enabled_touches_nothing(self, tmp_path):
        c = _connector(tmp_path, {})
        assert c._sync_alt_scan_settings(0xFF, 0xFF) is None
        c._interface.console.write_i2c_packet.assert_not_called()

    def test_long_exposure_widens_the_floor_and_returns_the_long_skip(
            self, tmp_path):
        c = _connector(tmp_path, dict(LONG_EXPOSURE))
        assert c._sync_alt_scan_settings(0xFF, 0xFF) == LONG_SKIP
        assert _rate_ll_writes(c) == [(6, FLOOR_BYTES), (7, FLOOR_BYTES)]
        assert c._app_config["altLaserSafetyRateFloorDirty"] is True

    def test_long_pulse_alone_also_needs_it(self, tmp_path):
        c = _connector(tmp_path, {
            "altLaserPulseWidthEnabled": True,
            "altLaserPulseWidthUsec": 2000,
        })
        assert c._sync_alt_scan_settings(0xFF, 0xFF) == LONG_SKIP
        # Pulse-width ceiling (0x04) + TA width, then the rate floor.
        channels = [(k.kwargs["channel"], k.kwargs["reg_addr"]) for k in
                    c._interface.console.write_i2c_packet.call_args_list]
        assert channels == [(6, 0x04), (7, 0x04), (4, 0),
                            (6, RATE_LL_OFFSET), (7, RATE_LL_OFFSET)]

    def test_rewidened_every_long_run(self, tmp_path):
        # Like the PULSE_WIDTH_UL ceiling: a console reconnect between runs
        # (apply_laser_power) puts the stored floor back, so every long run
        # writes it again.
        c = _connector(tmp_path, dict(LONG_EXPOSURE))
        c._sync_alt_scan_settings(0xFF, 0xFF)
        c._sync_alt_scan_settings(0xFF, 0xFF)
        assert len(_rate_ll_writes(c)) == 4

    def test_restore_on_the_first_short_run_then_nothing(self, tmp_path):
        c = _connector(tmp_path, dict(LONG_EXPOSURE))
        c._sync_alt_scan_settings(0xFF, 0xFF)
        c._interface.console.write_i2c_packet.reset_mock()

        c._app_config["altCameraExposureUs"] = 648
        assert c._sync_alt_scan_settings(0xFF, 0xFF) is None
        assert _rate_ll_writes(c) == [(6, STOCK_BYTES), (7, STOCK_BYTES)]
        assert c._app_config["altLaserSafetyRateFloorDirty"] is False

        c._interface.console.write_i2c_packet.reset_mock()
        c._sync_alt_scan_settings(0xFF, 0xFF)
        assert _rate_ll_writes(c) == []

    def test_restore_after_the_toggle_goes_off(self, tmp_path):
        c = _connector(tmp_path, {
            "altCameraSettingsEnabled": False,
            "altCameraSettingsDirty": True,
            "altLaserSafetyRateFloorDirty": True,
        })
        assert c._sync_alt_scan_settings(0xFF, 0xFF) is None
        assert _rate_ll_writes(c) == [(6, STOCK_BYTES), (7, STOCK_BYTES)]
        assert c._app_config["altLaserSafetyRateFloorDirty"] is False

    def test_restore_uses_the_console_user_config(self, tmp_path):
        c = _connector(tmp_path, {"altLaserSafetyRateFloorDirty": True})
        c._interface.console.read_config = MagicMock(
            return_value=SimpleNamespace(json_data={
                "EE_RATE_LL": 23125, "OPT_RATE_LL": 23125}))
        c._sync_alt_scan_settings(0xFF, 0xFF)
        stored = (72266).to_bytes(4, "little")
        assert _rate_ll_writes(c) == [(6, stored), (7, stored)]

    def test_unreadable_user_config_restores_the_stock_floor(self, tmp_path):
        c = _connector(tmp_path, {"altLaserSafetyRateFloorDirty": True})
        c._interface.console.read_config = MagicMock(
            side_effect=RuntimeError("uart timeout"))
        c._sync_alt_scan_settings(0xFF, 0xFF)
        assert _rate_ll_writes(c) == [(6, STOCK_BYTES), (7, STOCK_BYTES)]
        assert c._app_config["altLaserSafetyRateFloorDirty"] is False

    def test_failed_restore_keeps_dirty_for_retry(self, tmp_path):
        c = _connector(tmp_path, {"altLaserSafetyRateFloorDirty": True},
                       write_ok=False)
        assert c._sync_alt_scan_settings(0xFF, 0xFF) is None
        assert c._app_config["altLaserSafetyRateFloorDirty"] is True

        c._interface.console.write_i2c_packet.return_value = True
        c._sync_alt_scan_settings(0xFF, 0xFF)
        assert c._app_config["altLaserSafetyRateFloorDirty"] is False

    def test_failed_widen_keeps_the_default_skip(self, tmp_path):
        """A 2400 µs skip under an unmoved floor latches E-202 on the first
        dark; the run keeps 1800 instead (contaminated darks, laser on)."""
        c = _connector(tmp_path, dict(LONG_EXPOSURE), write_ok=False)
        assert c._sync_alt_scan_settings(0xFF, 0xFF) is None
        assert c._app_config.get("altLaserSafetyRateFloorDirty") is not True

    def test_partial_widen_keeps_the_default_skip_and_stays_dirty(
            self, tmp_path):
        c = _connector(tmp_path, dict(LONG_EXPOSURE))
        c._interface.console.write_i2c_packet.side_effect = [True, False]
        assert c._sync_alt_scan_settings(0xFF, 0xFF) is None
        # EE did move, so the next short run must restore it.
        assert c._app_config["altLaserSafetyRateFloorDirty"] is True

    def test_force_laser_fail_armed_writes_nothing(self, tmp_path):
        c = _connector(tmp_path, {**LONG_EXPOSURE, "forceLaserFail": True})
        assert c._sync_alt_scan_settings(0xFF, 0xFF) is None
        assert _rate_ll_writes(c) == []

        c2 = _connector(tmp_path, {
            "altLaserSafetyRateFloorDirty": True,
            "forceLaserFail": True,
        })
        c2._sync_alt_scan_settings(0xFF, 0xFF)
        assert _rate_ll_writes(c2) == []
        assert c2._app_config["altLaserSafetyRateFloorDirty"] is True

    def test_plain_clinical_build_never_moves_the_floor(self, tmp_path):
        c = _connector(tmp_path, {
            **LONG_EXPOSURE,
            "clinicalMode": True,
            "engineeringMode": False,
        })
        assert c._sync_alt_scan_settings(0xFF, 0xFF) is None
        assert _rate_ll_writes(c) == []

    def test_engineering_unlock_on_clinical_allows_it(self, tmp_path):
        c = _connector(tmp_path, {
            **LONG_EXPOSURE,
            "clinicalMode": True,
            "engineeringMode": True,
        })
        assert c._sync_alt_scan_settings(0xFF, 0xFF) == LONG_SKIP

    def test_invalid_alt_exposure_does_not_count(self, tmp_path):
        c = _connector(tmp_path, {
            "altCameraSettingsEnabled": True,
            "altCameraExposureUs": 5000,       # outside the valid window
            "altCameraGains": [16] * 8,
        })
        assert c._sync_alt_scan_settings(0xFF, 0xFF) is None
        assert _rate_ll_writes(c) == []


# ── the override reaches the trigger that fires ──────────────────────────────

class TestScanStartCarriesTheLongSkip:
    """ScanWorkflow re-sends the interface-resolved trigger config right
    before start_trigger, so only ScanRequest.trigger_config survives."""

    def _request(self, c):
        assert c.startCapture("subj", 5, 0xFF, 0xFF, False) is True
        c._interface.start_scan.assert_called_once()
        return c._interface.start_scan.call_args.args[0]

    def test_long_exposure_scan_requests_the_long_skip(self, tmp_path):
        c = _connector(tmp_path, dict(LONG_EXPOSURE))
        req = self._request(c)
        assert req.trigger_config == LONG_SKIP
        # The floor was widened before the scan was started.
        assert len(_rate_ll_writes(c)) == 2

    def test_short_exposure_scan_uses_the_interface_default(self, tmp_path):
        c = _connector(tmp_path, dict(SHORT_EXPOSURE))
        assert self._request(c).trigger_config is None

    def test_sdk_resolve_puts_the_override_on_top(self):
        # What MotionInterface.resolve_trigger_config does with the request
        # override, on top of the default main.py builds the interface with.
        from omotion.config import merge_trigger_config
        default = merge_trigger_config(DEFAULT_TRIGGER_OVERRIDES)
        resolved = merge_trigger_config(default, LONG_SKIP)
        assert resolved["LaserPulseSkipDelayUsec"] == DARK_SKIP_HIGH_EXPOSURE_US
        assert resolved["LaserPulseDelayUsec"] == LASER_PULSE_DELAY_US
        assert merge_trigger_config(default, None)[
            "LaserPulseSkipDelayUsec"] == DARK_SKIP_DELAY_US


class TestPreflightCarriesTheLongSkip:
    """The preflight's dark frames gate AMBIENT_LIGHT, so it must run the
    same skip as the scan it gates. The SDK's check() builds its own
    ScanRequest, so the connector runs it over a wrapper that adds the
    override; these tests drive the real ContactQualityWorkflow."""

    def _run_check(self, c):
        seen = {}
        done = threading.Event()
        sw = c._interface.scan_workflow

        def _start_scan(request):
            seen["request"] = request
            seen["rate_ll_writes"] = len(_rate_ll_writes(c))
            done.set()
            return True

        sw.start_scan.side_effect = _start_scan
        sw.last_scan_canceled = False
        c.runContactQualityCheck(0x01, 0x01)
        return seen, done

    def test_long_exposure_check_requests_the_long_skip(self, tmp_path):
        c = _connector(tmp_path, dict(LONG_EXPOSURE))
        seen, done = self._run_check(c)
        assert done.wait(5.0), "the check scan never started"
        assert seen["request"].trigger_config == LONG_SKIP
        assert seen["request"].subject_id == "_cq_check"
        assert seen["rate_ll_writes"] == 2   # floor widened before the check
        c._interface.contact_quality_workflow.check.assert_not_called()

    def test_short_exposure_check_uses_the_interface_workflow(self, tmp_path):
        c = _connector(tmp_path, dict(SHORT_EXPOSURE))
        done = threading.Event()
        c._interface.contact_quality_workflow.check.side_effect = (
            lambda **kw: (done.set(), MagicMock())[1])
        c.runContactQualityCheck(0x01, 0x01)
        assert done.wait(5.0), "the check never ran"
        c._interface.scan_workflow.start_scan.assert_not_called()


class TestTriggerOverrideScanWorkflow:
    def test_start_scan_adds_the_override_without_touching_the_original(self):
        from omotion.ScanWorkflow import ScanRequest
        inner = MagicMock()
        inner.start_scan.return_value = True
        wrapper = _TriggerOverrideScanWorkflow(inner, LONG_SKIP)
        original = ScanRequest(
            subject_id="s", duration_sec=1, left_camera_mask=1,
            right_camera_mask=0, trigger_config={"TriggerFrequencyHz": 40})

        assert wrapper.start_scan(original) is True
        sent = inner.start_scan.call_args.args[0]
        assert sent.trigger_config == {"TriggerFrequencyHz": 40, **LONG_SKIP}
        assert sent.subject_id == "s"
        assert original.trigger_config == {"TriggerFrequencyHz": 40}

    def test_everything_else_passes_through(self):
        inner = MagicMock()
        inner.running = True
        inner.last_scan_error = "boom"
        wrapper = _TriggerOverrideScanWorkflow(inner, LONG_SKIP)
        assert wrapper.running is True
        assert wrapper.last_scan_error == "boom"
        wrapper.await_complete(timeout_sec=1.0)
        inner.await_complete.assert_called_once_with(timeout_sec=1.0)
