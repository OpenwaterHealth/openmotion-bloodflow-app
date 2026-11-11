"""Console telemetry, laser safety, trigger and the error LED."""
import json
import time

from PyQt6.QtCore import QTimer, QVariant, QRecursiveMutex, pyqtProperty, pyqtSignal, pyqtSlot
from connector._common import logger
from hardware.motion_config import (
    TEC_TRIP_MAX_C, TEC_TRIP_MIN_C, TecTripOutcome, ensure_tec_trip, tec_voltage_params,
)

# constants for calculations
SCALE_V = 0.0909
SCALE_I = 0.25

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
    """Update the safety-unknown streak counter and decide whether to fire.

    Returns ``(new_streak, should_fire)``.

    - ``safety_known=False`` (chip unresponsive on this poll): increment
      the streak and report ``should_fire=True`` once it reaches the
      threshold. The connector's outer ``if not self._safetyFailure``
      guard prevents the toast from re-firing on every subsequent poll.
    - ``safety_known=True`` (or missing, for backward compat with legacy
      SDK snapshots): reset the streak; the caller handles the
      ``safety_ok`` branch separately.
    """
    if not getattr(snap, "safety_known", True):
        new_streak = prev_streak + 1
        return new_streak, new_streak >= threshold
    return 0, False


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
        self._tec_voltage = 0.0
        self._tec_temp = 0.0
        self._tec_monV = 0.0
        self._tec_monC = 0.0
        self._tec_good = False
        self._pdu_raws = [0] * 16
        self._pdu_vals = [0.0] * 16
        # Console error-LED blink (issue #257): while the critical-error
        # modal is up, blink the console front-panel LED blue instead of
        # leaving it solid green. Started via _errorLedBlinkRequested
        # (queued from worker threads); stopped and restored to idle green
        # by criticalErrorsDismissed() when the modal queue empties.
        self._error_led_timer = QTimer(self)
        self._error_led_timer.setInterval(_ERROR_LED_BLINK_MS)
        self._error_led_timer.timeout.connect(self._on_error_led_tick)
        self._error_led_on = False  # True while the blue half-period is lit

    laserStateChanged = pyqtSignal()  # Signal to notify QML of laser state changes
    safetyFailureStateChanged = pyqtSignal()  # Signal to notify QML of safety
    safetyTripDuringCaptureRequested = pyqtSignal()  # Emitted when safety trips while scan running (main-thread slot shows message & schedules cancel)
    triggerStateChanged = pyqtSignal()  # Signal to notify QML of trigger state changes
    # Private worker→main marshalling: _raise_critical may fire from USB I/O
    # or scanner worker threads, but the console error-LED blink QTimer lives
    # on the GUI thread that owns it (issue #257). Wired in connect_signals.
    _errorLedBlinkRequested = pyqtSignal()
    # post-processing signals

    pduMonChanged = pyqtSignal()
    tecStatusChanged = pyqtSignal()

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
            # Clear the persistent laser-safety toast on recovery. The
            # toast is *fired* by ``_fire_safety_notification`` in the
            # call site (readSafetyStatus) where the decoded fault
            # detail is available — we only handle dismissal here so
            # we don't double-fire without context.
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
        self.notify(
            msg,
            type_="error",
            duration_ms=0,
            dismissible=False,
            tag="laser_safety",
        )

    @pyqtProperty(str, notify=triggerStateChanged)
    def triggerState(self):
        return self._trigger_state

    def _on_telemetry_update(self, snap) -> None:
        if not snap.read_ok:
            logger.warning("Telemetry poll error: %s", snap.error)
            return

        try:
            self.tec_status(snap)
            logger.debug(
                "TEC Status – temp: %.2f set: %.2f tec_c: %.3f tec_v: %.3f good: %s",
                self._tec_voltage, self._tec_temp,
                snap.tec_curr_raw, snap.tec_volt_raw, snap.tec_good,
            )
        except Exception as exc:
            logger.error("_on_telemetry_update TEC error: %s", exc)

        try:
            self.pdu_mon(snap)
            if snap.pdu_volts:
                logger.debug(
                    "PDU MON ADC0 vals: %s",
                    " ".join(f"{(v / SCALE_V):.3f}" for v in snap.pdu_volts[:8]),
                )
                adc1_scaled = [
                    (v / SCALE_V) if idx == 6 else (v / SCALE_I)
                    for idx, v in enumerate(snap.pdu_volts[8:])
                ]
                logger.debug(
                    "PDU MON ADC1 vals: %s",
                    " ".join(f"{v:.3f}" for v in adc1_scaled),
                )
        except Exception as exc:
            logger.error("_on_telemetry_update PDU error: %s", exc)

        try:
            self.readSafetyStatus(snap)
        except Exception as exc:
            logger.error("_on_telemetry_update safety error: %s", exc)

        try:
            logger.debug(
                "Analog Values – TCM: %d, TCL: %d, PDC: %.3f",
                snap.tcm, snap.tcl, snap.pdc,
            )
        except Exception as exc:
            logger.error("_on_telemetry_update analog error: %s", exc)

    def _on_safety_trip_during_capture(self):
        """Called on main thread when safety tripped while scan was running: show message and cancel scan in 5 s."""
        if not self._capture_running or self._safety_cancel_scheduled:
            return
        self._safety_cancel_scheduled = True
        self.captureLog.emit(
            "Laser safety system tripped. Scan will be cancelled in 5 seconds."
        )
        self._note_scan_abort(
            "E-202",
            f"laser safety system tripped mid-scan "
            f"(scan elapsed {self._scan_elapsed_str()})",
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

    @pyqtSlot(result=QVariant)
    def tec_status(self, snap=None):
        if snap is None:
            snap = self._interface.console.telemetry.get_snapshot()
        if snap is None or not snap.read_ok:
            return False

        from omotion.console_telemetry_conversions import (
            tec_thermistor_voltage_to_celsius,
            tec_current_to_amps,
            tec_voltage_to_volts,
        )

        self._tec_voltage = round(
            tec_thermistor_voltage_to_celsius(snap.tec_v_raw), 2
        )
        self._tec_temp = round(
            tec_thermistor_voltage_to_celsius(snap.tec_set_raw), 2
        )
        self._tec_monC = round(tec_current_to_amps(snap.tec_curr_raw), 3)
        self._tec_monV = round(tec_voltage_to_volts(snap.tec_volt_raw), 3)
        self._tec_good = bool(snap.tec_good)

        self.tecStatusChanged.emit()
        return True

    @pyqtSlot(result=QVariant)
    def pdu_mon(self, snap=None):
        if snap is None:
            snap = self._interface.console.telemetry.get_snapshot()
        if snap is None or not snap.read_ok or not snap.pdu_raws:
            return {"ok": False, "error": "no data"}

        self._pdu_raws = list(snap.pdu_raws)
        self._pdu_vals = list(snap.pdu_volts)
        self.pduMonChanged.emit()

        return {
            "ok": True,
            "adc0": {"raws": self._pdu_raws[:8], "vals": self._pdu_vals[:8]},
            "adc1": {"raws": self._pdu_raws[8:], "vals": self._pdu_vals[8:]},
        }

    @pyqtSlot()
    def readSafetyStatus(self, snap=None):
        if snap is None:
            snap = self._interface.console.telemetry.get_snapshot()
        if snap is None:
            logger.warning("readSafetyStatus: no telemetry snapshot yet")
            return
        try:
            # If the safety interlock chip didn't respond on this poll,
            # ``safety_ok`` is the dataclass default ``True`` — not a
            # verified clear signal. Earlier #107 fix tripped on the
            # first unresponsive poll, but #119 showed that a single
            # missed I2C read can be a transient during a USB
            # disconnect cascade rather than a real chip fault. Require
            # a streak (~3 s at 1 Hz) before firing; still well under
            # the time a user takes to reach Check.
            #
            # **GRACE PERIOD**: During the first 5 seconds after console
            # connection (power-on or reconnect), allow transient
            # unresponsiveness without triggering a safety failure. This
            # prevents spurious warnings during rapid power cycles where
            # the safety chip may not respond immediately while hardware
            # initializes. After the grace period, any unresponsiveness
            # is treated as a persistent fault.
            #
            # Backward compat: snapshots from older SDK builds without
            # ``safety_known`` keep the existing (less safe) behavior
            # of trusting the default ``True``.
            self._safety_unknown_streak, should_fire_unknown = (
                _safety_unknown_streak_decision(snap, self._safety_unknown_streak)
            )
            if not getattr(snap, "safety_known", True):
                # Grace period: suppress all trips during first 5 s after connect
                if self._console_connected_at is not None:
                    time_since_connect = time.monotonic() - self._console_connected_at
                    if time_since_connect < 5.0:
                        logger.debug(
                            f"readSafetyStatus: safety chip unresponsive "
                            f"{time_since_connect:.1f}s after connect (within grace period)"
                        )
                        return
                # After grace period: require streak before firing
                if not should_fire_unknown:
                    return  # transient miss; wait for streak to confirm
                if not self._safetyFailure:
                    fault_detail = (
                        "safety interlock chip unresponsive — cannot "
                        "verify laser safety state"
                    )
                    logger.error(f"Laser safety failure: {fault_detail}")
                    self.safetyFailure = True
                    self._fire_safety_notification(fault_detail)
                    self._raise_critical("E-201", detail=fault_detail)
                    self.stopTrigger()
                    self._laserOn = False
                    self.laserStateChanged.emit()
                    if self._capture_running and not self._safety_cancel_scheduled:
                        self.safetyTripDuringCaptureRequested.emit()
                return
            if snap.safety_ok:
                if self._safetyFailure:
                    self.safetyFailure = False
            else:
                if not self._safetyFailure:
                    # Decode which safety bits tripped so the engineering-
                    # mode toast (and the log) can name them. The SDK
                    # owns the bit→label mapping in ConsoleTelemetry.
                    from omotion.ConsoleTelemetry import _decode_safety_faults
                    se_faults = _decode_safety_faults(snap.safety_se)
                    so_faults = _decode_safety_faults(snap.safety_so)
                    fault_detail = (
                        f"safety_se=0x{snap.safety_se:02X} "
                        f"({', '.join(se_faults) or 'no faults'}); "
                        f"safety_so=0x{snap.safety_so:02X} "
                        f"({', '.join(so_faults) or 'no faults'})"
                    )
                    logger.error(f"Laser safety failure: {fault_detail}")
                    self.safetyFailure = True
                    self._fire_safety_notification(fault_detail)
                    self.stopTrigger()
                    # laserStateChanged is the notify signal for the
                    # zero-arg laserOn pyqtProperty — set the underlying
                    # state and emit() without args so QML re-reads the
                    # property. Previous form passed `False` and PyQt
                    # raised "signal has 0 arguments but 1 provided",
                    # which surfaced via the dev-mode safety toast.
                    self._laserOn = False
                    self.laserStateChanged.emit()
                    self._surface_safety_trip(fault_detail)
        except Exception as e:
            logger.error(f"readSafetyStatus failed: {e}")
            self.safetyFailure = True
            self._fire_safety_notification(f"telemetry exception: {e}")
            if self._capture_running and not self._safety_cancel_scheduled:
                self.safetyTripDuringCaptureRequested.emit()

    @pyqtSlot(str, result=bool)
    def setTrigger(self, triggerjson):  # Lock auto-released at function exit
        try:
            json_trigger_data = json.loads(triggerjson)

            # Resolve to (interface default ⊕ override). QML callers
            # only need to specify the fields they want to change
            # (typically just ``TriggerStatus``); absent fields fall
            # through to the SDK's resolved default rather than being
            # sent as missing keys.
            json_trigger_data = self._interface.resolve_trigger_config(
                json_trigger_data
            )

            # Engineering override (#446 family): alternative laser pulse
            # width, applied to every trigger push while enabled. (The
            # dark-frame skip needs no counterpart here — it lives in the
            # interface's default_trigger_config (main.py, #449), so the
            # resolve above already carries it.)
            json_trigger_data = self._apply_alt_laser_pulse_width(
                json_trigger_data
            )

            trigger_setting = self._interface.console.set_trigger_json(
                data=json_trigger_data
            )
            if trigger_setting:
                logger.info(f"Trigger Setting: {trigger_setting}")
                return True
            else:
                logger.error("Failed to set trigger setting.")
                return False

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON data: {e}")
            return False

        except AttributeError as e:
            logger.error(f"Invalid interface or method: {e}")
            return False

        except Exception as e:
            logger.error(f"Unexpected error while setting trigger: {e}")
            return False

    @pyqtSlot(result=bool)
    def startTrigger(self):
        success = self._interface.console.start_trigger()
        if success:
            self._trigger_state = "ON"
            self.triggerStateChanged.emit()
            logger.info("Trigger started successfully.")
        return success

    @pyqtSlot()
    def stopTrigger(self):
        self._interface.console.stop_trigger()
        self._trigger_state = "OFF"
        self.triggerStateChanged.emit()
        logger.info("Trigger stopped.")

    def set_laser_power_from_config(self, interface):
        # Laser-power config now lives in the SDK (omotion.laser): it bundles
        # the FPGA register map + the laser param set and writes them over I2C.
        # force_fault honors the forceLaserFail app flag (loads the
        # safety-trip param set to exercise the interlock).
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
                f"trip untouched (expected a number in "
                f"{TEC_TRIP_MIN_C}-{TEC_TRIP_MAX_C} °C)"
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
            logger.debug("console LED write failed (state %d)", state,
                         exc_info=True)
            return False
