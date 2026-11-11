"""Device connection state, connect-time bring-up, the startup watchdog, fans."""
import threading
import time

from PyQt6.QtCore import QTimer, pyqtProperty, pyqtSignal, pyqtSlot
from connector._common import DEVICES, SIDES, DISCONNECTED, SENSOR_CONNECTED, CONSOLE_CONNECTED, READY, RUNNING, logger
from omotion.config import (
    DEBUG_FLAG_CMD_VERBOSE, DEBUG_FLAG_COMM_VERBOSE, DEBUG_FLAG_FAKE_DATA,
    DEBUG_FLAG_HISTO_CMP, DEBUG_FLAG_HISTO_STALL, DEBUG_FLAG_HISTO_THROTTLE,
    DEBUG_FLAG_SEND_DEFER, DEBUG_FLAG_USB_PRINTF,
)
from hardware.motion_config import select_tec_voltage

# Startup connection watchdog (E-104/E-106). When its deadline finds a
# device still mid-handshake, it checks once more this much later (#667):
# a sensor powered on just before the deadline spends ~2 s in CONNECTING,
# and the SDK monitor connects devices one at a time, so two sensors
# arriving together take ~4.5 s.
_CONNECTION_WATCHDOG_CONNECTING_GRACE_SEC = 10
_CONNECTION_WATCHDOG_TOAST_TAG = "connection-watchdog"

def _should_abort_on_disconnect(
    name: str,
    capture_running: bool,
    active_sides: set,
) -> bool:
    """Decide whether losing device ``name`` must abort the scan (issue #387).

    A device unplugged mid-scan aborts with the E-304 critical modal only
    when a capture is actually running AND the dropped device is part of
    THIS scan — the console (always; it drives the trigger/laser) or a
    sensor whose camera mask was non-zero at scan start (``active_sides``,
    snapshotted in startCapture). An idle / masked-out sensor unplugged
    mid-scan, or any disconnect while not capturing, must not abort.
    """
    return bool(capture_running) and name in active_sides

def _disconnect_toast_text(name: str) -> str:
    """Toast copy for a device that dropped off USB (issue #387). Used for the
    low-key notice when the drop does NOT abort a running scan (idle, or a
    non-participating device)."""
    label = {
        "console": "Console",
        "left": "Left sensor",
        "right": "Right sensor",
    }.get(name, name)
    return f"{label} disconnected"


