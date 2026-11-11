"""Console telemetry, laser safety, trigger and the error LED."""
import json
import time

from PyQt6.QtCore import QTimer, QRecursiveMutex, pyqtProperty, pyqtSignal, pyqtSlot
from connector._common import logger
from hardware.motion_config import (
    TEC_TRIP_MAX_C, TEC_TRIP_MIN_C, TecTripOutcome, ensure_tec_trip, tec_voltage_params,
)

# Console front-panel RGB LED states: wire values of the firmware's
# OW_CTRL_SET_IND command (console-fw led_driver.h: LED_NONE=0, LED_RED=1,
# LED_GREEN=2, LED_BLUE=3). The firmware shows solid green when idle and
# solid blue while the trigger/laser is active; its hard-fault
# Error_Handler blinks blue at 500 ms, which the app-side error blink mirrors.
_RGB_LED_OFF = 0
_RGB_LED_BLUE = 3
_RGB_LED_GREEN = 2
_ERROR_LED_BLINK_MS = 500  # on/off half-period, matches firmware Error_Handler

# Issue #119: a single ``safety_known=False`` poll can be a transient I2C
# miss during a USB disconnect cascade rather than a real safety-chip
# fault. Require a streak before firing the persistent toast. Telemetry
# polls at ~1 Hz, so 3 polls is about 3 s: well under the time a user takes
# to reach Check, preserving the #107 contract (latched chip fault must
# surface before the laser can be fired).
SAFETY_UNKNOWN_STREAK_THRESHOLD = 3


def _safety_unknown_streak_decision(snap, prev_streak, threshold=SAFETY_UNKNOWN_STREAK_THRESHOLD):
    """Update the safety-unknown streak; returns ``(new_streak, should_fire)``.

    ``safety_known=False`` (chip unresponsive on this poll) increments the
    streak and fires once it reaches the threshold; the caller's
    ``if not self._safetyFailure`` guard stops re-fires on later polls.
    ``safety_known=True`` (or missing, on legacy snapshots) resets it.
    """
    if not getattr(snap, "safety_known", True):
        new_streak = prev_streak + 1
        return new_streak, new_streak >= threshold
    return 0, False


# The two steps every laser-safety trip shares. Module functions (not
# methods) so readSafetyStatus stays callable on the bare fakes the tests use.
def _latch_safety_failure(conn, fault_detail: str) -> None:
    """Log the trip, latch ``safetyFailure`` and fire the persistent toast."""
    logger.error(f"Laser safety failure: {fault_detail}")
    conn.safetyFailure = True
    conn._fire_safety_notification(fault_detail)


def _shut_laser_off(conn) -> None:
    """Stop the trigger and tell QML the laser is off.

    laserStateChanged is the zero-arg notify signal of ``laserOn``: set the
    state and emit() without args (passing ``False`` raises in PyQt).
    """
    conn.stopTrigger()
    conn._laserOn = False
    conn.laserStateChanged.emit()


