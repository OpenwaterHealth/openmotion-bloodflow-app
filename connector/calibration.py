"""Calibrate and Test flows (Settings -> Engineering)."""
import os
import time

from PyQt6.QtCore import pyqtProperty, pyqtSignal, pyqtSlot
from connector._common import logger

# The two flows share one state machine; per flow: (capture-log label, the
# verb in "Cannot …" refusals). The flow key is also the attribute prefix
# (``_<flow>_status`` / ``_<flow>_failure_reason`` / ``_<flow>_t0``).
_FLOWS = {
    "calibration": ("Calibration", "calibrate"),
    "test_scan": ("Test scan", "run Test scan"),
}
# Per-camera threshold lists (appConfig ``ft_<name>``, cached as
# ``self._ft_<name>``). An empty min list falls back to the factory bar
# (truthiness); an empty max list is honoured (only None falls back).
_FT_MIN = ("min_mean_per_camera", "min_contrast_per_camera",
           "min_bfi_per_camera", "min_bvi_per_camera")
# max_dark (#122): per-camera max dark-frame mean, gating on ambient light.
_FT_MAX = ("max_bfi_per_camera", "max_bvi_per_camera", "max_dark_per_camera")
_CALIBRATION_TESTS = (("mean", "mean_test"), ("contrast", "contrast_test"), ("bfi", "bfi_test"),
                      ("bvi", "bvi_test"), ("ambient", "dark_test"))
