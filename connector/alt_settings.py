"""Engineering-mode alternative camera / laser register writes (#446, #449, #483, #510)."""
import time

from connector._common import logger
from hardware.motion_config import (
    ALT_PULSE_WIDTH_SAFETY_CEILING_US, FW_DEFAULT_CAMERA_SETTINGS, SAFETY_PULSE_TICK_US,
    TA_PULSE_TICK_US, apply_camera_settings, camera_settings_from_config,
    laser_pulse_width_from_config, safety_pulse_width_ul_writes, ta_pulse_width_write,
)


class AltSettingsMixin:
    """Engineering-mode alternative camera / laser register writes (#446, #449, #483, #510)."""


    def _apply_alt_camera_settings(self, left_mask: int, right_mask: int):
        """Write the Engineering-mode alternative camera settings (#446).

        Runs just before every scan start and every preflight
        signal-quality check (via _sync_alt_scan_settings, #510), after
        the scan flow's configure/trigger-config steps — so the writes
        land on configured, idle cameras and are re-asserted every run (a
        camera power-cycle between scans silently reverts registers to
        firmware defaults; OW_CAMERA_SET_CONFIG skips already-configured
        cameras, so nothing else rewrites them mid-session).

        Camera registers persist until the camera loses power, so turning
        the toggle OFF must actively restore the firmware defaults: the
        persisted ``altCameraSettingsDirty`` flag makes the first scan
        after disabling write the firmware values back once (redundant but
        harmless if the cameras were really power-cycled meanwhile).

        Fail-soft throughout: a failed write logs and the scan proceeds —
        this is an engineering lever, not a scan gate.
        """
        enabled = self._app_config.get("altCameraSettingsEnabled") is True
        dirty = self._app_config.get("altCameraSettingsDirty") is True
        if not enabled and not dirty:
            return

        if enabled:
            settings, reason = camera_settings_from_config(
                self._app_config.get("altCameraExposureUs"),
                self._app_config.get("altCameraGains"),
            )
            if settings is None:
                logger.warning(
                    "Alternative camera settings enabled but invalid (%s); "
                    "NOT writing camera registers for this scan", reason
                )
                return
            label = "alternative"
        else:
            settings = FW_DEFAULT_CAMERA_SETTINGS
            label = "firmware-default restore"

        t0 = time.monotonic()
        failures = []
        wrote_any = False
        for side, mask, connected in (
            ("left", left_mask, self._leftSensorConnected),
            ("right", right_mask, self._rightSensorConnected),
        ):
            if not connected or not mask:
                continue
            sensor = getattr(self._interface, side, None)
            if sensor is None:
                continue
            wrote_any = True
            failures.extend(
                f"{side} {f}"
                for f in apply_camera_settings(sensor, mask, settings)
            )
        logger.info(
            "Camera settings (%s): exposure %d rows (%g us), gains %s -> "
            "left=0x%02X right=0x%02X in %.0f ms",
            label, settings.exposure_rows, settings.exposure_us,
            list(settings.gains), left_mask, right_mask,
            (time.monotonic() - t0) * 1000.0,
        )
        if failures:
            logger.warning(
                "Camera settings: %d register write(s) failed: %s",
                len(failures), "; ".join(failures),
            )

        # Dirty bookkeeping, persisted so an app restart can't strand stale
        # registers. Enabled + wrote → registers differ from firmware
        # defaults. Restore pass with zero failures → clean (a module that
        # wasn't connected lost camera power with its USB, which reset its
        # registers anyway); any failure keeps dirty so the next scan
        # retries the restore.
        if enabled and wrote_any:
            new_dirty = True
        elif not enabled and not failures:
            new_dirty = False
        else:
            new_dirty = dirty
        if new_dirty != dirty:
            self._app_config["altCameraSettingsDirty"] = new_dirty
            self._save_app_config()

    def _apply_alt_laser_pulse_width(self, trigger_data: dict) -> dict:
        """Engineering override: alternative laser pulse width (#449).

        Replaces ``LaserPulseWidthUsec`` in the resolved trigger config
        just before it is pushed to the console — every push, while
        enabled. Experiments only. The safety FPGAs' PULSE_WIDTH_UL
        ceiling is raised to cover the whole width range at scan start
        while this is enabled (#483, ``_apply_alt_safety_pulse_width_ul``),
        so nobody edits the laser-safety config by hand for it any more.

        Unlike the camera-register overrides there is no restore path:
        the scan flow re-resolves the full trigger config from the SDK
        defaults on every push, so disabling the toggle is sufficient.

        Fail-closed on a plain clinical build (#43/#234 pattern): a
        hand-edited config key must never change laser emission for a
        clinical user; the UI to enable this only exists behind the
        engineering unlock anyway.
        """
        if self._app_config.get("altLaserPulseWidthEnabled") is not True:
            return trigger_data
        if (self._app_config.get("clinicalMode", False)
                and not self._app_config.get("engineeringMode", False)):
            logger.warning(
                "altLaserPulseWidthEnabled is set but this is a clinical "
                "build without engineering mode — ignoring the override"
            )
            return trigger_data
        width, reason = laser_pulse_width_from_config(
            self._app_config.get("altLaserPulseWidthUsec")
        )
        if width is None:
            logger.warning(
                "Alternative laser pulse width enabled but invalid (%s); "
                "keeping the resolved trigger config", reason
            )
            return trigger_data
        old = trigger_data.get("LaserPulseWidthUsec")
        trigger_data = dict(trigger_data)
        trigger_data["LaserPulseWidthUsec"] = width
        # WARNING on purpose — an active laser-emission override must be
        # unmissable in any log this scan appears in.
        logger.warning(
            "ALT LASER PULSE WIDTH ACTIVE (experiments only): %s -> %d µs "
            "(safety PULSE_WIDTH_UL ceiling %d µs while enabled)",
            old, width, ALT_PULSE_WIDTH_SAFETY_CEILING_US,
        )
        return trigger_data

    def _apply_alt_ta_pulse_width(self):
        """Write the TA driver FPGA's pulse_width register (#449).

        This register — not the trigger config — shapes the optical
        pulse: the TA driver edge-detects the console's laser trigger and
        times the drive itself (driver_control.v), which is why
        LaserPulseWidthUsec alone changed nothing on the bench
        (2026-08-10: 500/700/1100 µs gates → identical means). The
        setTrigger override stays purely so the console's echoed config
        records the commanded width alongside the scan.

        Same persistence shape as the camera registers: the TA FPGA holds
        the value until console power-off or the next apply_laser_power
        (console connect re-applies laser_params.json), so disabling the
        toggle restores the laser_params baseline once via the persisted
        ``altLaserPulseWidthDirty`` flag. Runs before every scan start
        and preflight signal-quality check (#510), with the trigger off.
        Fail-soft: a failed write logs and the scan proceeds; a failed
        restore keeps the dirty flag for retry.
        """
        enabled = self._app_config.get("altLaserPulseWidthEnabled") is True
        dirty = self._app_config.get("altLaserPulseWidthDirty") is True
        if not enabled and not dirty:
            return
        if (self._app_config.get("clinicalMode", False)
                and not self._app_config.get("engineeringMode", False)):
            return  # same fail-closed gate as the trigger override
        console = getattr(self._interface, "console", None)
        if console is None:
            return

        if enabled:
            width, reason = laser_pulse_width_from_config(
                self._app_config.get("altLaserPulseWidthUsec"))
            if width is None:
                logger.warning(
                    "Alternative laser pulse width enabled but invalid "
                    "(%s); NOT writing TA_PULSE_WIDTH for this scan", reason
                )
                return
            write_kwargs, ticks = ta_pulse_width_write(width)
            label = f"{width} µs = {ticks} ticks (alternative)"
        else:
            write_kwargs, ticks = ta_pulse_width_write(None)
            label = (f"{ticks * TA_PULSE_TICK_US:.1f} µs = {ticks} ticks "
                     f"(laser_params baseline restore)")

        try:
            ok = console.write_i2c_packet(**write_kwargs) is not False
        except Exception as e:
            logger.warning("TA_PULSE_WIDTH write raised: %s", e)
            ok = False
        if ok:
            # WARNING on purpose — this changes actual laser emission.
            logger.warning("TA_PULSE_WIDTH WRITTEN: %s", label)
        else:
            logger.warning(
                "TA_PULSE_WIDTH write FAILED (%s); laser drive keeps its "
                "previous pulse width", label,
            )

        new_dirty = dirty
        if enabled and ok:
            new_dirty = True
        elif not enabled and ok:
            new_dirty = False
        if new_dirty != dirty:
            self._app_config["altLaserPulseWidthDirty"] = new_dirty
            self._save_app_config()

    def _apply_alt_safety_pulse_width_ul(self) -> bool:
        """Raise / restore the safety FPGAs' pulse-width ceiling (#483).

        The Safety EE / OPT FPGAs trip and LATCH the laser (E-202, console
        power-cycle to clear) when the actual pulse exceeds their
        PULSE_WIDTH_UL — 1000 µs stock, ~550 µs on a WI-15-calibrated
        console — so an alternative pulse width above that cannot be
        written safely without raising the ceiling first. While the
        toggle is on this writes ALT_PULSE_WIDTH_SAFETY_CEILING_US
        (2200 µs, the dropdown max) to both registers before every scan
        start and preflight signal-quality check (#510); on the first
        run after the toggle is turned off it restores the unit's
        baseline once — the flash user-config value when the console has
        one, else the laser_params.json stock — via the persisted
        ``altLaserSafetyCeilingDirty`` flag. Runs with the trigger off.

        Returns False only when a write this scan needed actually failed
        (the caller then skips widening the TA pulse); gated no-ops
        return True. Never writes while ``forceLaserFail`` is armed: that
        test stages EE_PULSE_WIDTH_UL = 0 through apply_laser_power, and a
        raise OR a restore here would neutralize it (the sdk#252 bug
        class) — the dirty flag is carried until the flag is cleared.
        Same clinical fail-closed gate as the TA write.
        """
        enabled = self._app_config.get("altLaserPulseWidthEnabled") is True
        dirty = self._app_config.get("altLaserSafetyCeilingDirty") is True
        if not enabled and not dirty:
            return True
        if (self._app_config.get("clinicalMode", False)
                and not self._app_config.get("engineeringMode", False)):
            return True  # same fail-closed gate as the trigger override
        console = getattr(self._interface, "console", None)
        if console is None:
            return True
        if self._force_laser_fail:
            logger.warning(
                "forceLaserFail is armed; leaving the safety PULSE_WIDTH_UL "
                "registers alone (%s deferred)",
                "ceiling raise" if enabled else "ceiling restore",
            )
            return True

        if enabled:
            writes = safety_pulse_width_ul_writes(
                ALT_PULSE_WIDTH_SAFETY_CEILING_US)
        else:
            user_cfg = None
            try:
                cfg_obj = console.read_config()
                json_data = getattr(cfg_obj, "json_data", None)
                if isinstance(json_data, dict):
                    user_cfg = json_data
            except Exception as e:
                logger.warning(
                    "Could not read the console user config before the "
                    "safety PULSE_WIDTH_UL restore (%s); restoring the "
                    "laser_params baseline", e,
                )
            writes = safety_pulse_width_ul_writes(None, user_config=user_cfg)

        failures = []
        wrote_any = False
        for name, write_kwargs, ticks, source in writes:
            label = (f"{name} {ticks * SAFETY_PULSE_TICK_US:.0f} µs = "
                     f"{ticks} ticks ({source})")
            try:
                ok = console.write_i2c_packet(**write_kwargs) is not False
            except Exception as e:
                logger.warning("%s write raised: %s", name, e)
                ok = False
            if ok:
                wrote_any = True
                # WARNING on purpose — this moves a laser-safety limit.
                logger.warning("SAFETY PULSE_WIDTH_UL WRITTEN: %s", label)
            else:
                failures.append(label)
                logger.warning(
                    "SAFETY PULSE_WIDTH_UL write FAILED (%s); the register "
                    "keeps its previous limit", label,
                )

        new_dirty = dirty
        if enabled and wrote_any:
            new_dirty = True
        elif not enabled and not failures:
            new_dirty = False
        if new_dirty != dirty:
            self._app_config["altLaserSafetyCeilingDirty"] = new_dirty
            self._save_app_config()
        return not failures

    def _apply_alt_laser_pulse_width_registers(self):
        """Orchestration for the alt-pulse-width register pair, run before
        every scan start and preflight signal-quality check (#510).

        Order matters because the safety FPGAs act on the actual pulse:
        raise the ceiling BEFORE widening the TA pulse, and narrow the
        pulse BEFORE lowering the ceiling. If raising the ceiling fails,
        the TA override is skipped for this scan — the laser keeps its
        previous width rather than pulsing wider than the interlock
        allows and latching the console.
        """
        if self._app_config.get("altLaserPulseWidthEnabled") is True:
            if not self._apply_alt_safety_pulse_width_ul():
                logger.warning(
                    "NOT writing TA_PULSE_WIDTH this scan: the safety "
                    "PULSE_WIDTH_UL ceiling could not be raised, so a wider "
                    "pulse would latch the laser-safety interlock (E-202)"
                )
                return
            self._apply_alt_ta_pulse_width()
        else:
            self._apply_alt_ta_pulse_width()
            self._apply_alt_safety_pulse_width_ul()

    def _sync_alt_scan_settings(self, left_mask: int, right_mask: int):
        """Bring the hardware in line with the Engineering-mode alternative
        settings toggles — the ONE place those registers are written (#510).

        "Sync", not "apply": each sub-step is apply-or-restore-or-no-op.
        Toggle on → write the alternative values; toggle freshly off
        (persisted dirty flag) → write the firmware/baseline values back
        once; off and clean → no hardware I/O at all, three config reads
        and out.

        Called immediately before each capture start AND before each
        preflight signal-quality check, so the preflight evaluates
        contact under exactly the registers the scan it gates will run
        with. Issue #510: the alt writes used to live only in
        startCapture, so after an app restart (console connect restores
        the ~500 µs laser_params baseline) a 20 µs alt pulse passed the
        preflight at full baseline light, then failed live CQ the moment
        the scan applied the ~3 µs optical pulse. The restore-on-disable
        dirty paths ride along for the same reason: the first preflight
        after a toggle is turned off already runs at the restored
        baseline.
        """
        # #446: write exposure + per-camera analog gain via the SDK, every
        # time — or restore firmware defaults on the first run after the
        # toggle was turned off. No-op when disabled and clean.
        self._apply_alt_camera_settings(left_mask, right_mask)

        # #449: the TA driver FPGA times the optical pulse itself, so the
        # alternative pulse width must be written to its pulse_width
        # register — the setTrigger gate override alone is record-keeping.
        # #483: bracketed by the safety FPGAs' PULSE_WIDTH_UL ceiling —
        # raised before the pulse is widened, restored after it's narrowed.
        self._apply_alt_laser_pulse_width_registers()
