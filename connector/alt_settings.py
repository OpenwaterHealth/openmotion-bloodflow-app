"""Engineering-mode alternative camera / laser register writes (#446, #449, #483, #510)."""
import time

from connector._common import SIDES, logger
from hardware.motion_config import (
    ALT_PULSE_WIDTH_SAFETY_CEILING_US, FW_DEFAULT_CAMERA_SETTINGS, SAFETY_PULSE_TICK_US,
    TA_PULSE_TICK_US, apply_camera_settings, camera_settings_from_config,
    laser_pulse_width_from_config, safety_pulse_width_ul_writes, ta_pulse_width_write,
)


class AltSettingsMixin:
    """Engineering-mode alternative camera / laser register writes (#446, #449, #483, #510).

    The three register groups share one shape. Toggle on: write the
    alternative values before every run. Toggle freshly off: the registers
    keep their values until power-off, so a persisted ``...Dirty`` flag makes
    the next run write the baseline back once. Off and clean: no hardware I/O.
    All of it is fail-soft: a failed write logs and the scan proceeds.
    """

    def _alt_state(self, enabled_key: str, dirty_key: str) -> tuple[bool, bool]:
        return (self._app_config.get(enabled_key) is True,
                self._app_config.get(dirty_key) is True)

    def _settle_alt_dirty(self, dirty_key: str, enabled: bool, wrote_any: bool,
                          failed: bool) -> None:
        """Persist the dirty flag so an app restart can't strand stale
        registers. Enabled and wrote: dirty. Restore with no failure: clean
        (a module that wasn't connected lost power with its USB, which reset
        its registers anyway). Anything else keeps the flag, so a failed
        restore is retried on the next run."""
        if enabled and wrote_any:
            dirty = True
        elif not enabled and not failed:
            dirty = False
        else:
            return
        if dirty != (self._app_config.get(dirty_key) is True):
            self._app_config[dirty_key] = dirty
            self._save_app_config()

    @property
    def _alt_laser_blocked(self) -> bool:
        """Fail-closed on a plain clinical build (#43/#234 pattern): a
        hand-edited config key must never change laser emission for a
        clinical user. The UI for it only exists behind the engineering unlock."""
        return self._clinical and not self._engineering

    @staticmethod
    def _write_i2c(console, name: str, write_kwargs: dict) -> bool:
        try:
            return console.write_i2c_packet(**write_kwargs) is not False
        except Exception as e:
            logger.warning("%s write raised: %s", name, e)
            return False

    def _apply_alt_camera_settings(self, left_mask: int, right_mask: int):
        """Write the alternative camera exposure + gains, or restore the
        firmware defaults once after the toggle is turned off (#446).

        Runs after the scan flow's configure/trigger-config steps, so the
        writes land on configured, idle cameras, and on every run: a camera
        power-cycle between scans silently reverts the registers, and
        OW_CAMERA_SET_CONFIG skips already-configured cameras.
        """
        enabled, dirty = self._alt_state("altCameraSettingsEnabled", "altCameraSettingsDirty")
        if not enabled and not dirty:
            return
        settings, label = FW_DEFAULT_CAMERA_SETTINGS, "firmware-default restore"
        if enabled:
            label = "alternative"
            settings, reason = camera_settings_from_config(
                self._app_config.get("altCameraExposureUs"),
                self._app_config.get("altCameraGains"))
            if settings is None:
                logger.warning("Alternative camera settings enabled but invalid (%s); "
                               "NOT writing camera registers for this scan", reason)
                return

        t0 = time.monotonic()
        failures = []
        wrote_any = False
        connected = {"left": self._leftSensorConnected, "right": self._rightSensorConnected}
        for side, mask in zip(SIDES, (left_mask, right_mask)):
            sensor = getattr(self._interface, side, None)
            if not connected[side] or not mask or sensor is None:
                continue
            wrote_any = True
            failures += [f"{side} {f}" for f in apply_camera_settings(sensor, mask, settings)]
        logger.info(
            "Camera settings (%s): exposure %d rows (%g us), gains %s -> "
            "left=0x%02X right=0x%02X in %.0f ms",
            label, settings.exposure_rows, settings.exposure_us, list(settings.gains),
            left_mask, right_mask, (time.monotonic() - t0) * 1000.0,
        )
        if failures:
            logger.warning("Camera settings: %d register write(s) failed: %s",
                           len(failures), "; ".join(failures))
        self._settle_alt_dirty("altCameraSettingsDirty", enabled, wrote_any, bool(failures))

    def _apply_alt_laser_pulse_width(self, trigger_data: dict) -> dict:
        """Replace ``LaserPulseWidthUsec`` in the resolved trigger config
        just before every push to the console, while enabled (#449).
        Experiments only.

        Record-keeping: the TA register shapes the optical pulse (see
        _apply_alt_ta_pulse_width); this makes the console's echoed config
        carry the commanded width. No restore path: every push re-resolves
        the trigger config from the SDK defaults.
        """
        if self._app_config.get("altLaserPulseWidthEnabled") is not True:
            return trigger_data
        if self._alt_laser_blocked:
            logger.warning("altLaserPulseWidthEnabled is set but this is a clinical "
                           "build without engineering mode — ignoring the override")
            return trigger_data
        width, reason = laser_pulse_width_from_config(
            self._app_config.get("altLaserPulseWidthUsec"))
        if width is None:
            logger.warning("Alternative laser pulse width enabled but invalid (%s); "
                           "keeping the resolved trigger config", reason)
            return trigger_data
        # WARNING on purpose: an active laser-emission override must be
        # unmissable in any log this scan appears in.
        logger.warning(
            "ALT LASER PULSE WIDTH ACTIVE (experiments only): %s -> %d µs "
            "(safety PULSE_WIDTH_UL ceiling %d µs while enabled)",
            trigger_data.get("LaserPulseWidthUsec"), width, ALT_PULSE_WIDTH_SAFETY_CEILING_US,
        )
        return {**trigger_data, "LaserPulseWidthUsec": width}

    def _apply_alt_ta_pulse_width(self):
        """Write the TA driver FPGA's pulse_width register, or restore the
        laser_params baseline once after the toggle is turned off (#449).

        This register, not the trigger config, shapes the optical pulse: the
        TA driver edge-detects the console's laser trigger and times the
        drive itself (driver_control.v), which is why LaserPulseWidthUsec
        alone changed nothing on the bench (2026-08-10: 500/700/1100 µs
        gates gave identical means). The FPGA holds the value until console
        power-off or the next apply_laser_power (console connect). Runs with
        the trigger off.
        """
        enabled, dirty = self._alt_state("altLaserPulseWidthEnabled", "altLaserPulseWidthDirty")
        if (not enabled and not dirty) or self._alt_laser_blocked:
            return
        console = getattr(self._interface, "console", None)
        if console is None:
            return

        width = None   # None restores the laser_params baseline
        if enabled:
            width, reason = laser_pulse_width_from_config(
                self._app_config.get("altLaserPulseWidthUsec"))
            if width is None:
                logger.warning("Alternative laser pulse width enabled but invalid "
                               "(%s); NOT writing TA_PULSE_WIDTH for this scan", reason)
                return
        write_kwargs, ticks = ta_pulse_width_write(width)
        if enabled:
            label = f"{width} µs = {ticks} ticks (alternative)"
        else:
            label = (f"{ticks * TA_PULSE_TICK_US:.1f} µs = {ticks} ticks "
                     f"(laser_params baseline restore)")

        ok = self._write_i2c(console, "TA_PULSE_WIDTH", write_kwargs)
        if ok:
            # WARNING on purpose: this changes actual laser emission.
            logger.warning("TA_PULSE_WIDTH WRITTEN: %s", label)
        else:
            logger.warning("TA_PULSE_WIDTH write FAILED (%s); laser drive keeps its "
                           "previous pulse width", label)
        self._settle_alt_dirty("altLaserPulseWidthDirty", enabled, ok, not ok)

    def _apply_alt_safety_pulse_width_ul(self) -> bool:
        """Raise / restore the safety FPGAs' pulse-width ceiling (#483).

        The Safety EE / OPT FPGAs trip and LATCH the laser (E-202, console
        power-cycle to clear) when the actual pulse exceeds their
        PULSE_WIDTH_UL (1000 µs stock, ~550 µs on a WI-15-calibrated
        console), so a wider alternative pulse needs the ceiling raised
        first. While the toggle is on this writes
        ALT_PULSE_WIDTH_SAFETY_CEILING_US (2200 µs, the dropdown max) to both
        registers; the first run after it is turned off restores the unit's
        baseline once: the flash user-config value when the console has one,
        else the laser_params.json stock. Runs with the trigger off.

        Returns False only when a write this scan needed actually failed
        (the caller then skips widening the TA pulse); gated no-ops return
        True. Never writes while ``forceLaserFail`` is armed: that test
        stages EE_PULSE_WIDTH_UL = 0 through apply_laser_power, and a raise
        OR a restore here would neutralize it (the sdk#252 bug class); the
        dirty flag is carried until the flag is cleared.
        """
        enabled, dirty = self._alt_state("altLaserPulseWidthEnabled", "altLaserSafetyCeilingDirty")
        if (not enabled and not dirty) or self._alt_laser_blocked:
            return True
        console = getattr(self._interface, "console", None)
        if console is None:
            return True
        if self._force_laser_fail:
            logger.warning("forceLaserFail is armed; leaving the safety PULSE_WIDTH_UL "
                           "registers alone (%s deferred)",
                           "ceiling raise" if enabled else "ceiling restore")
            return True

        if enabled:
            writes = safety_pulse_width_ul_writes(ALT_PULSE_WIDTH_SAFETY_CEILING_US)
        else:
            user_cfg = None
            try:
                json_data = getattr(console.read_config(), "json_data", None)
                if isinstance(json_data, dict):
                    user_cfg = json_data
            except Exception as e:
                logger.warning("Could not read the console user config before the "
                               "safety PULSE_WIDTH_UL restore (%s); restoring the "
                               "laser_params baseline", e)
            writes = safety_pulse_width_ul_writes(None, user_config=user_cfg)

        failures = []
        wrote_any = False
        for name, write_kwargs, ticks, source in writes:
            label = f"{name} {ticks * SAFETY_PULSE_TICK_US:.0f} µs = {ticks} ticks ({source})"
            if self._write_i2c(console, name, write_kwargs):
                wrote_any = True
                # WARNING on purpose: this moves a laser-safety limit.
                logger.warning("SAFETY PULSE_WIDTH_UL WRITTEN: %s", label)
            else:
                failures.append(label)
                logger.warning("SAFETY PULSE_WIDTH_UL write FAILED (%s); the register "
                               "keeps its previous limit", label)
        self._settle_alt_dirty("altLaserSafetyCeilingDirty", enabled, wrote_any, bool(failures))
        return not failures

    def _apply_alt_laser_pulse_width_registers(self):
        """Write the alt-pulse-width register pair in the safe order.

        The safety FPGAs act on the actual pulse: raise the ceiling BEFORE
        widening the TA pulse, and narrow the pulse BEFORE lowering the
        ceiling. If raising the ceiling fails, the TA override is skipped for
        this scan: the laser keeps its previous width rather than pulsing
        wider than the interlock allows and latching the console.
        """
        if self._app_config.get("altLaserPulseWidthEnabled") is not True:
            self._apply_alt_ta_pulse_width()
            self._apply_alt_safety_pulse_width_ul()
        elif self._apply_alt_safety_pulse_width_ul():
            self._apply_alt_ta_pulse_width()
        else:
            logger.warning("NOT writing TA_PULSE_WIDTH this scan: the safety "
                           "PULSE_WIDTH_UL ceiling could not be raised, so a wider "
                           "pulse would latch the laser-safety interlock (E-202)")

    def _sync_alt_scan_settings(self, left_mask: int, right_mask: int):
        """Bring the hardware in line with the Engineering-mode alternative
        settings toggles: the ONE place those registers are written (#510).

        Called immediately before each capture start AND each preflight
        signal-quality check, so the preflight evaluates contact under
        exactly the registers the scan it gates will run with. The writes
        used to live only in startCapture, so after an app restart (console
        connect restores the ~500 µs laser_params baseline) a 20 µs alt pulse
        passed the preflight at full baseline light, then failed live CQ the
        moment the scan applied the ~3 µs optical pulse.
        """
        self._apply_alt_camera_settings(left_mask, right_mask)
        self._apply_alt_laser_pulse_width_registers()