_TEST_SCAN_TESTS = (("mean", "mean_test"), ("contrast", "contrast_test"), ("ambient", "dark_test"))


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
        for name in _FT_MIN + _FT_MAX:
            value = cfg.get(f"ft_{name}")
            setattr(self, f"_ft_{name}", list(value) if isinstance(value, (list, tuple)) else None)
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

    # Emitted on any status change (running/passed/failed/canceled/timed_out/error/idle).
    calibrationStateChanged = pyqtSignal()
    testScanStateChanged = pyqtSignal()
    # Private worker→main marshalling of the SDK result.
    _calibrationCompleteSignal = pyqtSignal(object)
    _testScanCompleteSignal = pyqtSignal(object)

    # --- Properties consumed by SettingsModal.qml ---
    @pyqtProperty(bool, notify=calibrationStateChanged)
    def calibrationRunning(self) -> bool:
        """True while a calibration is in flight."""
        return self._calibration_status == "running"

    @pyqtProperty(str, notify=calibrationStateChanged)
    def calibrationStatus(self) -> str:
        """Calibration status ("" when idle, "running", or the last outcome)."""
        return self._calibration_status

    @pyqtProperty(str, notify=calibrationStateChanged)
    def calibrationFailureReason(self) -> str:
        """Why the last calibration did not pass ("" if it did)."""
        return self._calibration_failure_reason

    @pyqtProperty(int, notify=calibrationStateChanged)
    def maxCalibrationTimeSec(self) -> int:
        """Watchdog limit for one calibration / Test run."""
        return self._max_calibration_time_sec

    @pyqtProperty(bool, notify=testScanStateChanged)
    def testScanRunning(self) -> bool:
        """True while a Test scan is in flight."""
        return self._test_scan_status == "running"

    @pyqtProperty(str, notify=testScanStateChanged)
    def testScanStatus(self) -> str:
        """Test-scan status ("" when idle, "running", or the last outcome)."""
        return self._test_scan_status

    @pyqtProperty(str, notify=testScanStateChanged)
    def testScanFailureReason(self) -> str:
        """Why the last Test scan did not pass ("" if it did)."""
        return self._test_scan_failure_reason

    @pyqtProperty('QVariantList', notify=testScanStateChanged)
    def testScanRows(self) -> list:
        """Per-camera result rows of the last Test scan."""
        return self._test_scan_rows

    def _ft_thresholds(self):
        """``CalibrationThresholds`` from the ft_* config values, falling
        back per-field to the SDK's factory acceptance thresholds.

        The fallback used to be ``[0.0]*8``: bars that can never fail, which
        turned the SDK's pre-write gate, rollback and PASS verdict into
        no-ops (#473). The SDK now also refuses to start a run whose gate
        cannot fail, so a config that still zeroes the thresholds surfaces
        as a loud refusal in the capture log instead of a below-spec EEPROM
        write displayed as PASSED.
        """
        from omotion import CalibrationThresholds, factory_calibration_thresholds

        factory = factory_calibration_thresholds()
        fields = {n: list(getattr(self, f"_ft_{n}") or getattr(factory, n)) for n in _FT_MIN}
        for name in _FT_MAX:
            own = getattr(self, f"_ft_{name}")
            fields[name] = list(own) if own is not None else getattr(factory, name)
        return CalibrationThresholds(**fields)

    def _flow_changed(self, flow: str) -> None:
        if flow == "calibration":
            self.calibrationStateChanged.emit()
        else:
            self.testScanStateChanged.emit()

    def _start_flow(self, flow, target, duration_sec, start_fn, complete_signal) -> None:
        """Shared body of runCalibration / runTestScan, after their own gates:
        the common refusals, the request, the "running" state and the SDK start.

        Camera mask is ``0xFF`` per chosen, connected side (every camera on
        the sensor); the app config's leftMask/rightMask don't apply.
        """
        from omotion import CalibrationRequest

        label, verb = _FLOWS[flow]
        if not self._consoleConnected:
            self.captureLog.emit(f"⚠️ Cannot {verb}: console not connected.")
            return
        if self.deviceInitBusy:
            # The laser registers may not be loaded yet (_init_console).
            self.captureLog.emit(f"⚠️ Cannot {verb}: the system is still initializing.")
            return
        target = (target or "both").lower().strip()
        if target not in ("left", "right", "both"):
            self.captureLog.emit(f"⚠️ Cannot {verb}: invalid target '{target}'.")
            return

        left_mask = 0xFF if target != "right" and self._leftSensorConnected else 0x00
        right_mask = 0xFF if target != "left" and self._rightSensorConnected else 0x00
        if (left_mask | right_mask) == 0:
            # #362: the dropdown filters disconnected targets, but keep the
            # guard for programmatic callers and races (sensor unplugged
            # between selection and click). Park the reason in the persistent
            # status too: a captureLog line alone is invisible to an operator
            # in the Settings overlay (it read as "no action is performed").
            reason = "no sensors connected"
            if target != "both":
                reason = f"{target} sensor not connected"
            self.captureLog.emit(f"⚠️ Cannot {verb}: {reason}.")
            setattr(self, f"_{flow}_status", "error")
            setattr(self, f"_{flow}_failure_reason", reason)
            self._flow_changed(flow)
            return

        thresholds = self._ft_thresholds()
        output_dir = os.path.join(self._data_root, "calibrations")
        os.makedirs(output_dir, exist_ok=True)
        # No trigger override: the workflow resolves the interface's default
        # trigger config (SDK ⊕ app override), like the scan / CQ flows.
        req = CalibrationRequest(
            operator_id="bloodflow-app", output_dir=output_dir, thresholds=thresholds,
            left_camera_mask=left_mask, right_camera_mask=right_mask,
            duration_sec=duration_sec,
            scan_delay_sec=self._calibration_scan_delay_sec,
            max_duration_sec=self._max_calibration_time_sec,
        )

        setattr(self, f"_{flow}_status", "running")
        if flow == "calibration":
            self._calibration_target = target
        else:
            self._test_scan_rows = []
            self._test_scan_failure_reason = ""
        self._flow_changed(flow)
        self.captureLog.emit(f"{label}: starting…")
        setattr(self, f"_{flow}_t0", time.monotonic())
        if flow == "calibration":
            self._audit.log("calibration_started", {"target": target})
        logger.info(
            "=== %s started: target=%s left=0x%02X right=0x%02X scan_duration=%ss ===",
            label, target, left_mask, right_mask, duration_sec,
        )

        started = start_fn(req, on_log_fn=lambda msg: self.captureLog.emit(msg),
                           on_complete_fn=complete_signal.emit)
        if not started:
            setattr(self, f"_{flow}_status", "")
            self._flow_changed(flow)
            self.captureLog.emit(f"⚠️ {label} failed to start.")

    def _finish_flow(self, flow, result, tests) -> None:
        """Shared head of the two completion slots (Qt main thread): status,
        failure reason, capture-log verdict and the end banner. The caller
        emits the state-changed signal."""
        label = _FLOWS[flow][0]
        outcome = _result_outcome(result)
        setattr(self, f"_{flow}_status", outcome)
        reason = ""
        if outcome == "failed":
            if self._engineering:
                reason = _format_threshold_breakdown(result.rows, tests)
            # Calibration only: result.error carries the pre-write gate's
            # below-threshold detail when the run failed there.
            error = getattr(result, "error", "") if flow == "calibration" else ""
            if error:
                reason = f"{reason} — {error}" if reason else error
        elif outcome != "passed":
            # canceled / timed_out / error: surface the SDK's reason in the
            # persistent status label, not just the transient log line.
            reason = result.error or ""
        setattr(self, f"_{flow}_failure_reason", reason)

        if outcome == "passed":
            self.captureLog.emit(f"✅ {label}: PASS  (CSV: {result.csv_path})")
        elif outcome == "failed":
            self.captureLog.emit(f"❌ {label}: FAIL  (CSV: {result.csv_path})")
            if flow == "calibration":
                # The SDK writes the EEPROM only after a fully-passing validation.
                self.captureLog.emit(
                    "Nothing was written to the console — it keeps its existing calibration.")
        else:
            icon = "⚠️" if outcome in ("canceled", "timed_out") else "❌"
            self.captureLog.emit(
                f"{icon} {label} {outcome.replace('_', ' ')}: {result.error or 'unknown'}")

        t0 = getattr(self, f"_{flow}_t0")
        elapsed_s = time.monotonic() - t0 if t0 is not None else 0.0
        logger.info("=== %s ended: %s after %.1fs ===", label, outcome, elapsed_s)

    @pyqtSlot()
    @pyqtSlot(str)
    def runCalibration(self, target: str = "both"):
        """Kick off the SDK calibration procedure; no-op if already running.

        ``target`` is ``"left"``, ``"right"`` or ``"both"`` (default): test
        stations with one static phantom calibrate one side at a time (#117).
        Refused without engineering mode (#706): Calibrate lives in the
        engineering card, and the unlock is its only gate. The SDK enforces
        the never-write rule itself (any camera outside any threshold fails
        the run and the console EEPROM is never touched), so there is no
        consent hook to wire up.
        """
        if self._calibration_status == "running":
            return
        if not self._require_engineering_mode("calibrate"):
            return
        self._start_flow("calibration", target, self._calibration_scan_duration_sec,
                         self._interface.start_calibration, self._calibrationCompleteSignal)

    @pyqtSlot()
    def cancelCalibration(self):
        """Cancel the in-flight calibration or Test scan. Non-blocking:
        join_timeout=0 skips the SDK's thread join (up to 10 s by default,
        which would freeze the GUI thread); completion still arrives via
        the normal on_complete callback with outcome=canceled."""
        if self._calibration_status != "running" and self._test_scan_status != "running":
            return
        self.captureLog.emit("Canceling…")
        self._interface.cancel_calibration(join_timeout=0)

    @pyqtSlot()
    @pyqtSlot(str)
    def runTestScan(self, target: str = "both"):
        """Run just the calibration scan (phase 1) as a Test diagnostic: no
        EEPROM write, no validation scan; no-op if a Test scan is running.

        ``target`` as in runCalibration (#117); refused without engineering
        mode, like runCalibration (#706). OQ8: uses the shorter
        ``test_scan_duration_sec``, not the calibration scan duration.
        """
        if self._test_scan_status == "running":
            return
        if not self._require_engineering_mode("test_scan"):
            return
        if self._calibration_status == "running":  # mutual exclusion with Calibrate
            self.captureLog.emit("⚠️ Cannot run Test scan: calibration in progress.")
            return
        self._start_flow("test_scan", target, self._test_scan_duration_sec,
                         self._interface.start_test_scan, self._testScanCompleteSignal)

    @pyqtSlot(object)
    def _on_test_scan_complete(self, result):
        """Main thread (queued via _testScanCompleteSignal): status plus the
        QML-friendly ``_test_scan_rows`` model for a TestScanResult."""
        self._finish_flow("test_scan", result, _TEST_SCAN_TESTS)

        def bar(name, cam_id):
            values = getattr(self, f"_ft_{name}_per_camera")
            return values[cam_id] if values and cam_id < len(values) else None

        self._test_scan_rows = [
            {
                "side": r.side,
                "cam": r.cam_id + 1,
                "light_mean": r.mean,
                "min_mean": bar("min_mean", r.cam_id),
                "mean_pf": r.mean_test,
                "dark_mean": r.dark,
                "max_dark": bar("max_dark", r.cam_id),
                "dark_pf": r.dark_test,
                "contrast": r.avg_contrast,
                "min_contrast": bar("min_contrast", r.cam_id),
                "contrast_pf": r.contrast_test,
                "overall": (
                    "PASS"
                    if r.mean_test == "PASS" and r.contrast_test == "PASS" and r.dark_test != "FAIL"
                    else "FAIL"
                ),
            }
            for r in result.rows
        ]
        self.testScanStateChanged.emit()

    @pyqtSlot(object)
    def _on_calibration_complete(self, result):
        """Main thread (queued via _calibrationCompleteSignal): status + audit."""
        self._finish_flow("calibration", result, _CALIBRATION_TESTS)
        self._audit.log("calibration_ended", {
            "target": self._calibration_target,
            "outcome": self._calibration_status,
            "reason": self._calibration_failure_reason or None,
        })
        self.calibrationStateChanged.emit()
