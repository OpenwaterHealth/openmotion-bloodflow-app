"""Device connection state, connect-time bring-up, the startup watchdog, fans."""
import threading
import time

from PyQt6.QtCore import QTimer, pyqtProperty, pyqtSignal, pyqtSlot
from omotion import ConnectionState
from omotion.config import (
    DEBUG_FLAG_CMD_VERBOSE, DEBUG_FLAG_COMM_VERBOSE, DEBUG_FLAG_FAKE_DATA,
    DEBUG_FLAG_HISTO_CMP, DEBUG_FLAG_HISTO_STALL, DEBUG_FLAG_HISTO_THROTTLE,
    DEBUG_FLAG_SEND_DEFER, DEBUG_FLAG_USB_PRINTF,
)

from connector._common import (
    CONSOLE_CONNECTED, DEVICES, DISCONNECTED, READY, SENSOR_CONNECTED, SIDES, logger, spawn)
from hardware.motion_config import select_tec_voltage

# Startup connection watchdog (E-104/E-106). When its deadline finds a
# device still mid-handshake, it checks once more this much later (#667):
# a sensor powered on just before the deadline spends ~2 s in CONNECTING,
# and the SDK monitor connects devices one at a time, so two sensors
# arriving together take ~4.5 s.
_CONNECTION_WATCHDOG_CONNECTING_GRACE_SEC = 10
_CONNECTION_WATCHDOG_TOAST_TAG = "connection-watchdog"

# Sensor debug flags: (connector attribute, app-config key, firmware bit).
_SENSOR_DEBUG_FLAGS = (
    ("_sensor_debug_logging", "sensorDebugLogging", DEBUG_FLAG_USB_PRINTF),
    ("_camera_fake_data", "cameraFakeData", DEBUG_FLAG_FAKE_DATA),
    ("_histo_throttle", "histoThrottle", DEBUG_FLAG_HISTO_THROTTLE),
    ("_comm_verbose", "commVerbose", DEBUG_FLAG_COMM_VERBOSE),
    ("_verbose_command_handling", "verboseCommandHandling", DEBUG_FLAG_CMD_VERBOSE),
    ("_histo_cmp", "histoCmp", DEBUG_FLAG_HISTO_CMP),
    ("_send_data_defer", "deferHistoSend", DEBUG_FLAG_SEND_DEFER),
    ("_histo_stall_test", "debugHistoStallTest", DEBUG_FLAG_HISTO_STALL),
)

# The connector's per-device "connected" flag (plain attributes, read and
# written by the other mixins).
_CONNECTED_ATTR = {
    "console": "_consoleConnected",
    "left": "_leftSensorConnected",
    "right": "_rightSensorConnected",
}
_DEVICE_LABEL = {"console": "Console", "left": "Left sensor", "right": "Right sensor"}


def _should_abort_on_disconnect(name: str, capture_running: bool, active_sides: set) -> bool:
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
    """Toast copy for a device that dropped off USB without aborting a scan
    (idle, or a non-participating device; issue #387)."""
    return f"{_DEVICE_LABEL.get(name, name)} disconnected"


def _prop(type_, notify, attr: str, key: str | None = None):
    """A read-only QML property over ``self.<attr>``, or over
    ``self.<attr>[key]`` (coerced to ``type_``) for the per-device caches."""
    if key is None:
        return pyqtProperty(type_, lambda self: getattr(self, attr), notify=notify)
    return pyqtProperty(type_, lambda self: type_(getattr(self, attr)[key]), notify=notify)