class DevicesMixin:
    """Device connection state, connect-time bring-up, the startup watchdog, fans."""

    def _setup_devices(self, cfg: dict) -> None:
        # Connection watchdog (E-104/E-106): one-shot check armed at startup
        # that flags expected devices that never enumerated. 0 disables it.
        self._connection_timeout_sec = float(cfg.get("connectionTimeoutSec", 12))
        self._require_console = bool(cfg.get("requireConsole", True))
        self._min_sensors = int(cfg.get("minSensors", 1))
        # True while the watchdog's E-104/E-106 toast may still be up, so a
        # device that connects afterwards can take it down (#667).
        self._connection_watchdog_toast_up = False

        self._sensor_debug_logging = bool(cfg.get("sensorDebugLogging", False))
        self._camera_fake_data = bool(cfg.get("cameraFakeData", False))
        self._histo_throttle = bool(cfg.get("histoThrottle", False))
        self._histo_cmp = bool(cfg.get("histoCmp", False))
        self._histo_stall_test = bool(cfg.get("debugHistoStallTest", False))
        self._send_data_defer = bool(cfg.get("deferHistoSend", False))
        self._comm_verbose = bool(cfg.get("commVerbose", False))
        self._verbose_command_handling = bool(cfg.get("verboseCommandHandling", False))
        self._power_off_unused_cameras = bool(cfg.get("powerOffUnusedCameras", False))

        console, left, right = self._interface.is_device_connected()
        self._consoleConnected = console
        self._leftSensorConnected = left
        self._rightSensorConnected = right
        # Per-device "reached CONNECTED" latch backing the first-loss edge
        # detection in _on_handle_state_changed_impl (issue #489). The
        # _xxxConnected flags can't serve: the console one is cleared on the
        # intermediate DISCONNECTING event, before the DISCONNECTED event
        # that does the surfacing arrives.
        self._device_was_connected: dict[str, bool] = {
            "console": bool(console), "left": bool(left), "right": bool(right)}
        # Connect-time bring-up, per device (see _schedule_device_init).
        # _device_init_pending counts bring-ups scheduled or running, under
        # _device_init_lock: +1 on the GUI thread when one is scheduled, -1
        # on the worker when it drains (success OR failure). Non-zero blocks
        # pipeline starts via _ensure_idle / deviceInitBusy (issue #303).
        # _device_connect_gen counts CONNECTED events, so a bring-up left
        # over from an earlier connection stops at its next step, and
        # _device_init_run_locks runs one bring-up per device at a time.
        self._device_init_pending: dict[str, int] = {n: 0 for n in DEVICES}
        self._device_init_lock = threading.Lock()
        self._device_connect_gen: dict[str, int] = {n: 0 for n in DEVICES}
        self._device_init_run_locks = {n: threading.Lock() for n in DEVICES}
        self._running = False
        self._state = DISCONNECTED
        self._last_fan_status: dict[str, bool | None] = {"left": None, "right": None}
        # Console fan cache. Seeded True because the bring-up forces
        # set_fan_speed(100) at console connect (_init_console_fan).
        self._console_fan_on: bool = True
        # Firmware versions (e.g. "v1.2.3") and programmed serial numbers,
        # cached on connect by _log_device_stats and cleared on disconnect.
        # "" = unknown / unprogrammed (#529).
        self._firmware_versions: dict[str, str] = {n: "" for n in DEVICES}
        self._device_serials: dict[str, str] = {n: "" for n in DEVICES}

    # Ensure signals are correctly defined
    signalConnected = pyqtSignal(str, str)  # (descriptor, port)
    signalDisconnected = pyqtSignal(str, str)  # (descriptor, port)
    connectionStatusChanged = pyqtSignal()  # 🔹 New signal for connection updates
    # Notify for ``deviceInitBusy`` (issues #303, #667): a device's
    # connect-time bring-up is scheduled or running. The drain-side emit
    # fires on the init worker thread; Qt auto-queues the cross-thread
    # delivery to QML/main-thread consumers.
    deviceInitBusyChanged = pyqtSignal()
    stateChanged = pyqtSignal()  # Signal to notify QML of state changes
    consoleFanChanged = pyqtSignal()
    # Sensor-module fan state cache (issue #463) — notify for the
    # left/rightSensorFanOn properties behind the Engineering switches.
    sensorFanChanged = pyqtSignal()
    # Per-device firmware versions, refreshed on every (dis)connect by
    # _log_device_stats. Surfaced to Settings → System Information.
    firmwareVersionsChanged = pyqtSignal()
    # Per-device programmed serial numbers, cached on connect and cleared
    # on disconnect (#529). Notify for the *SerialNumber properties behind
    # Settings → About.
    deviceIdentityChanged = pyqtSignal()

    def _compute_sensor_debug_flags(self) -> int:
        """Compute sensor debug flag bitfield from current config booleans."""
        flags = 0
        if self._sensor_debug_logging:
            flags |= DEBUG_FLAG_USB_PRINTF
        if self._camera_fake_data:
            flags |= DEBUG_FLAG_FAKE_DATA
        if self._histo_throttle:
            flags |= DEBUG_FLAG_HISTO_THROTTLE
        if self._comm_verbose:
            flags |= DEBUG_FLAG_COMM_VERBOSE
        if self._verbose_command_handling:
            flags |= DEBUG_FLAG_CMD_VERBOSE
        if self._histo_cmp:
            flags |= DEBUG_FLAG_HISTO_CMP
        if self._send_data_defer:
            flags |= DEBUG_FLAG_SEND_DEFER
        if self._histo_stall_test:
            flags |= DEBUG_FLAG_HISTO_STALL
        return flags

    def _apply_sensor_debug_flags(self) -> None:
        """Re-push the recomputed debug-flag bitmask to every connected sensor.

        Unlike ``_init_sensor``, this writes even when ``flags == 0`` so
        that turning the last flag off actually clears it on the firmware.
        Used by the live Settings → Engineering toggles (``setSensorDebugFlag``)
        so no reconnect/restart is needed.
        """
        flags = self._compute_sensor_debug_flags()
        for side in ("left", "right"):
            try:
                sensor = (
                    getattr(self._interface, side, None)
                    if self._interface else None
                )
            except Exception:
                sensor = None
            if sensor is None or not sensor.is_connected():
                continue
            logger.info("Re-applying debug flags 0x%x on %s sensor", flags, side)
            if not sensor.set_debug_flags(flags):
                logger.warning("Failed to set debug flags on %s sensor", side)

    # --- CONNECT-TIME BRING-UP ---------------------------------------------
    # Every device gets a command sequence once it reaches CONNECTED. It runs
    # on a worker thread, never in the state-change slot: SDK state changes
    # are queued to the GUI thread, and the console's sequence alone is
    # ~3.7 s of UART round-trips. On the GUI thread that froze the app, and
    # every queued state change and timer waited behind it (#667: the
    # startup watchdog fired late and missed a sensor's CONNECTING event).

    def _device_init_note(self, name: str, delta: int) -> None:
        """Adjust the per-device bring-up counter (issue #303).

        +1 when a bring-up is scheduled (GUI thread), -1 when the worker
        drains — success or failure alike, so a failed bring-up can't leak
        the busy state. Emits deviceInitBusyChanged only on the busy/idle
        edge; the drain-side emission happens on the worker thread and Qt
        auto-queues it to main-thread/QML consumers.
        """
        with self._device_init_lock:
            before = any(v > 0 for v in self._device_init_pending.values())
            new = self._device_init_pending.get(name, 0) + delta
            self._device_init_pending[name] = max(0, new)
            after = any(v > 0 for v in self._device_init_pending.values())
        if before != after:
            self.deviceInitBusyChanged.emit()

    @pyqtProperty(bool, notify=deviceInitBusyChanged)
    def deviceInitBusy(self) -> bool:
        """True while any device's connect-time bring-up is scheduled or
        running (issue #303). QML holds Start/Check disabled on this;
        _ensure_idle refuses pipeline starts with it up. Re-engages on every
        (re)connect because _schedule_device_init re-runs then."""
        with self._device_init_lock:
            return any(v > 0 for v in self._device_init_pending.values())

    def _schedule_device_init(self, name: str) -> None:
        """Run ``name``'s connect-time bring-up on a worker thread.

        The busy gate goes up now (issue #303): a Start clicked before or
        during the bring-up collides with it. For the console that means a
        scan before the laser registers are loaded; for a sensor, a camera
        wedged until a DUT power-cycle. Sensors wait 1 s first for USB to
        settle."""
        gen = self._device_connect_gen[name]
        self._device_init_note(name, +1)
        delay_ms = 0 if name == "console" else 1000
        QTimer.singleShot(
            delay_ms, lambda: self._start_device_init_worker(name, gen))

    def _start_device_init_worker(self, name: str, gen: int) -> None:
        """Spawn the bring-up worker. Worker-safety: the SDK serializes
        per-device command I/O (MotionUart._io_lock for the console,
        CommInterface._io_lock/_send_lock for sensors), every signal
        emitted from the sequence queues to the main thread, _raise_critical
        is worker-safe by contract and the audit log takes its own lock —
        same pattern as the CQ-check and past-scan-load workers."""
        threading.Thread(
            target=self._run_device_init, args=(name, gen),
            name=f"{name}-init", daemon=True,
        ).start()

    def _run_device_init(self, name: str, gen: int) -> None:
        """Run one bring-up, on its worker (unit tests call it directly).

        One at a time per device: the bring-up for a newer connection waits
        for the old one, which stops at its next step (_init_is_current).
        Never raises — an uncaught exception on a plain worker thread would
        vanish to stderr."""
        started = time.monotonic()
        try:
            with self._device_init_run_locks[name]:
                if name == "console":
                    self._init_console(gen)
                else:
                    self._init_sensor(name, gen)
        except Exception:
            logger.exception("connect-time init failed for %s", name)
        finally:
            logger.info("Connect-time init for %s done in %.2f s", name,
                        time.monotonic() - started)
            # Drop the busy gate on success AND failure — a leaked busy
            # state would lock Start/Check forever (issue #303).
            self._device_init_note(name, -1)

    def _init_is_current(self, name: str, gen: int) -> bool:
        """True while the connection a bring-up was scheduled for is still
        up: no newer CONNECTED event, and the SDK still reports the device
        connected."""
        if gen != self._device_connect_gen[name]:
            return False
        try:
            return bool(getattr(self._interface, name).is_connected())
        except Exception:
            return False

    def _init_console(self, gen: int) -> None:
        """Console bring-up, once per connection: the laser-driver FPGA
        registers and the TEC/fan settings do not survive a power cycle.

        Each step stands alone, so one that fails cannot skip the laser
        parameters after it. If the console disconnects mid-sequence the
        rest is skipped; the disconnect has its own notice."""
        steps = [
            ("identity and calibration", self._interface.log_console_info),
            ("TEC voltage", self._init_console_tec_voltage),
            ("fan speed", self._init_console_fan),
        ]
        if self._console_debug_logging:
            steps.append(("debug logging", self._init_console_debug_logging))
        steps += [
            ("laser parameters", lambda: self._init_console_laser(gen)),
            ("TEC trip", lambda: self.apply_tec_trip_from_config(self._interface)),
            ("device stats", lambda: self._log_device_stats("console")),
        ]
        for label, step in steps:
            if not self._init_is_current("console", gen):
                logger.info("Console init stopped before %s: the console "
                            "disconnected", label)
                return
            try:
                step()
            except Exception as e:
                logger.warning("Console init: %s failed: %s", label, e)

    def _init_console_tec_voltage(self) -> None:
        # EVT2 consoles (board-ID strap 1) need +1.16 V on the TEC DAC to
        # hold the lasers at 25 C; DVT and beyond use TEC_VOLTAGE_DEFAULT
        # from config/tec_params.py (issue #269).
        voltage, why = select_tec_voltage(
            self._interface.console, self._tec_voltage_params)
        if self._interface.console.tec_voltage(voltage):
            logger.info(f"Console TEC voltage set to {voltage}V ({why})")
        else:
            logger.error(
                f"Failed to set console TEC voltage to {voltage}V ({why})")

    def _init_console_fan(self) -> None:
        if self._interface.console.set_fan_speed(fan_speed=100):
            logger.info("Console fan speed set to 100%")
            self._console_fan_on = True
            self.consoleFanChanged.emit()
        else:
            logger.error("Failed to set console fan speed")

    def _init_console_debug_logging(self) -> None:
        # The flag is RAM-only on the MCU, so it resets across reconnects
        # and power cycles. Only re-applied when on: the firmware default
        # is off.
        if self._interface.console.enable_usb_printf(True):
            logger.info("Console debug logging re-enabled")
        else:
            logger.error("Failed to re-enable console debug logging")

    def _init_console_laser(self, gen: int) -> None:
        """Load the laser-driver registers (volatile across power cycles;
        scans don't re-apply them). E-103 when they aren't loaded and the
        console is still connected, since a scan would then run dark."""
        try:
            ok = self.set_laser_power_from_config(self._interface)
        except Exception as e:
            logger.error("Applying laser power params raised: %s", e)
            ok = False
        if ok:
            logger.info("Laser power params applied from config")
        elif self._init_is_current("console", gen):
            logger.error("Failed to apply laser power params from config")
            self._raise_critical(
                "E-103", detail="laser power params not applied")
        else:
            logger.warning("Laser power params not applied: the console "
                           "disconnected")

    def _init_sensor(self, side: str, gen: int) -> None:
        """Sensor bring-up: debug flags, I2C health, camera ID cache,
        identity log."""
        if not self._init_is_current(side, gen):
            return

        # Apply sensor debug flags (USB has had time to settle after connection)
        flags = self._compute_sensor_debug_flags()
        try:
            sensor = getattr(self._interface, side, None) if self._interface else None
        except Exception:
            sensor = None
        if flags != 0 and sensor is not None and sensor.is_connected():
            logger.info(
                "Setting debug flags 0x%x on %s sensor "
                "(debug_logging=%s, fake_data=%s, histoThrottle=%s, histoCmp=%s, "
                "deferHistoSend=%s, commVerbose=%s, verboseCommand=%s)",
                flags,
                side,
                self._sensor_debug_logging,
                self._camera_fake_data,
                getattr(self, "_histo_throttle", False),
                getattr(self, "_histo_cmp", False),
                getattr(self, "_send_data_defer", False),
                getattr(self, "_comm_verbose", False),
                getattr(self, "_verbose_command_handling", False),
            )
            if not sensor.set_debug_flags(flags):
                logger.warning("Failed to set debug flags on %s sensor", side)
        elif flags != 0:
            logger.info(
                "Skipping debug flag set on %s sensor (flags=0x%x, sensor_present=%s, connected=%s)",
                side,
                flags,
                sensor is not None,
                getattr(sensor, "is_connected", lambda: False)() if sensor else False,
            )

        self.getFanControlStatus(side)

        # Surface the firmware's boot-time I2C self-check (E-101/E-102) — the
        # canonical "I2C check at the beginning". Best-effort: never blocks
        # init, just raises the modal if the sensor reports trouble.
        self._check_sensor_i2c_health(side)

        # Power on all cameras, fill the ID cache (serial numbers, connection info), then power off
        try:
            sensor = getattr(self._interface, side, None) if self._interface else None
            if sensor is not None and sensor.is_connected():
                enable_power = getattr(sensor, "enable_camera_power", None)
                disable_power = getattr(sensor, "disable_camera_power", None)
                refresh_cache = getattr(sensor, "refresh_id_cache", None)
                if enable_power and disable_power and refresh_cache:
                    if enable_power(0xFF):
                        time.sleep(0.5)  # settle time
                        refresh_cache()
                        if self._power_off_unused_cameras:
                            disable_power(0xFF)
                            time.sleep(0.05)
                    else:
                        logger.warning(
                            "Could not power on cameras on %s sensor for ID cache fill",
                            side,
                        )
                        self._raise_critical("E-105", detail=f"{side} sensor")
                        refresh_cache()  # try anyway in case some cameras are already on
                elif refresh_cache:
                    refresh_cache()  # fallback: fill cache without power cycle (may get zeros for off cameras)
        except Exception as e:
            logger.debug("Could not refresh sensor ID cache for %s: %s", side, e)

        # Log the sensor's hardware identity (firmware, hw_id, serial, and
        # all 8 camera UIDs) once per sensor connect, after the cache fill.
        # The SDK's log_sensor_info emits the guarded LEFT/RIGHT block; this
        # mirrors log_console_info on console connect and log_system_info at
        # startup — the three "log the configuration" calls.
        self._interface.log_sensor_info(side)
        self._log_device_stats(side)

        self.connectionStatusChanged.emit()

    def _check_sensor_i2c_health(self, side: str) -> None:
        """Raise E-101/E-102 if a sensor's boot-time I2C self-check is bad.

        Reads the firmware's cached snapshot (no disruptive rescan). Never
        raises out — it's a best-effort surfacing layer over the SDK's
        ``MotionSensor.i2c_health``.
        """
        try:
            sensor = getattr(self._interface, side, None) if self._interface else None
            if sensor is None or not sensor.is_connected():
                return
            health = getattr(sensor, "i2c_health", None)
            if health is None:
                self._raise_critical("E-102", detail=f"{side} sensor")
            elif not health.get("all_present", False):
                missing = self._i2c_missing_devices(health)
                self._raise_critical("E-101", detail=f"{side} sensor: {missing}")
        except Exception:
            logger.exception("I2C health check failed for %s sensor", side)

    def _arm_connection_watchdog(self) -> None:
        """Schedule the one-shot startup connection check (E-104/E-106)."""
        if self._connection_timeout_sec > 0:
            QTimer.singleShot(
                int(self._connection_timeout_sec * 1000),
                self._check_connection_watchdog,
            )

    def _sdk_state(self, name: str):
        """The SDK's live connection state for ``name`` (None if unreadable).

        The startup watchdog reads this rather than the connector's
        ``_xxxConnected`` flags, which only catch up once the queued
        state-change event is handled: a deadline that fires while the GUI
        thread is busy would otherwise miss a device the SDK already reports
        CONNECTING or CONNECTED (#667)."""
        try:
            return getattr(self._interface, name).state
        except Exception:
            return None

    def _sdk_connected(self, name: str) -> bool:
        from omotion import ConnectionState
        return self._sdk_state(name) == ConnectionState.CONNECTED

    def _connection_shortfall(self) -> tuple[bool, int, bool]:
        """``(console_missing, n_sensors, sensors_missing)`` from the SDK's
        live states, against ``requireConsole`` / ``minSensors``."""
        console_missing = self._require_console and not self._sdk_connected("console")
        n_sensors = (int(self._sdk_connected("left"))
                     + int(self._sdk_connected("right")))
        return console_missing, n_sensors, n_sensors < self._min_sensors

    def _check_connection_watchdog(self, final: bool = False) -> None:
        """Warn about devices that never connected within the startup timeout.

        One-shot: reads the SDK's live device states. A missing console
        (E-104) and/or too few sensors (E-106) are non-blocking — they surface
        as a single yellow warning toast (bottom-right), not the critical
        modal, since the fix is usually just "plug it in". If *both* are
        missing the toast collapses to a plain "System not found". Disconnects
        that happen *after* startup are handled by the connection-status UI.

        A device still CONNECTING at the deadline is not missing yet (#667):
        QA powered the system on after launching the app, and E-106 fired
        while the sensor was mid-handshake. The check then runs once more
        (``final``) after ``_CONNECTION_WATCHDOG_CONNECTING_GRACE_SEC`` and
        reports whatever is still not connected, connecting or not, so a
        device that keeps failing its handshake (CONNECTING -> DISCONNECTED
        -> CONNECTING ...) is still reported.
        """
        try:
            from omotion import ConnectionState
            console_missing, n_sensors, sensors_missing = (
                self._connection_shortfall())
            if not console_missing and not sensors_missing:
                return

            if not final:
                expected = ((["console"] if console_missing else [])
                            + (["left", "right"] if sensors_missing else []))
                connecting = [n for n in expected
                              if self._sdk_state(n) == ConnectionState.CONNECTING]
                if connecting:
                    logger.info(
                        "Connection watchdog: %s still connecting at the "
                        "deadline — checking again in %d s",
                        ", ".join(connecting),
                        _CONNECTION_WATCHDOG_CONNECTING_GRACE_SEC)
                    QTimer.singleShot(
                        _CONNECTION_WATCHDOG_CONNECTING_GRACE_SEC * 1000,
                        lambda: self._check_connection_watchdog(final=True))
                    return

            if console_missing and sensors_missing:
                logger.warning(
                    "Connection watchdog: console and sensors missing "
                    "(E-104/E-106: %d of %d sensors)", n_sensors, self._min_sensors)
                msg = ("System not found. Check that the console and sensor "
                       "are connected and powered on.")
            elif console_missing:
                logger.warning("Connection watchdog: console not detected (E-104)")
                msg = ("Console not detected. Check the console USB cable and "
                       "power, then reconnect.")
            else:
                logger.warning(
                    "Connection watchdog: sensor not detected "
                    "(E-106: %d of %d)", n_sensors, self._min_sensors)
                msg = ("Sensor not detected. Check the sensor USB cable and "
                       "power, then reconnect.")
            # Single (tagged) yellow toast — never blocks the UI like the
            # critical modal. Auto-dismisses after 10 s so it doesn't nag while
            # the user explores the no-device sample scan (#314); the user can
            # still dismiss it early via the ✕, and it comes down by itself
            # once the missing devices connect (_withdraw_connection_watchdog_toast).
            self.notify(msg, "warning", duration_ms=10000, dismissible=True,
                        tag=_CONNECTION_WATCHDOG_TOAST_TAG)
            self._connection_watchdog_toast_up = True
            self._maybe_offer_sample_scan()
        except Exception:
            logger.exception("connection watchdog check failed")

    def _withdraw_connection_watchdog_toast(self) -> None:
        """Take the watchdog's E-104/E-106 toast down once nothing it warned
        about is missing any more (#667). It used to stay up for its full
        10 s after the device connected, which read as a disconnection.

        A "System not found" toast stays up when only the console comes
        back: its advice to check the sensor still applies."""
        if not self._connection_watchdog_toast_up:
            return
        console_missing, _n_sensors, sensors_missing = (
            self._connection_shortfall())
        if console_missing or sensors_missing:
            return
        self._connection_watchdog_toast_up = False
        logger.info("Connection watchdog: the devices it warned about have "
                    "connected — withdrawing its warning")
        self.dismissNotification(_CONNECTION_WATCHDOG_TOAST_TAG)

    @staticmethod
    def _i2c_missing_devices(health: dict) -> str:
        """Human-readable summary of which I2C devices didn't respond."""
        parts = []
        if not health.get("mux", True):
            parts.append("mux")
        if not health.get("imu", True):
            parts.append("imu")
        cams = [i for i, ok in enumerate(health.get("cameras", [])) if not ok]
        if cams:
            parts.append("cameras " + ",".join(str(i) for i in cams))
        fpgas = [i for i, ok in enumerate(health.get("fpgas", [])) if not ok]
        if fpgas:
            parts.append("fpgas " + ",".join(str(i) for i in fpgas))
        return "; ".join(parts) or "unknown device"

    @pyqtProperty(bool, notify=connectionStatusChanged)
    def leftSensorConnected(self):
        """Expose Sensor connection status to QML."""
        return self._leftSensorConnected

    @pyqtProperty(bool, notify=connectionStatusChanged)
    def rightSensorConnected(self):
        """Expose Sensor connection status to QML."""
        return self._rightSensorConnected

    @pyqtProperty(bool, notify=connectionStatusChanged)
    def consoleConnected(self):
        """Expose Console connection status to QML."""
        return self._consoleConnected

    @pyqtProperty(str, notify=firmwareVersionsChanged)
    def consoleFirmwareVersion(self) -> str:
        """Console firmware version cached at connect time (empty when
        unknown / disconnected). Shown in Settings → System Information."""
        return self._firmware_versions["console"]

    @pyqtProperty(str, notify=firmwareVersionsChanged)
    def leftSensorFirmwareVersion(self) -> str:
        """Left sensor firmware version; see consoleFirmwareVersion."""
        return self._firmware_versions["left"]

    @pyqtProperty(str, notify=firmwareVersionsChanged)
    def rightSensorFirmwareVersion(self) -> str:
        """Right sensor firmware version; see consoleFirmwareVersion."""
        return self._firmware_versions["right"]

    @pyqtProperty(str, notify=deviceIdentityChanged)
    def consoleSerialNumber(self) -> str:
        """Console EEPROM serial cached at connect time ("" when
        unprogrammed / disconnected). Shown in Settings → About (#529)."""
        return self._device_serials["console"]

    @pyqtProperty(str, notify=deviceIdentityChanged)
    def leftSensorSerialNumber(self) -> str:
        """Left sensor module serial; see consoleSerialNumber."""
        return self._device_serials["left"]

    @pyqtProperty(str, notify=deviceIdentityChanged)
    def rightSensorSerialNumber(self) -> str:
        """Right sensor module serial; see consoleSerialNumber."""
        return self._device_serials["right"]

    @pyqtProperty(bool, notify=consoleFanChanged)
    def consoleFanOn(self) -> bool:
        """Cached last-set console-fan state (True=on). Reflects the
        connect-time 100% default and any setConsoleFan call. Read by the
        Engineering settings switch when the Settings modal opens."""
        return self._console_fan_on

    @pyqtProperty(bool, notify=sensorFanChanged)
    def leftSensorFanOn(self) -> bool:
        """Cached left sensor-module fan state (issue #463). Seeded from
        the hardware by the connect-time getFanControlStatus poll, updated
        by setFanControl, and reset to unknown (shown Off) at disconnect.
        Read by the Engineering settings switch."""
        return bool(self._last_fan_status.get("left"))

    @pyqtProperty(bool, notify=sensorFanChanged)
    def rightSensorFanOn(self) -> bool:
        """Right-side counterpart of leftSensorFanOn."""
        return bool(self._last_fan_status.get("right"))

    @pyqtSlot(bool, result=bool)
    def setConsoleFan(self, on: bool) -> bool:
        """Drive the console fan to 100% (on) or 0% (off).

        Updates the cached state + notifies QML on success. Guarded and
        wrapped like the other console slots so a mid-flight disconnect
        can't raise out of the Qt slot and kill the process.
        """
        if not self._consoleConnected:
            logger.error("Console not connected — cannot set console fan")
            return False
        try:
            speed = 100 if on else 0
            # set_fan_speed returns the duty cycle that was set (0..100),
            # or -1 on OW_ERROR. Check against the -1 sentinel — NOT
            # truthiness — because turning the fan off returns 0, which
            # is falsy but successful.
            if self._interface.console.set_fan_speed(fan_speed=speed) != -1:
                self._console_fan_on = bool(on)
                self.consoleFanChanged.emit()
                logger.info("Console fan set to %s", "ON" if on else "OFF")
                return True
            logger.error("Failed to set console fan")
            return False
        except Exception as e:  # noqa: BLE001 — slot must not raise
            logger.error("Error setting console fan: %s", e)
            return False

    @pyqtProperty(int, notify=stateChanged)
    def state(self):
        """Expose state as a QML property."""
        return self._state

    # --- DEVICE CONNECTION / DISCONNECTION / STATE MANAGEMENT METHODS ---
    def _on_handle_state_changed(self, handle, old, new, reason):
        """Single state-change handler wired to console/left/right.

        Replaces the old signal_connect/signal_disconnect pair. The handle
        argument is the stable MotionConsole/MotionSensor instance; we
        switch on handle.name. ``new`` is a ConnectionState enum.
        """
        try:
            self._on_handle_state_changed_impl(handle, old, new, reason)
        except Exception as e:
            # Top-level safety net: the SDK emits from its
            # connection-monitor thread, and Qt queues the call to this
            # slot onto the GUI thread (so it runs late whenever the GUI
            # thread is busy, e.g. in console connect setup). Any uncaught
            # exception here propagates as "Unhandled Python exception"
            # at the Qt boundary and terminates the bloodflow process.
            # Log loudly and swallow — the worst case is a stale UI
            # state until the next connection event re-fires the slot.
            logger.exception(
                "_on_handle_state_changed crashed for handle=%s "
                "old=%s new=%s reason=%s — swallowing to keep app alive",
                getattr(handle, "name", "?"), old, new, reason,
            )

    def _on_handle_state_changed_impl(self, handle, old, new, reason):
        from omotion import ConnectionState

        is_now_connected = (new == ConnectionState.CONNECTED)
        is_now_lost = (new == ConnectionState.DISCONNECTED)
        name = handle.name

        # A device we are deliberately flashing drops into DFU and re-enumerates
        # as a different USB device; that disconnect is expected. Suppress it
        # entirely (no state mutation, no UI thrash) so the connector's
        # connected-flags + _state stay consistent until the post-flash
        # power-cycle reconnect re-runs normal connect handling.
        if is_now_lost and self._firmware_update_in_progress == name:
            logger.info("Ignoring expected DFU disconnect for %s during "
                        "firmware update", name)
            return

        if name == "console":
            self._consoleConnected = is_now_connected
        if is_now_connected:
            self._on_device_connected(name, reason)
        elif is_now_lost:
            self._on_device_lost(name, reason)
        # CONNECTING / DISCONNECTING are intermediate; UI doesn't need to
        # fire a connect/disconnect signal for those, and emitting
        # connectionStatusChanged on every intermediate transition would
        # cause QML observers to re-poll device state 4× per disconnect
        # cycle (once for each of CONNECTED→DISCONNECTING→DISCONNECTED→
        # CONNECTING→CONNECTED).
        if is_now_connected or is_now_lost:
            self.connectionStatusChanged.emit()
            self.update_state()

    def _on_device_connected(self, name: str, reason) -> None:
        """Bookkeeping for a device that reached CONNECTED. No device I/O
        here: this runs on the GUI thread, so the bring-up goes to a worker
        (_schedule_device_init)."""
        self._device_connect_gen[name] += 1
        if name == "console":
            # Start of the safety-status grace period (readSafetyStatus).
            self._console_connected_at = time.monotonic()
        elif name == "left":
            self._leftSensorConnected = True
        else:
            self._rightSensorConnected = True
        logger.info("Handle %s -> CONNECTED (%s)", name, reason)
        # Before anything reacts to the connect: this raises the busy gate.
        self._schedule_device_init(name)
        # Re-arm the first-loss edge (issue #489) — the next drop is a
        # real one worth surfacing again.
        self._device_was_connected[name] = True
        # A '<device> disconnected' card must not outlive the reconnect
        # (issue #489): during a console power-cycle the cards kept
        # saying 'disconnected' after the devices were already back.
        self.dismissNotification(f"disconnect_{name}")
        self._withdraw_connection_watchdog_toast()
        self.signalConnected.emit(name, "")
        self._audit.log("device_connected",
                        {"device": name, "reason": str(reason)})

    def _on_device_lost(self, name: str, reason) -> None:
        """Bookkeeping and user-facing surfacing for a device that reached
        DISCONNECTED."""
        if name == "console":
            self._console_connected_at = None
            # A dead console can't be driving the laser trigger, and it
            # can't deliver the SDK's trigger-OFF event either (its
            # stop_trigger raises during teardown). Close the trigger
            # clock at detection time so the notes duration line and the
            # header counter stop here instead of counting several more
            # seconds of scan teardown (issue #201).
            if self._trigger_state == "ON" or self._trigger_on_mono is not None:
                logger.warning(
                    "Console lost while trigger ON — closing the trigger "
                    "clock at disconnect detection (%s)", reason,
                )
                self._trigger_clock_close()
                self._trigger_state = "OFF"
                self.triggerStateChanged.emit()
        else:
            if name == "left":
                self._leftSensorConnected = False
            else:
                self._rightSensorConnected = False
            self._last_fan_status[name] = None
            self.sensorFanChanged.emit()
            try:
                sensor = getattr(self._interface, name)
                if getattr(sensor, "clear_id_cache", None):
                    sensor.clear_id_cache()
            except Exception:
                logger.debug("clear_id_cache failed for %s", name, exc_info=True)

        logger.info(
            "Handle %s -> DISCONNECTED (%s) and state is %s",
            name, reason, self._state,
        )
        self.signalDisconnected.emit(name, "")
        self._audit.log("device_disconnected",
                        {"device": name, "reason": str(reason)})
        # Drop the cached firmware version so System Information no
        # longer reports a stale value for the unplugged device.
        if name in self._firmware_versions and self._firmware_versions[name]:
            self._firmware_versions[name] = ""
            self.firmwareVersionsChanged.emit()
        if self._device_serials.get(name):
            self._device_serials[name] = ""
            self.deviceIdentityChanged.emit()
        if self._firmware_update_available.get(name):
            self._firmware_update_available[name] = False
            self._firmware_latest[name] = ""
            self.firmwareUpdateInfoChanged.emit()
        # Abort an in-flight FPGA flash / sensor-configure pipeline.
        # The SDK does not subscribe to disconnect events for the
        # configure-cameras flow (only start_scan does), so without
        # this hand-off the QML flashTask waits on its 4-min watchdog
        # and the Start button stays stuck on "Stop" through reconnect.
        if self._config_running:
            logger.warning(
                "Aborting in-flight camera configuration: %s disconnected",
                name,
            )
            try:
                self._interface.cancel_configure_camera_sensors()
            except Exception as e:
                logger.debug("cancel_configure_camera_sensors raised: %s", e)
            self._config_running = False
            self.configFinished.emit(
                False, f"Device disconnected during sensor configuration ({name})"
            )
        # User-facing surfacing, gated on the first loss since the
        # device last reached CONNECTED (issue #489) — the SDK monitor
        # keeps retrying an absent device, and each failed retry ends in
        # another CONNECTING -> DISCONNECTED event.
        first_loss = self._device_was_connected.get(name, False)
        self._device_was_connected[name] = False
        self._surface_device_loss(name, first_loss, reason)

    def _log_device_stats(self, name: str) -> None:
        """Best-effort audit of a device's hardware + firmware IDs on
        connect. Missing values are logged as empty strings."""
        handle = getattr(self._interface, name, None)
        hwid = ""
        fw = ""
        try:
            if handle is not None and hasattr(handle, "get_hardware_id"):
                hwid = str(handle.get_hardware_id() or "")
        except Exception:
            logger.debug("device_stats: %s hardware-id lookup failed",
                         name, exc_info=True)
        try:
            if handle is not None and hasattr(handle, "get_version"):
                fw = str(handle.get_version() or "")
        except Exception:
            logger.debug("device_stats: %s firmware-version lookup failed",
                         name, exc_info=True)
        serial = self._refresh_device_identity(name)
        self._audit.log("device_stats", {
            "device": name, "hardware_id": hwid, "firmware_version": fw,
            "serial": serial,
        })
        # Cache for the Settings → System Information card. Runs on the
        # device's bring-up worker; the bound QML rows update via the
        # queued firmwareVersionsChanged emit.
        if name in self._firmware_versions:
            self._firmware_versions[name] = fw
            self.firmwareVersionsChanged.emit()
        self._maybe_check_firmware_update(name)

    def _refresh_device_identity(self, name: str) -> str:
        """Re-read a device's programmed serial number, cache it for
        Settings → About (#529) and return it ("" when unprogrammed or
        unknown). Best-effort like _log_device_stats — identity reads never
        block a connect. Runs on the device's bring-up worker at connect;
        the emit queues to the main thread for QML."""
        handle = getattr(self._interface, name, None)
        serial = ""
        try:
            if handle is not None and hasattr(handle, "read_serial_number"):
                raw = handle.read_serial_number()
                serial = raw.strip() if isinstance(raw, str) else ""
        except Exception:
            logger.debug("device_identity: %s serial lookup failed",
                         name, exc_info=True)
        if name in self._device_serials:
            self._device_serials[name] = serial
            self.deviceIdentityChanged.emit()
        return serial

    def update_state(self):
        """Update system state based on connection and configuration."""
        if not self._consoleConnected and (
            (not self._leftSensorConnected) or (not self._rightSensorConnected)
        ):
            self._state = DISCONNECTED
        elif self._leftSensorConnected and not self._consoleConnected:
            self._state = SENSOR_CONNECTED
        elif self._consoleConnected and not self._leftSensorConnected:
            self._state = CONSOLE_CONNECTED
        elif self._consoleConnected and self._leftSensorConnected:
            self._state = READY
        elif self._consoleConnected and self._leftSensorConnected and self._running:
            self._state = RUNNING
        self.stateChanged.emit()  # Notify QML of state update
        logger.debug(f"Updated state: {self._state}")

    def _any_device_connected(self) -> bool:
        """True when the console OR either sensor is currently connected.

        A lone sensor (no console) still counts as a device, so this gates
        on the SDK's per-device states rather than the DISCONNECTED FSM
        state (which reads DISCONNECTED for a single sensor with no
        console)."""
        return any(self._sdk_connected(n) for n in DEVICES)

    def _surface_device_loss(self, name: str, first_loss: bool,
                             reason="") -> None:
        """User-facing surfacing for a device drop, gated on the first loss
        since the device last reached CONNECTED (issues #387/#489).

        After a real unplug the SDK monitor keeps retrying the absent device
        for a while, and every failed retry ends in another CONNECTING ->
        DISCONNECTED transition. Bench trace 2026-08-27: one power-off
        produced repeat DISCONNECTED events at ~2 s intervals per device
        (four in a row for the console) — each re-fired the disconnect
        toast, visibly recreating the card and resetting its 6 s clock, and
        a card whose original had just expired re-appeared at the bottom of
        the stack. Only the first loss notifies; repeats are logged and
        dropped. On a sensor's first loss, its cameras' per-camera
        'connection lost' cards are dismissed too — the device-level card
        subsumes them (a card lingering from a scan that already ended, or
        fired in the window before the USB drop was detected).
        """
        if not first_loss:
            logger.info(
                "Repeat DISCONNECTED for %s while already down (%s) — "
                "not re-surfacing", name, reason,
            )
            return
        if name in SIDES:
            self._dismiss_dropout_toasts(side=name)
        # A device dropped off USB (issue #387). If it's a device taking
        # part in a running scan, raise the E-304 critical modal and abort;
        # otherwise (idle, or a non-participating device) show a low-key
        # disconnect toast. The SDK's own mid-scan disconnect subscription
        # is already tearing a running scan down; the E-304 modal is the
        # operator-facing notice the app would otherwise omit (a real
        # unplug rarely trips the E-303 stall watchdog).
        self._surface_disconnect(name)

    def _surface_disconnect(self, name: str) -> None:
        """Surface a device dropping off USB (issue #387). If the drop aborts a
        running scan (a participating device lost mid-scan), raise the E-304
        modal; otherwise show a low-key toast (an idle disconnect, or a
        non-participating device). Exactly one notification per event."""
        if _should_abort_on_disconnect(
            name, self._capture_running, self._capture_active_sides
        ):
            self._abort_scan_device_disconnect(name)
        else:
            self.notify(
                _disconnect_toast_text(name),
                type_="warning",
                duration_ms=6000,
                tag=f"disconnect_{name}",
            )

    @pyqtSlot(str)
    def softResetSensor(self, target: str):
        """Reset a device. ``target`` is "console", "left", or "right"."""
        try:
            if target == "console":
                if self._interface.console.soft_reset():
                    logger.info("Software Reset Sent")
                else:
                    logger.error("Failed to send Software Reset")
            elif target in ("left", "right"):
                if getattr(self._interface, target).soft_reset():
                    logger.info("Software Reset Sent")
                else:
                    logger.error("Failed to send Software Reset")
        except Exception as e:
            logger.error(f"Error Sending Software Reset: {e}")

    # Fan control methods
    @pyqtSlot(str, bool, result=bool)
    def setFanControl(self, sensor_side: str, fan_on: bool) -> bool:
        """
        Set fan control for the specified sensor.

        Args:
            sensor_side (str): "left" or "right"
            fan_on (bool): True to turn fan ON, False to turn fan OFF

        Returns:
            bool: True if command was sent successfully, False otherwise
        """
        side = sensor_side.lower()
        try:
            if side == "left":
                if not self._leftSensorConnected:
                    logger.error("Left sensor not connected")
                    return False
                result = self._interface.left.set_fan_control(fan_on)
            elif side == "right":
                if not self._rightSensorConnected:
                    logger.error("Right sensor not connected")
                    return False
                result = self._interface.right.set_fan_control(fan_on)
            else:
                logger.error(f"Invalid sensor side: {sensor_side}")
                return False

            if result:
                logger.info(
                    f"Fan control set to {'ON' if fan_on else 'OFF'} for {sensor_side} sensor"
                )
                # Keep the QML-facing cache truthful (issue #463) — the
                # Engineering switches bind to left/rightSensorFanOn.
                self._last_fan_status[side] = bool(fan_on)
                self.sensorFanChanged.emit()
            else:
                logger.error(f"Failed to set fan control for {sensor_side} sensor")

            return result

        except Exception as e:
            logger.error(f"Error setting fan control: {e}")
            return False

    @pyqtSlot(str, result=bool)
    def getFanControlStatus(self, sensor_side: str) -> bool:
        """
        Get fan control status for the specified sensor.

        Args:
            sensor_side (str): "left" or "right"

        Returns:
            bool: True if fan is ON, False if fan is OFF
        """
        side = sensor_side.lower()
        if side not in ("left", "right"):
            logger.error(f"Invalid sensor side: {sensor_side}")
            return False

        sensor = self._interface.left if side == "left" else self._interface.right
        if not sensor.is_connected():
            # Polled while disconnected/disconnecting — silent, expected.
            return False

        try:
            status = sensor.get_fan_control_status()
            if status != self._last_fan_status.get(side):
                self._last_fan_status[side] = status
                self.sensorFanChanged.emit()
                logger.info(
                    f"Fan status for {sensor_side} sensor: {'ON' if status else 'OFF'}"
                )
            return status
        except Exception as e:
            # Disconnect raced with the in-flight call. Log at DEBUG;
            # the SDK's state machine already logs the disconnect at INFO.
            logger.debug(f"Fan status read on {side} failed during disconnect: {e}")
            return False

    def _device_info_str(self) -> str:
        """Best-effort one-line device identity for bug reports."""
        try:
            console, left, right = self._interface.is_device_connected()
            return f"console={'yes' if console else 'no'} " \
                   f"left={'yes' if left else 'no'} " \
                   f"right={'yes' if right else 'no'}"
        except Exception:
            return "unknown"