class ConsoleMixin:
    """Console telemetry, laser safety, trigger and the error LED."""

    def _setup_console(self, cfg: dict) -> None:
        self._force_laser_fail = bool(cfg.get("forceLaserFail", False))
        # Console USB-printf mirror (DEBUG_FLAG_USB_PRINTF), applied to the
        # console, not the sensors. See setConsoleDebugLogging.
        self._console_debug_logging = bool(cfg.get("consoleDebugLogging", False))
        self._laserOn = False
        self._safetyFailure = False
        self._safety_unknown_streak = 0  # see SAFETY_UNKNOWN_STREAK_THRESHOLD
        self._trigger_state = "OFF"
        # Console connect time, for the safety grace period (issue #107 follow-up).
        self._console_connected_at: float | None = None
        self._tec_voltage_params = tec_voltage_params()
        self._console_mutex = QRecursiveMutex()
        # Console error-LED blink (issue #257): while the critical-error
        # modal is up, blink the console front-panel LED blue instead of
        # leaving it solid green. Started via _errorLedBlinkRequested
        # (queued from worker threads); stopped and restored to idle green
        # by criticalErrorsDismissed() when the modal queue empties.
        self._error_led_timer = QTimer(self)
        self._error_led_timer.setInterval(_ERROR_LED_BLINK_MS)
        self._error_led_timer.timeout.connect(self._on_error_led_tick)
        self._error_led_on = False  # True while the blue half-period is lit

    laserStateChanged = pyqtSignal()
    safetyFailureStateChanged = pyqtSignal()
    # Safety tripped while a scan runs; the main-thread slot shows the
    # message and schedules the cancel.
    safetyTripDuringCaptureRequested = pyqtSignal()
    triggerStateChanged = pyqtSignal()
    # Private worker→main marshalling: _raise_critical may fire from USB I/O
    # or scanner worker threads, but the console error-LED blink QTimer lives
    # on the GUI thread that owns it (issue #257). Wired in connect_signals.
    _errorLedBlinkRequested = pyqtSignal()

    @pyqtProperty(bool, notify=laserStateChanged)
    def laserOn(self):
        """Expose the laser-on state to QML."""
        return self._laserOn

    @pyqtProperty(bool, notify=safetyFailureStateChanged)
    def safetyFailure(self):
        """Expose the laser-safety-failure state to QML."""
        return self._safetyFailure

    @safetyFailure.setter
    def safetyFailure(self, value: bool):
        if self._safetyFailure != value:
            self._safetyFailure = value
            self.safetyFailureStateChanged.emit()
            # Recovery clears the persistent toast; it is *fired* by
            # readSafetyStatus, where the decoded fault detail is available.
            if not value:
                self.dismissNotification("laser_safety")

    def _fire_safety_notification(self, fault_detail: str = ""):
        """Fire the persistent laser-safety toast.

        ``fault_detail`` is appended to the toast text only when
        ``appConfig.engineeringMode`` is enabled, so end users see a
        friendly message and engineers see which fault bits tripped.
        Tagged ``laser_safety`` so re-fires replace rather than stack.
        """
        msg = (
            "Laser safety warning detected. Please restart your "
            "console. If this error persists, please contact support."
        )
        if fault_detail and self._app_config.get("engineeringMode", False):
            msg += f"\n[dev] {fault_detail}"
        self.notify(msg, type_="error", duration_ms=0, dismissible=False, tag="laser_safety")

    @pyqtProperty(str, notify=triggerStateChanged)
    def triggerState(self):
        """Expose the trigger state ("ON" / "OFF") to QML."""
        return self._trigger_state

    def _on_telemetry_update(self, snap) -> None:
        """SDK telemetry-poll callback (~1 Hz): feed the laser-safety check."""
        if not snap.read_ok:
            logger.warning("Telemetry poll error: %s", snap.error)
            return
        try:
            self.readSafetyStatus(snap)
        except Exception as exc:
            logger.error("_on_telemetry_update safety error: %s", exc)

    def _on_safety_trip_during_capture(self):
        """Main thread: safety tripped mid-scan; show E-202 and cancel the scan in 5 s."""
        if not self._capture_running or self._safety_cancel_scheduled:
            return
        self._safety_cancel_scheduled = True
        self.captureLog.emit("Laser safety system tripped. Scan will be cancelled in 5 seconds.")
        self._note_scan_abort(
            "E-202",
            f"laser safety system tripped mid-scan (scan elapsed {self._scan_elapsed_str()})",
        )
        self._raise_critical("E-202")
        QTimer.singleShot(5000, self.stopCapture)

    def _surface_safety_trip(self, fault_detail: str) -> None:
        """Raise the blocking modal for a laser-safety FPGA trip (#431).

        Called once per trip, on the transition into ``safetyFailure``. A
        trip during a scan goes through ``_on_safety_trip_during_capture``
        (E-202, which also cancels the scan); a trip at any other time —
        idle, preflight signal-quality check, test/calibrate — raises E-203.
        Before #431 that second case showed only the persistent toast, which
        an operator can miss.
        """
        if self._capture_running:
            if not self._safety_cancel_scheduled:
                self.safetyTripDuringCaptureRequested.emit()
        else:
            self._raise_critical("E-203", detail=fault_detail)

    @pyqtSlot()
    def readSafetyStatus(self, snap=None):
        """Check one telemetry snapshot for a laser-safety trip and surface it.

        A trip latches ``safetyFailure`` (so it surfaces once), fires the
        persistent toast, stops the trigger and raises a blocking modal:
        E-201 when the interlock chip is unresponsive, else E-202 / E-203
        via ``_surface_safety_trip``.
        """
        if snap is None:
            snap = self._interface.console.telemetry.get_snapshot()
        if snap is None:
            logger.warning("readSafetyStatus: no telemetry snapshot yet")
            return
        try:
            self._safety_unknown_streak, should_fire_unknown = (
                _safety_unknown_streak_decision(snap, self._safety_unknown_streak)
            )
            # Chip didn't respond on this poll: ``safety_ok`` is then the
            # dataclass default ``True``, not a verified clear (#107). A
            # snapshot without ``safety_known`` is trusted, as before.
            if not getattr(snap, "safety_known", True):
                # Grace period: for 5 s after console connect the chip may
                # not answer yet while hardware initialises (rapid power
                # cycles); suppress every trip in that window.
                if self._console_connected_at is not None:
                    time_since_connect = time.monotonic() - self._console_connected_at
                    if time_since_connect < 5.0:
                        logger.debug(
                            f"readSafetyStatus: safety chip unresponsive "
                            f"{time_since_connect:.1f}s after connect (within grace period)"
                        )
                        return
                # #119: one missed I2C read can be a transient of a USB
                # disconnect cascade; wait for the streak to confirm.
                if not should_fire_unknown or self._safetyFailure:
                    return
                fault_detail = (
                    "safety interlock chip unresponsive — cannot verify laser safety state")
                _latch_safety_failure(self, fault_detail)
                self._raise_critical("E-201", detail=fault_detail)
                _shut_laser_off(self)
                if self._capture_running and not self._safety_cancel_scheduled:
                    self.safetyTripDuringCaptureRequested.emit()
            elif snap.safety_ok:
                if self._safetyFailure:
                    self.safetyFailure = False
            elif not self._safetyFailure:
                # Name the tripped bits for the engineering toast and the
                # log; the SDK owns the bit→label mapping.
                from omotion.ConsoleTelemetry import _decode_safety_faults
                se_faults = _decode_safety_faults(snap.safety_se)
                so_faults = _decode_safety_faults(snap.safety_so)
                fault_detail = (
                    f"safety_se=0x{snap.safety_se:02X} "
                    f"({', '.join(se_faults) or 'no faults'}); "
                    f"safety_so=0x{snap.safety_so:02X} "
                    f"({', '.join(so_faults) or 'no faults'})"
                )
                _latch_safety_failure(self, fault_detail)
                _shut_laser_off(self)
                self._surface_safety_trip(fault_detail)
        except Exception as e:
            logger.error(f"readSafetyStatus failed: {e}")
            self.safetyFailure = True
            self._fire_safety_notification(f"telemetry exception: {e}")
            if self._capture_running and not self._safety_cancel_scheduled:
                self.safetyTripDuringCaptureRequested.emit()

    @pyqtSlot(str, result=bool)
    def setTrigger(self, triggerjson):
        """Push a trigger config (JSON; only the fields to change) to the console."""
        try:
            # Absent fields fall through to the interface's resolved default
            # (which already carries the dark-frame skip, #449); then the
            # engineering alternative laser pulse width (#446 family).
            data = self._apply_alt_laser_pulse_width(
                self._interface.resolve_trigger_config(json.loads(triggerjson)))
            trigger_setting = self._interface.console.set_trigger_json(data=data)
            if trigger_setting:
                logger.info(f"Trigger Setting: {trigger_setting}")
                return True
            logger.error("Failed to set trigger setting.")
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON data: {e}")
        except AttributeError as e:
            logger.error(f"Invalid interface or method: {e}")
        except Exception as e:
            logger.error(f"Unexpected error while setting trigger: {e}")
        return False

    @pyqtSlot(result=bool)
    def startTrigger(self):
        """Start the console trigger; True on success."""
        success = self._interface.console.start_trigger()
        if success:
            self._trigger_state = "ON"
            self.triggerStateChanged.emit()
            logger.info("Trigger started successfully.")
        return success

    @pyqtSlot()
    def stopTrigger(self):
        """Stop the console trigger."""
        self._interface.console.stop_trigger()
        self._trigger_state = "OFF"
        self.triggerStateChanged.emit()
        logger.info("Trigger stopped.")

    def set_laser_power_from_config(self, interface):
        """Write the SDK's laser-power config (omotion.laser: FPGA register
        map + laser params over I2C). forceLaserFail loads the safety-trip
        param set instead, to exercise the interlock."""
        return interface.apply_laser_power(force_fault=self._force_laser_fail)

    def apply_tec_trip_from_config(self, interface) -> TecTripOutcome:
        """Push the configured TEC over-temp trip (tecTripTempC, °C) into the
        console user config. Read-modify-write touching only the TEC_TRIP key
        (calibration + OPT_*/EE_* preserved), serialized under _console_mutex.
        Today this connect-time push is the only holder of that mutex (so it is
        not yet load-bearing); a future concurrent console-config writer must
        take the same mutex for the read+write pair to stay atomic.

        Non-fatal: a bad config value or a device read/write failure is logged
        at ERROR and the device keeps its existing trip — never blocks scanning
        and never writes 0 (which would disable the firmware over-temp trip)."""
        temp_c = self._app_config.get("tecTripTempC", 40)
        self._console_mutex.lock()
        try:
            outcome = ensure_tec_trip(interface.console, temp_c)
        finally:
            self._console_mutex.unlock()

        if outcome is TecTripOutcome.WROTE:
            logger.info(f"Console TEC_TRIP set to {temp_c} °C")
        elif outcome is TecTripOutcome.UNCHANGED:
            logger.info(f"Console TEC_TRIP already {temp_c} °C, skipping write")
        elif outcome is TecTripOutcome.SKIPPED_INVALID:
            logger.error(
                f"Invalid tecTripTempC={temp_c!r}; leaving console over-temp "
                f"trip untouched (expected a number in {TEC_TRIP_MIN_C}-{TEC_TRIP_MAX_C} °C)"
            )
        else:  # TecTripOutcome.FAILED
            logger.error(f"Failed to apply TEC_TRIP={temp_c} °C to console")
        return outcome

    # --- Console error-LED blink (issue #257) -----------------------------
    # While the critical-error modal is showing, the console front-panel LED
    # blinks blue (mirroring the firmware Error_Handler's 500 ms blink)
    # instead of staying solid green. There is no firmware "blink" command —
    # OW_CTRL_SET_IND only sets solid states — so the app toggles blue/off on
    # a GUI-thread QTimer and restores idle green on dismissal.

    def _start_error_led_blink(self) -> None:
        """Main-thread slot: begin blinking the console LED blue.

        Idempotent — additional critical errors queued while the modal is
        already up keep the one running timer.
        """
        if self._error_led_timer.isActive():
            return
        self._error_led_on = False
        self._error_led_timer.start()
        self._on_error_led_tick()  # first (blue) edge now, not after 500 ms

    def _on_error_led_tick(self) -> None:
        """Toggle the console LED between blue and off; stop on failure."""
        self._error_led_on = not self._error_led_on
        state = _RGB_LED_BLUE if self._error_led_on else _RGB_LED_OFF
        if not self._set_console_led(state):
            # Console unreachable (e.g. disconnected while the modal is up)
            # — stop retrying rather than spamming failing UART writes.
            self._error_led_timer.stop()
            self._error_led_on = False

    def _set_console_led(self, state: int) -> bool:
        """Best-effort console RGB LED write. True on success; never raises."""
        try:
            return self._interface.console.set_rgb_led(state) == state
        except Exception:
            logger.debug("console LED write failed (state %d)", state, exc_info=True)
            return False
