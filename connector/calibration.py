"""Calibrate and Test flows (Settings -> Engineering)."""
import os
import time

from PyQt6.QtCore import pyqtProperty, pyqtSignal, pyqtSlot
from connector._common import logger

def _result_outcome(result) -> str:
    """Terminal outcome of a Calibration/TestScan result as a plain string.

    Prefers the SDK's authoritative ``outcome`` field; falls back to
    deriving from the legacy boolean triple for SDKs that predate it
    (pre-outcome SDKs cannot distinguish a watchdog timeout from a
    cancel, so both report "canceled" there).
    """
    out = getattr(result, "outcome", None)
    if out is not None:
        return str(getattr(out, "value", out))
    if result.canceled:
        return "canceled"
    if not result.ok:
        return "error"
    return "passed" if result.passed else "failed"

def _format_threshold_breakdown(rows, tests) -> str:
    """Per-camera FAIL breakdown, e.g. 'L1:mean,contrast; R3:bfi'.
    ``tests`` is a tuple of (label, row_attr) pairs."""
    breakdown = "; ".join(
        f"{'L' if r.side == 'left' else 'R'}{r.cam_id + 1}:"
        f"{','.join(n for n, a in tests if getattr(r, a) == 'FAIL')}"
        for r in rows
        if any(getattr(r, a) == "FAIL" for _, a in tests)
    )
    # #122: call out ambient explicitly so "ambient" isn't misread as a
    # generic test name.
    if any(r.dark_test == "FAIL" for r in rows):
        breakdown = f"too much ambient light — {breakdown}"
    return breakdown