class DevicesMixin:
    """Device connection state, connect-time bring-up, the startup watchdog, fans."""

    signalConnected = pyqtSignal(str, str)  # (descriptor, port)
    signalDisconnected = pyqtSignal(str, str)  # (descriptor, port)
    connectionStatusChanged = pyqtSignal()
    # Notify for ``deviceInitBusy`` (issues #303, #667). The drain-side emit
    # fires on the init worker thread; Qt queues it to main-thread consumers.
    deviceInitBusyChanged = pyqtSignal()
    stateChanged = pyqtSignal()
    consoleFanChanged = pyqtSignal()
    # Sensor-module fan state cache (issue #463), behind the Engineering switches.
    sensorFanChanged = pyqtSignal()
    # Settings → System Information; refreshed on every (dis)connect.
    firmwareVersionsChanged = pyqtSignal()
    # Programmed serial numbers behind Settings → About (#529).
    deviceIdentityChanged = pyqtSignal()

    def _setup_devices(self, cfg: dict) -> None:
        # Connection watchdog (E-104/E-106): one-shot check armed at startup
        # that flags expected devices that never enumerated. 0 disables it.
        self._connection_timeout_sec = float(cfg.get("connectionTimeoutSec", 12))
        self._require_console = bool(cfg.get("requireConsole", True))
        self._min_sensors = int(cfg.get("minSensors", 1))
        # True while the watchdog's E-104/E-106 toast may still be up, so a
        # device that connects afterwards can take it down (#667).
        self._connection_watchdog_toast_up = False

        for attr, key, _bit in _SENSOR_DEBUG_FLAGS:
            setattr(self, attr, bool(cfg.get(key, False)))
        self._power_off_unused_cameras = bool(cfg.get("powerOffUnusedCameras", False))

        console, left, right = self._interface.is_device_connected()
        self._consoleConnected = console
        self._leftSensorConnected = left
        self._rightSensorConnected = right
        # Per-device "reached CONNECTED" latch backing the first-loss edge
        # detection in _on_device_lost (issue #489). The _xxxConnected flags
        # can't serve: the console one is cleared on the intermediate
        # DISCONNECTING event, before the DISCONNECTED event that does the
        # surfacing arrives.
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
        # Sensor fan cache; None = unknown (shown Off).
        self._last_fan_status: dict[str, bool | None] = {"left": None, "right": None}
        # Console fan cache. Seeded True because the bring-up forces
        # set_fan_speed(100) at console connect (_init_console_fan).
        self._console_fan_on: bool = True
        # Firmware versions (e.g. "v1.2.3") and programmed serial numbers,
        # cached on connect by _log_device_stats and cleared on disconnect.
        # "" = unknown / unprogrammed (#529).
        self._firmware_versions: dict[str, str] = {n: "" for n in DEVICES}
        self._device_serials: dict[str, str] = {n: "" for n in DEVICES}

    # --- QML PROPERTIES ----------------------------------------------------
    leftSensorConnected = _prop(bool, connectionStatusChanged, "_leftSensorConnected")
    rightSensorConnected = _prop(bool, connectionStatusChanged, "_rightSensorConnected")
    consoleConnected = _prop(bool, connectionStatusChanged, "_consoleConnected")
    state = _prop(int, stateChanged, "_state")

    # Firmware version cached at connect time ("" when unknown or
    # disconnected). Shown in Settings → System Information.
    consoleFirmwareVersion = _prop(str, firmwareVersionsChanged, "_firmware_versions", "console")
    leftSensorFirmwareVersion = _prop(str, firmwareVersionsChanged, "_firmware_versions", "left")
    rightSensorFirmwareVersion = _prop(str, firmwareVersionsChanged, "_firmware_versions", "right")

    # Programmed serial cached at connect time ("" when unprogrammed or
    # disconnected). Shown in Settings → About (#529).
    consoleSerialNumber = _prop(str, deviceIdentityChanged, "_device_serials", "console")
    leftSensorSerialNumber = _prop(str, deviceIdentityChanged, "_device_serials", "left")
    rightSensorSerialNumber = _prop(str, deviceIdentityChanged, "_device_serials", "right")

    # Cached fan states (True = on) behind the Engineering settings switches.
    # Console: the connect-time 100% default, then any setConsoleFan call.
    # Sensors (issue #463): seeded from the hardware by the connect-time
    # getFanControlStatus poll, updated by setFanControl, and reset to
    # unknown (None, shown Off) at disconnect.
    consoleFanOn = _prop(bool, consoleFanChanged, "_console_fan_on")
    leftSensorFanOn = _prop(bool, sensorFanChanged, "_last_fan_status", "left")
    rightSensorFanOn = _prop(bool, sensorFanChanged, "_last_fan_status", "right")

    @pyqtProperty(bool, notify=deviceInitBusyChanged)
    def deviceInitBusy(self) -> bool:
        """True while any device's connect-time bring-up is scheduled or
        running (issue #303). QML holds Start/Check disabled on this;
        _ensure_idle refuses pipeline starts with it up. Re-engages on every
        (re)connect because _schedule_device_init re-runs then."""
        with self._device_init_lock:
            return any(v > 0 for v in self._device_init_pending.values())

    # --- SENSOR DEBUG FLAGS ------------------------------------------------
    def _live_sensor(self, side: str):
        """The SDK handle for ``side`` when it is connected, else None."""
        sensor = getattr(self._interface, side, None)
        return sensor if sensor is not None and sensor.is_connected() else None

    def _compute_sensor_debug_flags(self) -> int:
        """Compute sensor debug flag bitfield from current config booleans."""
        flags = 0
        for attr, _key, bit in _SENSOR_DEBUG_FLAGS:
            if getattr(self, attr):
                flags |= bit
        return flags

    def _apply_sensor_debug_flags(self) -> None:
        """Re-push the debug-flag bitmask to every connected sensor, for the
        live Settings → Engineering toggles (``setSensorDebugFlag``).

        Unlike ``_init_sensor``, this writes even when ``flags == 0`` so
        that turning the last flag off actually clears it on the firmware.
        """
        flags = self._compute_sensor_debug_flags()
        for side in SIDES:
            sensor = self._live_sensor(side)
            if sensor is None:
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
        edge.
        """
        pending = self._device_init_pending
        with self._device_init_lock:
            before = any(v > 0 for v in pending.values())
            pending[name] = max(0, pending.get(name, 0) + delta)
            after = any(v > 0 for v in pending.values())
        if before != after:
            self.deviceInitBusyChanged.emit()

    def _schedule_device_init(self, name: str) -> None:
        """Run ``name``'s connect-time bring-up on a worker thread.

        The busy gate goes up now (issue #303): a Start clicked before or
        during the bring-up collides with it. For the console that means a
        scan before the laser registers are loaded; for a sensor, a camera
        wedged until a DUT power-cycle. Sensors wait 1 s first for USB to
        settle."""
        gen = self._device_connect_gen[name]
        self._device_init_note(name, +1)
        QTimer.singleShot(0 if name == "console" else 1000,
                          lambda: self._start_device_init_worker(name, gen))

    def _start_device_init_worker(self, name: str, gen: int) -> None:
        """Spawn the bring-up worker. Worker-safety: the SDK serializes
        per-device command I/O (MotionUart._io_lock for the console,
        CommInterface._io_lock/_send_lock for sensors), every signal
        emitted from the sequence queues to the main thread, _raise_critical
        is worker-safe by contract and the audit log takes its own lock."""
        spawn(self._run_device_init, name, gen, name=f"{name}-init")

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
                logger.info("Console init stopped before %s: the console disconnected", label)
                return
            try:
                step()
            except Exception as e:
                logger.warning("Console init: %s failed: %s", label, e)

    def _init_console_tec_voltage(self) -> None:
        # EVT2 consoles (board-ID strap 1) need +1.16 V on the TEC DAC to
        # hold the lasers at 25 C; DVT and beyond use TEC_VOLTAGE_DEFAULT
        # from config/tec_params.py (issue #269).
        voltage, why = select_tec_voltage(self._interface.console, self._tec_voltage_params)
        if self._interface.console.tec_voltage(voltage):
            logger.info(f"Console TEC voltage set to {voltage}V ({why})")
        else:
            logger.error(f"Failed to set console TEC voltage to {voltage}V ({why})")

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
            self._raise_critical("E-103", detail="laser power params not applied")
        else:
            logger.warning("Laser power params not applied: the console disconnected")

    def _init_sensor(self, side: str, gen: int) -> None:
        """Sensor bring-up: debug flags, fan state, I2C health, camera ID
        cache, identity log."""
        if not self._init_is_current(side, gen):
            return

        # Only written when a flag is set. USB has settled by now (the 1 s
        # delay in _schedule_device_init).
        flags = self._compute_sensor_debug_flags()
        sensor = self._live_sensor(side) if flags else None
        if sensor is not None:
            logger.info("Setting debug flags 0x%x on %s sensor (%s)", flags, side, ", ".join(
                key for attr, key, _bit in _SENSOR_DEBUG_FLAGS if getattr(self, attr)))
            if not sensor.set_debug_flags(flags):
                logger.warning("Failed to set debug flags on %s sensor", side)
        elif flags:
            logger.info("Skipping debug flag set on %s sensor (flags=0x%x): not connected",
                        side, flags)

        self.getFanControlStatus(side)
        self._check_sensor_i2c_health(side)

        # Power on all cameras, fill the ID cache (serial numbers,
        # connection info), then optionally power them off again.
        try:
            sensor = self._live_sensor(side)
            if sensor is None:
                pass
            elif sensor.enable_camera_power(0xFF):
                time.sleep(0.5)  # settle time
                sensor.refresh_id_cache()
                if self._power_off_unused_cameras:
                    sensor.disable_camera_power(0xFF)
                    time.sleep(0.05)
            else:
                logger.warning("Could not power on cameras on %s sensor for ID cache fill", side)
                self._raise_critical("E-105", detail=f"{side} sensor")
                sensor.refresh_id_cache()  # try anyway in case some cameras are already on
        except Exception as e:
            logger.debug("Could not refresh sensor ID cache for %s: %s", side, e)

        # Hardware identity (firmware, hw_id, serial, all 8 camera UIDs),
        # once per connect and after the cache fill; mirrors
        # log_console_info on console connect.
        self._interface.log_sensor_info(side)
        self._log_device_stats(side)
        self.connectionStatusChanged.emit()

    def _check_sensor_i2c_health(self, side: str) -> None:
        """Raise E-101/E-102 if a sensor's boot-time I2C self-check is bad.

        Reads the firmware's snapshot cached by the SDK at connect
        (``MotionSensor.i2c_health``; no disruptive rescan). Best-effort:
        never raises out, so it cannot block the bring-up.
        """
        try:
            sensor = self._live_sensor(side)
            if sensor is None:
                return
            health = sensor.i2c_health
            if health is None:
                self._raise_critical("E-102", detail=f"{side} sensor")
            elif not health.get("all_present", False):
                missing = self._i2c_missing_devices(health)
                self._raise_critical("E-101", detail=f"{side} sensor: {missing}")
        except Exception:
            logger.exception("I2C health check failed for %s sensor", side)

    @staticmethod
    def _i2c_missing_devices(health: dict) -> str:
        """Human-readable summary of which I2C devices didn't respond."""
        parts = [dev for dev in ("mux", "imu") if not health.get(dev, True)]
        for group in ("cameras", "fpgas"):
            bad = [str(i) for i, ok in enumerate(health.get(group, [])) if not ok]
            if bad:
                parts.append(f"{group} " + ",".join(bad))
        return "; ".join(parts) or "unknown device"

    # --- STARTUP CONNECTION WATCHDOG ---------------------------------------
    def _arm_connection_watchdog(self) -> None:
        """Schedule the one-shot startup connection check (E-104/E-106)."""
        if self._connection_timeout_sec > 0:
            QTimer.singleShot(int(self._connection_timeout_sec * 1000),
                              self._check_connection_watchdog)

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
        return self._sdk_state(name) == ConnectionState.CONNECTED

    def _any_device_connected(self) -> bool:
        """True when the console OR either sensor is currently connected.

        A lone sensor (no console) still counts, so this reads the SDK's
        per-device states rather than the FSM state (which is DISCONNECTED
        for a single sensor with no console)."""
        return any(self._sdk_connected(n) for n in DEVICES)

    def _connection_shortfall(self) -> tuple[bool, int, bool]:
        """``(console_missing, n_sensors, sensors_missing)`` from the SDK's
        live states, against ``requireConsole`` / ``minSensors``."""
        console_missing = self._require_console and not self._sdk_connected("console")
        n_sensors = sum(self._sdk_connected(side) for side in SIDES)
        return console_missing, n_sensors, n_sensors < self._min_sensors

    def _check_connection_watchdog(self, final: bool = False) -> None:
        """Warn about devices that never connected within the startup timeout.

        One-shot, from the SDK's live device states. A missing console
        (E-104) and/or too few sensors (E-106) are non-blocking: one yellow
        warning toast, not the critical modal, since the fix is usually just
        "plug it in". Disconnects after startup are handled by the
        connection-status UI.

        A device still CONNECTING at the deadline is not missing yet (#667):
        QA powered the system on after launching the app, and E-106 fired
        while the sensor was mid-handshake. The check then runs once more
        (``final``) after ``_CONNECTION_WATCHDOG_CONNECTING_GRACE_SEC`` and
        reports whatever is still not connected, connecting or not, so a
        device that keeps failing its handshake is still reported.
        """
        try:
            console_missing, n_sensors, sensors_missing = self._connection_shortfall()
            if not console_missing and not sensors_missing:
                return
            if not final:
                expected = ((["console"] if console_missing else [])
                            + (list(SIDES) if sensors_missing else []))
                connecting = [n for n in expected
                              if self._sdk_state(n) == ConnectionState.CONNECTING]
                if connecting:
                    logger.info(
                        "Connection watchdog: %s still connecting at the deadline — "
                        "checking again in %d s",
                        ", ".join(connecting), _CONNECTION_WATCHDOG_CONNECTING_GRACE_SEC)
                    QTimer.singleShot(_CONNECTION_WATCHDOG_CONNECTING_GRACE_SEC * 1000,
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
                msg = "Console not detected. Check the console USB cable and power, then reconnect."
            else:
                logger.warning("Connection watchdog: sensor not detected (E-106: %d of %d)",
                               n_sensors, self._min_sensors)
                msg = "Sensor not detected. Check the sensor USB cable and power, then reconnect."
            # Auto-dismisses after 10 s so it doesn't nag while the user
            # explores the no-device sample scan (#314), and comes down by
            # itself once the missing devices connect
            # (_withdraw_connection_watchdog_toast).
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
        console_missing, _n_sensors, sensors_missing = self._connection_shortfall()
        if console_missing or sensors_missing:
            return
        self._connection_watchdog_toast_up = False
        logger.info("Connection watchdog: the devices it warned about have connected — "
                    "withdrawing its warning")
        self.dismissNotification(_CONNECTION_WATCHDOG_TOAST_TAG)

    # --- CONNECTION STATE CHANGES ------------------------------------------
    def _on_handle_state_changed(self, handle, old, new, reason):
        """Single state-change handler wired to console/left/right.

        The SDK emits from its connection-monitor thread and Qt queues the
        call onto the GUI thread. An exception escaping here would terminate
        the process at the Qt boundary, so log and swallow: the worst case
        is a stale UI state until the next connection event.
        """
        try:
            self._on_handle_state_changed_impl(handle, old, new, reason)
        except Exception:
            logger.exception(
                "_on_handle_state_changed crashed for handle=%s "
                "old=%s new=%s reason=%s — swallowing to keep app alive",
                getattr(handle, "name", "?"), old, new, reason)

    def _on_handle_state_changed_impl(self, handle, old, new, reason):
        is_now_connected = new == ConnectionState.CONNECTED
        is_now_lost = new == ConnectionState.DISCONNECTED
        name = handle.name

        # A device we are deliberately flashing drops into DFU and
        # re-enumerates as a different USB device. Suppress that expected
        # disconnect entirely so the connected flags and _state stay
        # consistent until the post-flash power-cycle reconnect.
        if is_now_lost and self._firmware_update_in_progress == name:
            logger.info("Ignoring expected DFU disconnect for %s during firmware update", name)
            return

        # The console flag also drops on the intermediate DISCONNECTING.
        if name == "console":
            self._consoleConnected = is_now_connected
        if is_now_connected:
            self._on_device_connected(name, reason)
        elif is_now_lost:
            self._on_device_lost(name, reason)
        # Not for CONNECTING / DISCONNECTING: QML observers would re-poll
        # device state on every intermediate transition of a disconnect cycle.
        if is_now_connected or is_now_lost:
            self.connectionStatusChanged.emit()
            self.update_state()

    def _on_device_connected(self, name: str, reason) -> None:
        """Bookkeeping for a device that reached CONNECTED. No device I/O
        here: this runs on the GUI thread, so the bring-up goes to a worker
        (_schedule_device_init)."""
        self._device_connect_gen[name] += 1
        setattr(self, _CONNECTED_ATTR[name], True)
        if name == "console":
            # Start of the safety-status grace period (readSafetyStatus).
            self._console_connected_at = time.monotonic()
        logger.info("Handle %s -> CONNECTED (%s)", name, reason)
        # Before anything reacts to the connect: this raises the busy gate.
        self._schedule_device_init(name)
        # Re-arm the first-loss edge (issue #489): the next drop is a real
        # one worth surfacing again.
        self._device_was_connected[name] = True
        # A '<device> disconnected' card must not outlive the reconnect
        # (issue #489): during a console power-cycle the cards kept saying
        # 'disconnected' after the devices were already back.
        self.dismissNotification(f"disconnect_{name}")
        self._withdraw_connection_watchdog_toast()
        self.signalConnected.emit(name, "")
        self._audit.log("device_connected", {"device": name, "reason": str(reason)})

    def _on_device_lost(self, name: str, reason) -> None:
        """Bookkeeping and user-facing surfacing for a device that reached
        DISCONNECTED."""
        setattr(self, _CONNECTED_ATTR[name], False)
        if name == "console":
            self._console_connected_at = None
            # A dead console can't be driving the laser trigger, and it
            # can't deliver the SDK's trigger-OFF event either (its
            # stop_trigger raises during teardown). Close the trigger clock
            # now so the notes duration line and the header counter stop
            # here instead of counting several more seconds of scan
            # teardown (issue #201).
            if self._trigger_state == "ON" or self._trigger_on_mono is not None:
                logger.warning(
                    "Console lost while trigger ON — closing the trigger "
                    "clock at disconnect detection (%s)", reason)
                self._trigger_clock_close()
                self._trigger_state = "OFF"
                self.triggerStateChanged.emit()
        else:
            self._last_fan_status[name] = None
            self.sensorFanChanged.emit()
            try:
                getattr(self._interface, name).clear_id_cache()
            except Exception:
                logger.debug("clear_id_cache failed for %s", name, exc_info=True)

        logger.info("Handle %s -> DISCONNECTED (%s) and state is %s", name, reason, self._state)
        self.signalDisconnected.emit(name, "")
        self._audit.log("device_disconnected", {"device": name, "reason": str(reason)})
        # Drop the cached identity so Settings no longer reports stale
        # values for the unplugged device.
        if self._firmware_versions.get(name):
            self._firmware_versions[name] = ""
            self.firmwareVersionsChanged.emit()
        if self._device_serials.get(name):
            self._device_serials[name] = ""
            self.deviceIdentityChanged.emit()
        if self._firmware_update_available.get(name):
            self._firmware_update_available[name] = False
            self.firmwareUpdateInfoChanged.emit()
        # Abort an in-flight FPGA flash / sensor-configure pipeline. The SDK
        # does not subscribe to disconnect events for the configure-cameras
        # flow (only start_scan does), so without this the QML flashTask
        # waits on its 4-min watchdog and the Start button stays stuck on
        # "Stop" through reconnect.
        if self._config_running:
            logger.warning("Aborting in-flight camera configuration: %s disconnected", name)
            try:
                self._interface.cancel_configure_camera_sensors()
            except Exception as e:
                logger.debug("cancel_configure_camera_sensors raised: %s", e)
            self._config_running = False
            self.configFinished.emit(
                False, f"Device disconnected during sensor configuration ({name})")
        first_loss = self._device_was_connected.get(name, False)
        self._device_was_connected[name] = False
        self._surface_device_loss(name, first_loss, reason)

    def update_state(self):
        """Update system state based on connection and configuration."""
        if self._consoleConnected:
            self._state = READY if self._leftSensorConnected else CONSOLE_CONNECTED
        elif self._leftSensorConnected and self._rightSensorConnected:
            self._state = SENSOR_CONNECTED
        else:
            self._state = DISCONNECTED
        self.stateChanged.emit()
        logger.debug(f"Updated state: {self._state}")

    def _surface_device_loss(self, name: str, first_loss: bool, reason="") -> None:
        """User-facing surfacing for a device drop, gated on the first loss
        since the device last reached CONNECTED (issues #387/#489).

        After a real unplug the SDK monitor keeps retrying the absent
        device, and every failed retry ends in another CONNECTING ->
        DISCONNECTED transition. Bench trace 2026-08-27: one power-off gave
        repeat DISCONNECTED events at ~2 s intervals per device (four in a
        row for the console), each re-firing the disconnect toast and
        resetting its 6 s clock. Only the first loss notifies; repeats are
        logged and dropped. On a sensor's first loss, its cameras'
        'connection lost' cards are dismissed too: the device-level card
        subsumes them.
        """
        if not first_loss:
            logger.info("Repeat DISCONNECTED for %s while already down (%s) — "
                        "not re-surfacing", name, reason)
            return
        if name in SIDES:
            self._dismiss_dropout_toasts(side=name)
        self._surface_disconnect(name)

    def _surface_disconnect(self, name: str) -> None:
        """Surface a device dropping off USB (issue #387): the E-304 modal
        and a scan abort when a participating device is lost mid-scan,
        otherwise a low-key toast. Exactly one notification per event.

        The SDK's own mid-scan disconnect subscription is already tearing a
        running scan down; E-304 is the operator-facing notice the app would
        otherwise omit (a real unplug rarely trips the E-303 stall watchdog).
        """
        if _should_abort_on_disconnect(name, self._capture_running, self._capture_active_sides):
            self._abort_scan_device_disconnect(name)
        else:
            self.notify(_disconnect_toast_text(name), type_="warning", duration_ms=6000,
                        tag=f"disconnect_{name}")

    # --- DEVICE IDENTITY ---------------------------------------------------
    def _device_read(self, name: str, method: str, what: str):
        """Best-effort ``<device>.<method>()``; None when the read fails.
        Identity reads never block a connect."""
        try:
            return getattr(getattr(self._interface, name), method)()
        except Exception:
            logger.debug("%s %s lookup failed", name, what, exc_info=True)
            return None

    def _log_device_stats(self, name: str) -> None:
        """Audit a device's hardware + firmware IDs on connect and cache its
        firmware version for Settings → System Information. Missing values
        are logged as empty strings. Runs on the device's bring-up worker;
        the emit queues to the main thread for QML."""
        hwid = str(self._device_read(name, "get_hardware_id", "hardware-id") or "")
        fw = str(self._device_read(name, "get_version", "firmware-version") or "")
        serial = self._refresh_device_identity(name)
        self._audit.log("device_stats", {
            "device": name, "hardware_id": hwid, "firmware_version": fw, "serial": serial})
        self._firmware_versions[name] = fw
        self.firmwareVersionsChanged.emit()
        self._maybe_check_firmware_update(name)

    def _refresh_device_identity(self, name: str) -> str:
        """Re-read a device's programmed serial number, cache it for
        Settings → About (#529) and return it ("" when unprogrammed or
        unknown). Runs on the device's bring-up worker at connect; the emit
        queues to the main thread for QML."""
        raw = self._device_read(name, "read_serial_number", "serial")
        serial = raw.strip() if isinstance(raw, str) else ""
        self._device_serials[name] = serial
        self.deviceIdentityChanged.emit()
        return serial

    def _device_info_str(self) -> str:
        """Best-effort one-line device identity for bug reports."""
        try:
            console, left, right = self._interface.is_device_connected()
            return (f"console={'yes' if console else 'no'} left={'yes' if left else 'no'} "
                    f"right={'yes' if right else 'no'}")
        except Exception:
            return "unknown"

    # --- SLOTS: RESET AND FANS ---------------------------------------------
    @pyqtSlot(str)
    def softResetSensor(self, target: str):
        """Reset a device. ``target`` is "console", "left", or "right"."""
        try:
            if target not in DEVICES:
                return
            if getattr(self._interface, target).soft_reset():
                logger.info("Software Reset Sent")
            else:
                logger.error("Failed to send Software Reset")
        except Exception as e:
            logger.error(f"Error Sending Software Reset: {e}")

    @pyqtSlot(bool, result=bool)
    def setConsoleFan(self, on: bool) -> bool:
        """Drive the console fan to 100% (on) or 0% (off); True on success.

        Updates the cached state and notifies QML. Never raises: a
        mid-flight disconnect must not escape the Qt slot.
        """
        if not self._consoleConnected:
            logger.error("Console not connected — cannot set console fan")
            return False
        try:
            # set_fan_speed returns the duty cycle that was set (0..100) or
            # -1 on OW_ERROR. Compare with -1, NOT truthiness: turning the
            # fan off returns 0, which is falsy but successful.
            if self._interface.console.set_fan_speed(fan_speed=100 if on else 0) == -1:
                logger.error("Failed to set console fan")
                return False
            self._console_fan_on = bool(on)
            self.consoleFanChanged.emit()
            logger.info("Console fan set to %s", "ON" if on else "OFF")
            return True
        except Exception as e:  # noqa: BLE001 — slot must not raise
            logger.error("Error setting console fan: %s", e)
            return False

    @pyqtSlot(str, bool, result=bool)
    def setFanControl(self, sensor_side: str, fan_on: bool) -> bool:
        """Turn the "left" / "right" sensor-module fan on or off; True when
        the command was sent successfully."""
        side = sensor_side.lower()
        try:
            if side not in SIDES:
                logger.error(f"Invalid sensor side: {sensor_side}")
                return False
            if not getattr(self, _CONNECTED_ATTR[side]):
                logger.error(f"{side.capitalize()} sensor not connected")
                return False
            result = getattr(self._interface, side).set_fan_control(fan_on)
            if result:
                logger.info(
                    f"Fan control set to {'ON' if fan_on else 'OFF'} for {sensor_side} sensor")
                # Keep the QML-facing cache truthful (issue #463).
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
        """Read the "left" / "right" sensor-module fan state (True = on) and
        refresh the QML-facing cache."""
        side = sensor_side.lower()
        if side not in SIDES:
            logger.error(f"Invalid sensor side: {sensor_side}")
            return False
        sensor = getattr(self._interface, side)
        if not sensor.is_connected():
            # Polled while disconnected/disconnecting — silent, expected.
            return False
        try:
            status = sensor.get_fan_control_status()
            if status != self._last_fan_status.get(side):
                self._last_fan_status[side] = status
                self.sensorFanChanged.emit()
                logger.info(f"Fan status for {sensor_side} sensor: {'ON' if status else 'OFF'}")
            return status
        except Exception as e:
            # Disconnect raced with the in-flight call; the SDK's state
            # machine already logs the disconnect at INFO.
            logger.debug(f"Fan status read on {side} failed during disconnect: {e}")
            return False