class CalibrationMixin:
    """Calibrate and Test flows (Settings -> Engineering)."""

    def _setup_calibration(self, cfg: dict) -> None:
        def per_camera(key):
            value = cfg.get(key)
            return list(value) if isinstance(value, (list, tuple)) else None

        self._ft_min_mean_per_camera = per_camera("ft_min_mean_per_camera")
        self._ft_min_contrast_per_camera = per_camera("ft_min_contrast_per_camera")
        self._ft_min_bfi_per_camera = per_camera("ft_min_bfi_per_camera")
        self._ft_max_bfi_per_camera = per_camera("ft_max_bfi_per_camera")
        self._ft_min_bvi_per_camera = per_camera("ft_min_bvi_per_camera")
        self._ft_max_bvi_per_camera = per_camera("ft_max_bvi_per_camera")
        # #122: per-camera max dark-frame mean, gating on ambient light.
        self._ft_max_dark_per_camera = per_camera("ft_max_dark_per_camera")
        self._max_calibration_time_sec = int(cfg.get("max_calibration_time_sec", 600))
        self._calibration_scan_duration_sec = int(cfg.get("calibration_scan_duration_sec", 5))
        self._calibration_scan_delay_sec = int(cfg.get("calibration_scan_delay_sec", 1))
        self._test_scan_duration_sec = int(cfg.get("test_scan_duration_sec", 5))
        # "", "running", "passed", "failed", "canceled", "timed_out", "error"
        self._calibration_status = ""
        self._calibration_failure_reason = ""  # populated only on FAIL in dev mode
        self._calibration_target = None  # last runCalibration() target
        self._test_scan_status = ""
        self._test_scan_failure_reason = ""
        self._test_scan_rows: list[dict] = []
        # Start times for the start/end log banners (monotonic, None = never run).
        self._calibration_t0 = None
        self._test_scan_t0 = None

    # Calibration procedure signals
    calibrationStateChanged = pyqtSignal()  # any of running/passed/failed/canceled/timed_out/error/idle
    _calibrationCompleteSignal = pyqtSignal(object)  # private worker→main marshalling
    testScanStateChanged = pyqtSignal()                # any of running/passed/failed/canceled/timed_out/error/idle
    _testScanCompleteSignal = pyqtSignal(object)       # private worker→main marshalling

    # --- Calibration procedure properties (consumed by SettingsModal.qml) ---
    @pyqtProperty(bool, notify=calibrationStateChanged)
    def calibrationRunning(self) -> bool:
        return self._calibration_status == "running"

    @pyqtProperty(str, notify=calibrationStateChanged)
    def calibrationStatus(self) -> str:
        return self._calibration_status

    @pyqtProperty(str, notify=calibrationStateChanged)
    def calibrationFailureReason(self) -> str:
        return self._calibration_failure_reason

    @pyqtProperty(int, notify=calibrationStateChanged)
    def maxCalibrationTimeSec(self) -> int:
        return self._max_calibration_time_sec

    @pyqtProperty(bool, notify=testScanStateChanged)
    def testScanRunning(self) -> bool:
        return self._test_scan_status == "running"

    @pyqtProperty(str, notify=testScanStateChanged)
    def testScanStatus(self) -> str:
        return self._test_scan_status

    @pyqtProperty(str, notify=testScanStateChanged)
    def testScanFailureReason(self) -> str:
        return self._test_scan_failure_reason

    @pyqtProperty('QVariantList', notify=testScanStateChanged)
    def testScanRows(self) -> list:
        return self._test_scan_rows

    def _ft_thresholds(self):
        """``CalibrationThresholds`` from the ft_* config values, falling
        back per-field to the SDK's factory acceptance thresholds.

        The fallback used to be ``[0.0]*8`` — bars that can never fail,
        which turned the SDK's calibration pre-write gate, rollback, and
        PASS verdict into no-ops (#473). The SDK now also refuses to start
        a run whose gate cannot fail, so a config that somehow still zeroes
        the thresholds surfaces as a loud refusal in the capture log
        instead of a below-spec EEPROM write displayed as PASSED.
        """
        from omotion import CalibrationThresholds, factory_calibration_thresholds

        f = factory_calibration_thresholds()
        return CalibrationThresholds(
            min_mean_per_camera=list(
                self._ft_min_mean_per_camera or f.min_mean_per_camera),
            min_contrast_per_camera=list(
                self._ft_min_contrast_per_camera or f.min_contrast_per_camera),
            min_bfi_per_camera=list(
                self._ft_min_bfi_per_camera or f.min_bfi_per_camera),
            min_bvi_per_camera=list(
                self._ft_min_bvi_per_camera or f.min_bvi_per_camera),
            max_bfi_per_camera=(
                list(self._ft_max_bfi_per_camera)
                if self._ft_max_bfi_per_camera is not None
                else f.max_bfi_per_camera
            ),
            max_bvi_per_camera=(
                list(self._ft_max_bvi_per_camera)
                if self._ft_max_bvi_per_camera is not None
                else f.max_bvi_per_camera
            ),
            max_dark_per_camera=(
                list(self._ft_max_dark_per_camera)
                if self._ft_max_dark_per_camera is not None
                else f.max_dark_per_camera
            ),
        )

    @pyqtSlot()
    @pyqtSlot(str)
    def runCalibration(self, target: str = "both"):
        """Kick off the SDK calibration procedure. Idempotent if already
        running. Marshals the worker-thread completion back onto the Qt
        event loop via _calibrationCompleteSignal.

        ``target`` selects which side(s) to calibrate: ``"left"``,
        ``"right"``, or ``"both"`` (default). Issue #117 — test stations
        with only one static phantom need to calibrate one side at a time.
        Camera mask is still ``0xFF`` per side (every camera on the chosen
        sensor); the app config's leftMask/rightMask still don't apply.

        Refused without engineering mode (#706): Calibrate lives in the
        engineering card, and the unlock is its only gate.
        """
        from omotion import CalibrationRequest

        if self._calibration_status == "running":
            return

        if not self._require_engineering_mode("calibrate"):
            return

        if not self._consoleConnected:
            self.captureLog.emit("⚠️ Cannot calibrate: console not connected.")
            return
        if self.deviceInitBusy:
            # The laser registers may not be loaded yet (_init_console).
            self.captureLog.emit(
                "⚠️ Cannot calibrate: the system is still initializing.")
            return

        target = (target or "both").lower().strip()
        if target not in ("left", "right", "both"):
            self.captureLog.emit(
                f"⚠️ Cannot calibrate: invalid target '{target}'."
            )
            return

        want_left  = target in ("left", "both")
        want_right = target in ("right", "both")
        left_mask  = 0xFF if (want_left  and self._leftSensorConnected)  else 0x00
        right_mask = 0xFF if (want_right and self._rightSensorConnected) else 0x00
        if (left_mask | right_mask) == 0:
            # #362 — the dropdown now filters disconnected targets, but keep
            # the guard for programmatic callers and races (sensor unplugged
            # between selection and click). Surface it in the persistent
            # status label too: a captureLog line alone is invisible to an
            # operator sitting in the Settings overlay, which is exactly why
            # #362 read as "no action is performed".
            if target == "left" and not self._leftSensorConnected:
                reason = "left sensor not connected"
            elif target == "right" and not self._rightSensorConnected:
                reason = "right sensor not connected"
            else:
                reason = "no sensors connected"
            self.captureLog.emit(f"⚠️ Cannot calibrate: {reason}.")
            self._calibration_status = "error"
            self._calibration_failure_reason = reason
            self.calibrationStateChanged.emit()
            return

        thresholds = self._ft_thresholds()
        output_dir = os.path.join(self._data_root, "calibrations")
        os.makedirs(output_dir, exist_ok=True)
        # CalibrationWorkflow resolves the trigger config to the
        # interface's default (SDK ⊕ app override at construction)
        # when the request doesn't override — matches what the QML
        # scan / CQ flows do via SetTriggerTask. Pass None so
        # the workflow always sees the canonical config.
        req = CalibrationRequest(
            operator_id="bloodflow-app",
            output_dir=output_dir,
            left_camera_mask=left_mask,
            right_camera_mask=right_mask,
            thresholds=thresholds,
            duration_sec=self._calibration_scan_duration_sec,
            scan_delay_sec=self._calibration_scan_delay_sec,
            max_duration_sec=self._max_calibration_time_sec,
        )

        self._calibration_status = "running"
        self._calibration_target = target
        self.calibrationStateChanged.emit()
        self.captureLog.emit("Calibration: starting…")
        self._calibration_t0 = time.monotonic()
        self._audit.log("calibration_started", {"target": target})
        logger.info(
            "=== Calibration started: target=%s left=0x%02X right=0x%02X "
            "scan_duration=%ss ===",
            target, left_mask, right_mask, self._calibration_scan_duration_sec,
        )

        # The SDK enforces the never-write rule itself: any camera outside
        # any threshold means the run FAILS and the console EEPROM is never
        # touched — there is no consent hook to wire up.
        started = self._interface.start_calibration(
            req,
            on_log_fn=lambda msg: self.captureLog.emit(msg),
            on_complete_fn=self._calibrationCompleteSignal.emit,
        )
        if not started:
            self._calibration_status = ""
            self.calibrationStateChanged.emit()
            self.captureLog.emit("⚠️ Calibration failed to start.")

    @pyqtSlot()
    def cancelCalibration(self):
        """Cancel the in-flight calibration or Test scan. Non-blocking:
        join_timeout=0 skips the SDK's thread join (up to 10 s by default,
        which would freeze the GUI thread); completion still arrives via
        the normal on_complete callback with outcome=canceled."""
        if (self._calibration_status != "running"
                and self._test_scan_status != "running"):
            return
        self.captureLog.emit("Canceling…")
        self._interface.cancel_calibration(join_timeout=0)

    @pyqtSlot()
    @pyqtSlot(str)
    def runTestScan(self, target: str = "both"):
        """Run just the calibration scan (phase 1) as a Test diagnostic.
        Mirrors runCalibration but does NOT write calibration to the
        console EEPROM and does NOT run a validation scan. Idempotent
        if a calibration or test scan is already in flight.

        ``target`` selects which side(s) to test: ``"left"``,
        ``"right"``, or ``"both"`` (default). Issue #117 — test stations
        with only one static phantom need to test one side at a time.

        Refused without engineering mode, like runCalibration (#706).
        """
        from omotion import CalibrationRequest

        # Mutual exclusion with the Calibrate flow.
        if self._test_scan_status == "running":
            return
        if not self._require_engineering_mode("test_scan"):
            return
        if self._calibration_status == "running":
            self.captureLog.emit(
                "⚠️ Cannot run Test scan: calibration in progress."
            )
            return

        if not self._consoleConnected:
            self.captureLog.emit(
                "⚠️ Cannot run Test scan: console not connected."
            )
            return
        if self.deviceInitBusy:
            # The laser registers may not be loaded yet (_init_console).
            self.captureLog.emit(
                "⚠️ Cannot run Test scan: the system is still initializing.")
            return

        target = (target or "both").lower().strip()
        if target not in ("left", "right", "both"):
            self.captureLog.emit(
                f"⚠️ Cannot run Test scan: invalid target '{target}'."
            )
            return

        want_left  = target in ("left", "both")
        want_right = target in ("right", "both")
        left_mask  = 0xFF if (want_left  and self._leftSensorConnected)  else 0x00
        right_mask = 0xFF if (want_right and self._rightSensorConnected) else 0x00
        if (left_mask | right_mask) == 0:
            # #362 — same visibility fix as runCalibration: the Test row's
            # status lives in the results window, so park the reason there
            # instead of only in the transient capture log.
            if target == "left" and not self._leftSensorConnected:
                reason = "left sensor not connected"
            elif target == "right" and not self._rightSensorConnected:
                reason = "right sensor not connected"
            else:
                reason = "no sensors connected"
            self.captureLog.emit(f"⚠️ Cannot run Test scan: {reason}.")
            self._test_scan_status = "error"
            self._test_scan_failure_reason = reason
            self.testScanStateChanged.emit()
            return

        thresholds = self._ft_thresholds()
        output_dir = os.path.join(self._data_root, "calibrations")
        os.makedirs(output_dir, exist_ok=True)
        req = CalibrationRequest(
            operator_id="bloodflow-app",
            output_dir=output_dir,
            left_camera_mask=left_mask,
            right_camera_mask=right_mask,
            thresholds=thresholds,
            duration_sec=self._test_scan_duration_sec,   # OQ8: Test uses shorter duration (default 5s), NOT _calibration_scan_duration_sec (15s)
            scan_delay_sec=self._calibration_scan_delay_sec,
            max_duration_sec=self._max_calibration_time_sec,
        )

        self._test_scan_status = "running"
        self._test_scan_rows = []
        self._test_scan_failure_reason = ""
        self.testScanStateChanged.emit()
        self.captureLog.emit("Test scan: starting…")
        self._test_scan_t0 = time.monotonic()
        logger.info(
            "=== Test scan started: target=%s left=0x%02X right=0x%02X "
            "duration=%ss ===",
            target, left_mask, right_mask, self._test_scan_duration_sec,
        )

        started = self._interface.start_test_scan(
            req,
            on_log_fn=lambda msg: self.captureLog.emit(msg),
            on_complete_fn=self._testScanCompleteSignal.emit,
        )
        if not started:
            self._test_scan_status = ""
            self.testScanStateChanged.emit()
            self.captureLog.emit("⚠️ Test scan failed to start.")

    @pyqtSlot(object)
    def _on_test_scan_complete(self, result):
        """Runs on the Qt main thread (queued from the SDK worker via
        _testScanCompleteSignal). Translates a TestScanResult into the
        QML-friendly _test_scan_rows model and updates _test_scan_status.
        """
        self._test_scan_failure_reason = ""  # reset each run
        outcome = _result_outcome(result)
        self._test_scan_status = outcome
        if outcome == "passed":
            self.captureLog.emit(
                f"✅ Test scan: PASS  (CSV: {result.csv_path})"
            )
        elif outcome == "failed":
            if self._app_config.get("engineeringMode", False):
                tests = (("mean", "mean_test"), ("contrast", "contrast_test"),
                         ("ambient", "dark_test"))
                self._test_scan_failure_reason = _format_threshold_breakdown(
                    result.rows, tests)
            self.captureLog.emit(
                f"❌ Test scan: FAIL  (CSV: {result.csv_path})"
            )
        else:
            # canceled / timed_out / error — surface the SDK's reason in the
            # persistent status label, not just this transient log line.
            self._test_scan_failure_reason = result.error or ""
            icon = "⚠️" if outcome in ("canceled", "timed_out") else "❌"
            self.captureLog.emit(
                f"{icon} Test scan {outcome.replace('_', ' ')}: "
                f"{result.error or 'unknown'}"
            )

        elapsed_s = (
            time.monotonic() - self._test_scan_t0
            if self._test_scan_t0 is not None
            else 0.0
        )
        logger.info(
            "=== Test scan ended: %s after %.1fs ===",
            self._test_scan_status, elapsed_s,
        )

        # Build the QML-friendly row dicts.
        self._test_scan_rows = [
            {
                "side": r.side,
                "cam": r.cam_id + 1,
                "light_mean": r.mean,
                "min_mean": (
                    self._ft_min_mean_per_camera[r.cam_id]
                    if self._ft_min_mean_per_camera
                    and r.cam_id < len(self._ft_min_mean_per_camera)
                    else None
                ),
                "mean_pf": r.mean_test,
                "dark_mean": r.dark,
                "max_dark": (
                    self._ft_max_dark_per_camera[r.cam_id]
                    if self._ft_max_dark_per_camera
                    and r.cam_id < len(self._ft_max_dark_per_camera)
                    else None
                ),
                "dark_pf": r.dark_test,
                "contrast": r.avg_contrast,
                "min_contrast": (
                    self._ft_min_contrast_per_camera[r.cam_id]
                    if self._ft_min_contrast_per_camera
                    and r.cam_id < len(self._ft_min_contrast_per_camera)
                    else None
                ),
                "contrast_pf": r.contrast_test,
                "overall": (
                    "PASS"
                    if r.mean_test == "PASS"
                    and r.contrast_test == "PASS"
                    and r.dark_test != "FAIL"
                    else "FAIL"
                ),
            }
            for r in result.rows
        ]
        self.testScanStateChanged.emit()

    @pyqtSlot(object)
    def _on_calibration_complete(self, result):
        """Runs on the Qt main thread (queued from worker via signal)."""
        self._calibration_failure_reason = ""  # reset each run
        outcome = _result_outcome(result)
        self._calibration_status = outcome
        if outcome == "passed":
            self.captureLog.emit(f"✅ Calibration: PASS  (CSV: {result.csv_path})")
        elif outcome == "failed":
            if self._app_config.get("engineeringMode", False):
                tests = (("mean", "mean_test"), ("contrast", "contrast_test"),
                         ("bfi", "bfi_test"), ("bvi", "bvi_test"),
                         ("ambient", "dark_test"))
                self._calibration_failure_reason = _format_threshold_breakdown(
                    result.rows, tests)
            # A FAILED run never touches the console (the SDK writes the
            # EEPROM only after a fully-passing validation); result.error
            # carries the gate's below-threshold detail when the run failed
            # at the pre-write gate.
            if getattr(result, "error", ""):
                self._calibration_failure_reason = (
                    f"{self._calibration_failure_reason} — {result.error}"
                    if self._calibration_failure_reason
                    else result.error
                )
            self.captureLog.emit(f"❌ Calibration: FAIL  (CSV: {result.csv_path})")
            self.captureLog.emit(
                "Nothing was written to the console — it keeps its "
                "existing calibration."
            )
        else:
            # canceled / timed_out / error — surface the SDK's reason in the
            # persistent status label, not just this transient log line.
            self._calibration_failure_reason = result.error or ""
            icon = "⚠️" if outcome in ("canceled", "timed_out") else "❌"
            self.captureLog.emit(
                f"{icon} Calibration {outcome.replace('_', ' ')}: "
                f"{result.error or 'unknown'}"
            )
        elapsed_s = (
            time.monotonic() - self._calibration_t0
            if self._calibration_t0 is not None
            else 0.0
        )
        logger.info(
            "=== Calibration ended: %s after %.1fs ===",
            self._calibration_status, elapsed_s,
        )
        self._audit.log("calibration_ended", {
            "target": self._calibration_target,
            "outcome": self._calibration_status,
            "reason": self._calibration_failure_reason or None,
        })
        self.calibrationStateChanged.emit()
