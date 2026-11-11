"""Scan lifecycle: start/stop, watchdogs, abort paths, trigger clock, notes."""
import functools
import math
import os
import random
import string
import threading
import time
from typing import Optional

from PyQt6.QtCore import QTimer, pyqtProperty, pyqtSignal, pyqtSlot
from omotion.ScanWorkflow import ConfigureRequest, ScanRequest
from omotion.contact_quality import CQThresholds, ContactQualityMonitor

from config.app_config import APP_CONFIG as _COMPILED_APP_CONFIG
from connector._common import hms, logger, scan_db
from connector.contact_quality import (
    CQ_DEFAULT_DARK_THRESHOLD_DN, CQ_DEFAULT_LIGHT_THRESHOLD_DN, CQ_DEFAULT_LIVE_ACTIVATE_FRAMES,
    CQ_DEFAULT_LIVE_CLEAR_FRAMES, CQ_DEFAULT_ROLLING_WINDOW,
)
from connector.history import _sdk_meta_dict
from connector.sinks import (
    ScanOutcome, _CompletionSink, _LivePlotSink, _ScanOutcomeSink, _TriggerStateSink,
    audit_scan_ended_details, classify_scan_outcome,
)
from plot.data_sources import LiveScanSource
from plot.nan_gap_tracker import NanGapTracker, gap_note_line
from utils import disk_space

# Mid-scan free-space poll period (issue #506). At the worst measured write
# rate (16 cameras with raw CSVs, ~2.6 MB/s) this spends ~13 MB of the
# 100 MB stop threshold between polls.
_STORAGE_CHECK_INTERVAL_S = 5.0

# Toast tag shared by the mid-scan low-storage warning and the stop notice,
# so the stop replaces the warning instead of stacking under it (#506).
_LOW_STORAGE_TOAST_TAG = "low_storage"


def _scan_data_stall_decision(now_mono: float, trigger_on_mono: float | None,
                              last_seen: dict, timeout_sec: float) -> float | None:
    """Decide whether data acquisition has completely stopped (issue #248).

    ``last_seen`` is the dropout watchdog's per-camera arrival map
    (``_camera_last_seen``): its newest value is the last time a frame
    arrived from ANY camera. ``trigger_on_mono`` (the current trigger-ON
    edge) is the floor for the silence measurement: it covers the case
    where no camera ever delivered a frame, and restarts the clock if the
    trigger re-opens after a pause.

    Returns the stall duration in seconds when EVERY camera has been silent
    for longer than ``timeout_sec``, else None. A non-positive
    ``timeout_sec`` disables the check; a None ``trigger_on_mono`` means no
    ON edge yet, so there is no baseline to measure silence against.
    """
    if timeout_sec <= 0 or trigger_on_mono is None:
        return None
    newest = max(last_seen.values(), default=trigger_on_mono)
    stalled = now_mono - max(newest, trigger_on_mono)
    return stalled if stalled > timeout_sec else None


def _abort_scan(conn, code: str, message: str, detail: str, dismiss_tag: str = "") -> None:
    """End the running scan on a fault: the shared tail of the E-303 / E-304 /
    E-306 paths. Main thread only.

    Sets the one-shot guard, dismisses the per-camera 'connection lost'
    toasts (the modal supersedes them, issue #489) plus ``dismiss_tag``,
    writes the capture-log line, records the cause for the scan_ended audit
    event (#535), raises the critical modal, then stopCapture(): that cancels
    the SDK scan, and the pipeline's completion sink finalizes the data and
    emits captureFinished exactly like a user Stop.

    A function, not a method, because the tests drive each abort path
    unbound on a fake that carries only the attributes read here.
    """
    conn._scan_abort_notified = True
    conn._dismiss_dropout_toasts()
    if dismiss_tag:
        conn.notificationDismissByTagRequested.emit(dismiss_tag)
    logger.error(message)
    conn.captureLog.emit(message)
    conn._note_scan_abort(code, detail)
    conn._raise_critical(code, detail=detail)
    conn.stopCapture()


class ScanMixin:
    """Scan lifecycle: start/stop, watchdogs, abort paths, trigger clock, notes."""

    def _setup_scan(self, cfg: dict) -> None:
        # One threshold, compiled in config/app_config.py (#702): no
        # literal fallback here to drift from it.
        self._camera_temp_alert_threshold_c = float(cfg.get(
            "cameraTempAlertThresholdC", _COMPILED_APP_CONFIG["cameraTempAlertThresholdC"]))
        self._camera_dropout_threshold_sec = float(cfg.get("cameraDropoutThresholdSec", 2.0))
        # Whole-scan data-stall watchdog (issue #248): if NO selected camera
        # delivers a frame for this long while the trigger is ON, the scan is
        # aborted with E-303 instead of running to completion on air. Must be
        # comfortably above cameraDropoutThresholdSec (per-camera toast) and
        # the sensor warmup window. <= 0 disables the abort.
        self._scan_data_stall_timeout_sec = float(cfg.get("scanDataStallTimeoutSec", 3.0))
        # Low-storage checks (issue #506), MB free on the data drive. Under
        # minFreeDiskMb: critical error at startup (E-107) and on Start
        # (E-305), one warning toast per running scan. Under
        # scanStopFreeDiskMb: the running scan is stopped (E-306 modal).
        # <= 0 disables the respective checks.
        self._min_free_disk_mb = float(cfg.get("minFreeDiskMb", 1024))
        self._scan_stop_free_disk_mb = float(cfg.get("scanStopFreeDiskMb", 100))
        self._reset_scan_state("", set())
        self._trigger_clock_reset()
        # 1 Hz watchdog timer, started/stopped around the scan lifecycle.
        self._dropout_timer = QTimer(self)
        self._dropout_timer.setInterval(1000)
        self._dropout_timer.timeout.connect(self._on_dropout_check)

        self._config_running = False
        self._capture_stop = threading.Event()
        self._capture_running = False
        self._capture_start_time = 0.0
        # True after scheduling cancel-due-to-safety; cleared when capture ends.
        self._safety_cancel_scheduled = False
        self._scan_notes = ""
        # session_label of the just-completed scan's DB session: notes edits
        # persist there (sessions.session_notes); empty until a scan completes.
        self._scan_notes_session_label = ""
        self._user_label = self.generate_user_label()
        logger.info(f"[Connector] Generated user label: {self._user_label}")

    def _reset_scan_state(self, subject_id: str, active_sides: set) -> None:
        """Fresh per-scan watchdog / abort bookkeeping (construction and every scan start)."""
        # Dropout watchdog. _camera_last_seen is refreshed on every FRAME
        # ARRIVAL (see _LivePlotSink), so _camera_dropped means "frames
        # stopped arriving": a covered camera streaming unlit frames is NOT a
        # dropout. Dropped cameras re-arm when frames resume, so the set holds
        # the currently-silent cameras, not a per-scan record.
        self._camera_last_seen: dict[tuple[str, int], float] = {}
        self._camera_last_temp: dict[tuple[str, int], float] = {}
        self._camera_dropped: set[tuple[str, int]] = set()
        # One-shot guard shared by the scan-ending abort modals (E-303 stall
        # #248, E-304 unplug #387, E-306 low storage #506): whichever fires
        # first wins, so two can't stack.
        self._scan_abort_notified = False
        # Termination cause of this scan for the scan_ended audit event
        # (issue #535): set by _note_scan_abort, None when the scan ran to
        # completion or the operator stopped it.
        self._scan_abort_code: Optional[str] = None
        self._scan_abort_reason = ""
        # Subject label, for the E-301 scan_ended record (that path never
        # reaches the completion handler that has it).
        self._capture_subject_id = subject_id
        # Devices participating in this scan: the console plus any sensor
        # with a non-zero camera mask, so unplugging an idle / masked-out
        # sensor doesn't abort a good scan (issue #387).
        self._capture_active_sides = active_sides
        # Next monotonic time the running scan re-checks free space, and
        # whether this scan has shown the low-storage toast (#506).
        self._storage_next_check_mono = 0.0
        self._storage_warned = False

    # Worker -> main: the SDK scan worker aborted after start_scan returned
    # True (issue #213); the main-thread slot raises E-301 and tears down.
    scanWorkerFailedDuringCapture = pyqtSignal(str)
    userLabelChanged = pyqtSignal()
    # Worker -> main: interrupted/empty-scan outcome toast (message, severity),
    # emitted by _on_pipeline_complete on the pipeline worker thread.
    _scanOutcomeWarningSignal = pyqtSignal(str, str)
    configProgress = pyqtSignal(int)
    configLog = pyqtSignal(str)
    configFinished = pyqtSignal(bool, str)
    captureProgress = pyqtSignal(int)  # 0..100
    captureLog = pyqtSignal(str)
    captureFinished = pyqtSignal(bool, str, str, str)  # ok, error, leftPath, rightPath
    scanNotesChanged = pyqtSignal()
    # Fires once at the end of the completion handler for a scan that finished
    # normally or was cancelled: the only moment scanNotes is guaranteed to
    # hold the just-completed scan's duration line, so the UI opens the notes
    # modal on it. Not emitted on hard errors.
    scanNotesReady = pyqtSignal()
    cameraDropoutDetected = pyqtSignal(str, int, str)  # side, cam_id (0-7), elapsed HH:MM:SS
    # Frames resumed for a camera previously flagged Connection Lost.
    cameraDropoutRecovered = pyqtSignal(str, int, str)  # side, cam_id (0-7), elapsed HH:MM:SS

    def getUserLabel(self) -> str:
        """The operator-editable subject label."""
        return self._user_label

    def setUserLabel(self, value: str):
        """Normalize to "ow" + uppercase alphanumerics; empty input is ignored."""
        if not value:
            return
        rest = value[2:] if value.startswith("ow") else value
        new_val = "ow" + "".join(ch for ch in rest.upper() if ch.isalnum())
        if new_val != self._user_label:
            self._user_label = new_val
            self.userLabelChanged.emit()

    userLabel = pyqtProperty(str, fget=getUserLabel, fset=setUserLabel, notify=userLabelChanged)

    def generate_user_label(self) -> str:
        """A random "ow" + 6 alphanumerics subject label."""
        return "ow" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))

    @pyqtSlot()
    def stopCapture(self):
        """Stop capture (Cancel button, app close, or a scan abort): cancel the SDK scan."""
        if self._capture_running:
            self.captureLog.emit("Stop requested.")
        self._dropout_timer.stop()
        self._capture_stop.set()
        try:
            if self._interface:
                self._interface.cancel_scan()
            self._trigger_state = "OFF"
            self.triggerStateChanged.emit()
        except Exception as e:
            logger.warning("Error stopping trigger: %s", e)

    # --- SCAN NOTES ---

    @pyqtProperty(str, notify=scanNotesChanged)
    def scanNotes(self):
        """The notes buffer of the current / just-completed scan."""
        return self._scan_notes

    @scanNotes.setter
    def scanNotes(self, value: str):
        value = value or ""
        if value != self._scan_notes:
            self._scan_notes = value
            self.scanNotesChanged.emit()
        # Always persist when a scan's DB session exists, even if the
        # in-memory value didn't change (covers the first save after capture).
        if self._scan_notes_session_label:
            self._persist_scan_notes(self._scan_notes_session_label)

    def _persist_scan_notes(self, session_label: str,
                            actual_duration_sec: float | None = None) -> bool:
        """Write the notes buffer to sessions.session_notes of the scan DB
        session with this label (ScanDBSink creates it at scan start).
        Returns True on success.

        ``actual_duration_sec`` is merged into session_meta so History shows
        the trigger-ON scan time rather than the pipeline wall-clock (issue
        #335). The completion sink runs after the SDK's ScanDBSink finalised
        the session, so this read-modify-write is race-free."""
        if not session_label:
            return False
        try:
            with scan_db(self._interface) as db:
                if db is None:
                    return False
                session = db.get_session_by_label(session_label)
                if session is None:
                    logger.error("Scan notes: no DB session with label %r", session_label)
                    return False
                updates: dict = {"session_notes": self._scan_notes.strip()}
                if actual_duration_sec is not None:
                    meta = _sdk_meta_dict(session)
                    meta["actual_duration_sec"] = float(actual_duration_sec)
                    updates["session_meta"] = meta
                db.update_session(session["id"], **updates)
                logger.info("Scan notes saved to DB session %r", session_label)
                return True
        except Exception:
            logger.exception("Failed to save scan notes to DB (label %r)", session_label)
            return False

    @pyqtSlot(str, str)
    def _on_scan_outcome_warning(self, message: str, severity: str) -> None:
        """GUI thread: surface an interrupted/empty-scan toast. Errors are
        sticky (duration_ms=0) so a 'not saved' scan can't be missed."""
        duration = 0 if severity == "error" else 8000
        self.notify(message, severity, duration_ms=duration, tag="scan-outcome")

    # --- START CAPTURE ---

    @pyqtSlot(str, int, int, int, bool, result=bool)
    def startCapture(self, subject_id: str, duration_sec: int, left_camera_mask: int,
                     right_camera_mask: int, disable_laser: bool) -> bool:
        """Start capture asynchronously; returns True if kicked off."""
        logger.info(
            f"startCapture(subject_id={subject_id}, dur={duration_sec}s, "
            f"left_mask=0x{left_camera_mask:02X}, right_mask=0x{right_camera_mask:02X}, "
            f"disable_laser={disable_laser})"
        )
        # Issue #352: a disconnected side's mask is recorded as 0x00, not the
        # UI's pending selection. The SDK persists the ScanRequest masks
        # verbatim into session_meta.sdk_flags, which History and the replay
        # grid trust (#175). The UI keeps the pending selection (it applies if
        # the sensor is plugged in before scanning, #127), so the gate lives
        # here, mirroring runContactQualityCheck and calibration/test.
        left_camera_mask = int(left_camera_mask) if self._leftSensorConnected else 0x00
        right_camera_mask = int(right_camera_mask) if self._rightSensorConnected else 0x00
        if duration_sec <= 0:
            logger.warning("duration_sec was %s, clamping to 3600", duration_sec)
            duration_sec = 3600
        if not self._capture_preconditions_ok():
            return False

        self._capture_stop = threading.Event()
        self._scan_notes = ""
        self._scan_notes_session_label = ""
        self.scanNotesChanged.emit()
        self._capture_running = True
        self._capture_start_time = time.time()
        self._audit.log("scan_started", {
            "label": subject_id, "left_mask": left_camera_mask, "right_mask": right_camera_mask})
        live_source = self._new_live_source()
        masks = (("left", left_camera_mask), ("right", right_camera_mask))
        self._reset_scan_state(subject_id, {"console"} | {side for side, mask in masks if mask})
        self._dropout_timer.start()
        self._trigger_clock_reset()

        # This scan's delivery-gap tracker and outcome tally are bound into
        # the completion handler (not read back off self), so the handler and
        # the sinks provably share THIS scan's instances.
        nan_gap_tracker = NanGapTracker()
        outcome_sink = _ScanOutcomeSink()
        on_complete = functools.partial(
            self._on_pipeline_complete, subject_id, disable_laser, nan_gap_tracker, outcome_sink)
        cq_config = self._scan_cq_config()
        # Engineering-mode alternative camera + laser settings (#446/#449),
        # shared with the preflight signal-quality check (#510).
        self._sync_alt_scan_settings(left_camera_mask, right_camera_mask)
        req = self._build_scan_request(
            subject_id, duration_sec, left_camera_mask, right_camera_mask, disable_laser,
            sinks=[
                _LivePlotSink(connector=self, live_source=live_source,
                              nan_gap_tracker=nan_gap_tracker),
                _TriggerStateSink(connector=self),
                outcome_sink,
                _CompletionSink(on_complete_cb=on_complete),
                # Live mid-scan contact-quality warnings (issue #364). Not
                # `critical`: a contact fault must never abort a clinical scan.
                ContactQualityMonitor(on_transition=self._on_cq_transition, **cq_config),
            ])

        if not self._interface.start_scan(req):
            self._on_scan_refused()
            return False
        logger.info(
            "=== Full scan started: subject=%s duration=%ss left=0x%02X right=0x%02X laser=%s ===",
            subject_id, duration_sec, left_camera_mask, right_camera_mask,
            "off" if disable_laser else "on")
        if self._sensor_debug_logging:
            # DEBUG_FLAG_USB_PRINTF raises the camera-dropout risk on firmware
            # without sensor-fw#115: the queue-full diagnostic goes out over
            # COMM, and if the host stops draining COMM that send busy-waits
            # the firmware main loop, starving the 25 ms camera DMA re-arm.
            # WARNING so such a scan is identifiable after the fact.
            logger.warning(
                "Scan started with sensor debug logging ENABLED "
                "(DEBUG_FLAG_USB_PRINTF) — elevated camera-dropout risk; "
                "not recommended for clinical acquisition")
        # Bind the live source's DB tail to THIS scan's session row by its
        # exact label (set synchronously inside start_scan), so a later
        # pan-into-past resolves the right session, not the newest one.
        label = getattr(self._scan_workflow, "current_scan_label", None)
        if label:
            live_source.set_scan_label(label)
        return True

    def _capture_preconditions_ok(self) -> bool:
        """The startCapture refusals, each with its own capture-log line."""
        if self._capture_running:
            self.captureLog.emit("Capture already running.")
            return False
        if self._safetyFailure:
            self.captureLog.emit("Scan cannot start: laser safety system is tripped. "
                                 "Clear the safety interlock first.")
            return False
        err = self._ensure_idle()
        if err is not None:
            logger.warning("startCapture refused: %s", err)
            self.captureLog.emit(err)
            return False
        try:
            os.makedirs(self._data_root, exist_ok=True)
        except Exception as e:
            self.captureLog.emit(f"Failed to create data dir: {e}")
            return False
        # Backstop for the Start-button check (checkStorageForScan): space
        # can run out between the click and here (clinical pre-scan check).
        return self.checkStorageForScan()

    def _new_live_source(self) -> LiveScanSource:
        """Create this scan's LiveScanSource, bind the viewer to it and retire the last one."""
        # In-memory cache before ring-trim to the DB tail: 30 min at 40 Hz by
        # default; shrink liveCacheMaxSeconds to exercise the DB lazy-load.
        cache_sec = self._app_config.get("liveCacheMaxSeconds", 1800)
        # BVI display low-pass (#228, #552): the compiled cutoff, gated by the
        # research-only Settings switch. Applied at live ingest only; scans.db
        # / CSVs / replay stay raw.
        bvi_lpf_cutoff = self._bvi_lpf_cutoff_hz()
        logger.info("BVI display low-pass: %s", self._describe_bvi_lpf(bvi_lpf_cutoff))
        live_source = LiveScanSource(
            # Scan-start anchor only: samples carry SDK-normalized timestamps
            # (t=0 at the first frame, seconds after this line; issue #284).
            plot_t0=time.monotonic(),
            parent=self,
            # Lets the source lazy-load older samples from the DB tail when
            # the user pans before the ring-trim window. None-safe.
            scan_db_path=getattr(self._interface, "scan_db_path", None),
            cache_max_samples=max(2, int(float(cache_sec) * 40)),
            bvi_lpf_cutoff_hz=bvi_lpf_cutoff,
        )
        # The live source is tracked separately from the current source so
        # the user can open a past scan and come "Back to live". The previous
        # scan's source is retired last; release() is idempotent, so it does
        # not matter if _set_current_scan_source already retired it.
        prev_live = self._live_scan_source
        self._live_scan_source = live_source
        if prev_live is None:
            self.liveSourceAvailableChanged.emit()
        self._set_current_scan_source(live_source)
        self._retire_scan_source(prev_live)
        return live_source

    def _scan_cq_int(self, key: str, default: int) -> int:
        """int(app_config[key]), or ``default`` for a missing, null or
        malformed value. `is not None` rather than `value or default`, so an
        explicit 0 stays 0 (ContactQualityMonitor's own max(1, ...) clamp
        turns that into "no debounce")."""
        value = self._app_config.get(key)
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            logger.warning("Invalid %s config value %r; using default %s", key, value, default)
            return default

    def _scan_cq_config(self) -> dict:
        """ContactQualityMonitor settings for the live monitor sink (issue #364).

        Only a hand-edited config can hold a bad value, but a malformed one
        (wrong type, null element, null scalar) must degrade to the shipped
        defaults rather than raise on the clinical scan path."""
        cfg = self._app_config
        defaults = ([CQ_DEFAULT_DARK_THRESHOLD_DN] * 8, [CQ_DEFAULT_LIGHT_THRESHOLD_DN] * 8)
        try:
            thresholds = CQThresholds.from_sequences(
                cfg.get("cq_dark_threshold_per_camera") or defaults[0],
                cfg.get("cq_light_threshold_per_camera") or defaults[1])
        except Exception as exc:
            logger.warning(
                "Invalid cq_dark_threshold_per_camera / cq_light_threshold_per_camera "
                "config (%s); falling back to default CQ thresholds", exc)
            thresholds = CQThresholds.from_sequences(*defaults)
        return {
            "thresholds": thresholds,
            "rolling_window": self._scan_cq_int(
                "cq_rolling_avg_window", CQ_DEFAULT_ROLLING_WINDOW),
            # Asymmetric debounce (#364): RAISE the warning fast, CLEAR it slow.
            "light_activate_debounce": self._scan_cq_int(
                "cq_live_activate_frames", CQ_DEFAULT_LIVE_ACTIVATE_FRAMES),
            "light_clear_debounce": self._scan_cq_int(
                "cq_live_clear_frames", CQ_DEFAULT_LIVE_CLEAR_FRAMES),
        }

    def _build_scan_request(self, subject_id, duration_sec, left_mask, right_mask,
                            disable_laser, sinks) -> ScanRequest:
        """The ScanRequest for a full scan, with the mode-gated CSV outputs."""
        cfg = self._app_config
        engineering_mode = cfg.get("engineeringMode", False)
        # Raw histogram CSVs are research data (#234): allowed on a
        # non-clinical build or with engineeringMode unlocked, never on a
        # plain clinical build (#43), even if a prior session left the
        # writeRawCsv toggle on.
        raw_csv_allowed = not cfg.get("clinicalMode", False) or engineering_mode
        return ScanRequest(
            subject_id=subject_id,
            duration_sec=duration_sec,
            left_camera_mask=left_mask,
            right_camera_mask=right_mask,
            disable_laser=disable_laser,
            reduced_mode=cfg.get("clinicalMode", False),
            # Opt-in now that per-cam BFI/BVI lands in the scan DB (the SDK
            # still forces it on if no DB is configured).
            write_corrected_csv=cfg.get("writeCorrectedCsv", False),
            # Issue #43: telemetry CSVs are engineering diagnostics. The SDK
            # defaults this to True, so it is gated here on engineeringMode
            # and (#471) on the persisted opt-in toggle: a stale
            # writeTelemetryCsv=true never writes on a clinical build.
            write_telemetry_csv=engineering_mode and self._write_telemetry_csv,
            # Feeds the pipeline's Tee("raw") gate: None writes the whole
            # scan, 0 omits the raw tee entirely.
            raw_save_max_duration_s=(
                self._raw_csv_duration_sec if (raw_csv_allowed and self._write_raw_csv) else 0),
            sinks=sinks,
            # Async backstop (#213): a worker that aborts after start_scan
            # returned True (e.g. a critical sink dies because the scan DB
            # became unwritable past the preflight) can't ride the return
            # value. Fires on the worker thread, so hop to the main thread
            # via a queued signal: see _on_scan_worker_failed.
            on_error=lambda exc: self.scanWorkerFailedDuringCapture.emit(str(exc)),
        )

    def _on_scan_refused(self) -> None:
        """start_scan returned False: undo the scan-start state and raise E-301 / E-302."""
        self._capture_running = False
        # Unbind the viewer. The orphaned LiveScanSource stays held as
        # _live_scan_source (it is parented to the connector) until the next
        # startCapture retires it.
        self._set_current_scan_source(None)
        # start_scan refuses for two reasons: a prior worker still alive, or a
        # pre-flight failure (e.g. the scan DB couldn't be opened, so the scan
        # is aborted before the laser fires rather than silently losing its
        # data). last_scan_error carries the reason for the latter. Logged at
        # WARNING because captureLog only reaches QML console.log, which the
        # run log filters out by default.
        reason = getattr(self._scan_workflow, "last_scan_error", None)
        if reason:
            logger.warning("startCapture aborted: %s", reason)
            self.captureLog.emit(f"Scan aborted: {reason}")
            self.errorOccurred.emit(reason)
            self._raise_critical("E-301", detail=reason)
        else:
            logger.warning("startCapture aborted: SDK refused to spawn a new scan "
                           "(usually a previous worker thread that didn't exit cleanly).")
            self.captureLog.emit("Capture already running.")
            self._raise_critical("E-302")

    # --- SCAN END ---

    def _capture_duration_s(self) -> Optional[float]:
        """Wall-clock seconds since startCapture, or None when no scan has started."""
        if not self._capture_start_time:
            return None
        return time.time() - self._capture_start_time

    def _on_pipeline_complete(self, subject_id: str, disable_laser: bool,
                              nan_gap_tracker: NanGapTracker, outcome_sink: _ScanOutcomeSink,
                              meta) -> None:
        """End-of-scan handler, run by _CompletionSink.on_complete() on the
        pipeline worker thread once the ScanRunner finishes. The leading
        arguments are bound per scan by startCapture."""
        canceled = self._capture_stop.is_set()
        self.captureLog.emit("Scan stopped." if canceled else "Capture session complete.")

        # Notes report the trigger-ON (laser-on) time, not wall-clock, which
        # includes pre-scan setup and post-scan USB drain. Wall-clock is the
        # fallback when no TriggerStateEvent was ever seen (e.g. a cancel
        # before the trigger fired).
        if self._trigger_on_mono is not None:
            # No OFF event and no disconnect-close reached us: close as a
            # last resort, knowing the measurement includes teardown time.
            logger.warning(
                "Scan completed with the trigger clock still open "
                "(no TriggerStateEvent OFF received); duration may include teardown time.")
            self._trigger_clock_close()
        if self._trigger_cumulative_s > 0:
            elapsed = self._trigger_cumulative_s
        else:
            elapsed = time.time() - self._capture_start_time
        duration_str = hms(elapsed)
        status = "stopped" if canceled else "completed"
        logger.info("=== Full scan ended: %s — duration %s ===", status, duration_str)
        self._scan_notes = (
            self._scan_notes.strip() + f"\n---\nScan {status} — duration: {duration_str}")
        # Data-gap footer: ranges where any camera went >1 s without a frame.
        # Fail-soft: a tracker bug must never block notes persistence.
        try:
            gap_line = gap_note_line(nan_gap_tracker)
            if gap_line:
                logger.warning("Scan data gaps detected: %s", gap_line.strip())
                self._scan_notes += gap_line
        except Exception:
            logger.exception("NaN-gap footer computation failed")
        self.scanNotesChanged.emit()

        # Persist notes to the DB session ScanDBSink created at scan start
        # (label = scan_id_subject_id); later Notes-modal edits update the
        # same row through the scanNotes setter. The trigger-ON duration is
        # stamped alongside for History (issue #335).
        scan_id = getattr(meta, "scan_id", "") if meta else ""
        session_label = f"{scan_id}_{subject_id}" if scan_id else ""
        self._scan_notes_session_label = session_label
        self._persist_scan_notes(session_label, actual_duration_sec=elapsed)

        # An interrupted scan loses its open interval; one that ends before
        # any interval closes saves nothing. Surface a toast (ScanDBSink
        # deletes the empty session row; History guards against loading it).
        outcome = None
        try:
            outcome = classify_scan_outcome(
                final_frames=outcome_sink.final_frames,
                terminal_dark_missing=outcome_sink.terminal_dark_missing,
                canceled=canceled, disable_laser=disable_laser)
            if outcome.severity:
                logger.warning("Scan outcome: %s — %s", outcome.kind, outcome.message)
                self._scanOutcomeWarningSignal.emit(outcome.message, outcome.severity)
        except Exception:
            logger.exception("scan-outcome classification failed")

        self._capture_running = False
        self._safety_cancel_scheduled = False
        # Termination cause + data verdict (issue #535): a fault-aborted scan
        # is recorded as "aborted" with its error code, never as an operator
        # stop.
        self._log_scan_ended_audit(
            subject_id, session_label, self._capture_duration_s(), outcome, canceled)
        # The pipeline writes its own output, so the two paths are empty.
        self.captureFinished.emit(True, "", "", "")
        self._emit_or_defer_scan_notes()

    @pyqtSlot(str)
    def _on_scan_worker_failed(self, detail: str):
        """Main-thread handler for an async scan-worker abort (#213).

        The SDK worker invokes ScanRequest.on_error after start_scan already
        returned True, typically a critical sink (ScanDBSink) dying because
        the scan DB became unwritable past the synchronous preflight. Surface
        E-301 and tear the capture down so the app doesn't sit 'capturing'
        forever with a dead worker.
        """
        if not self._capture_running:
            return  # already torn down (e.g. user cancelled first)
        logger.warning("scan worker aborted mid-capture: %s", detail)
        self.captureLog.emit(f"Scan aborted: {detail}")
        self._note_scan_abort("E-301", detail)
        self._raise_critical("E-301", detail=detail)
        # Stop the dropout watchdog and cancel the (dead) scan, then clear
        # running state and unblock QML, mirroring the completion path.
        self.stopCapture()
        self._capture_running = False
        self._safety_cancel_scheduled = False
        self._set_current_scan_source(None)
        # The pipeline never completed, so _on_pipeline_complete will not
        # record scan_ended for this scan: record the abort here (issue #535).
        # No double-log risk: when the runner does reach the completion sink
        # it does so synchronously on the worker BEFORE on_error fires, and
        # that handler clears _capture_running, which the guard above honours.
        self._log_scan_ended_audit(
            self._capture_subject_id, "", self._capture_duration_s(), None, True)
        self.captureFinished.emit(False, detail, "", "")

    def _note_scan_abort(self, code: str, detail: str = "") -> None:
        """Record that a fault is ending the current scan (issue #535).

        Called from every scan-ending fault path (E-303 stall, E-304 device
        disconnect, E-306 low storage, E-202 safety trip, E-301 worker death)
        right before the critical modal is raised and the scan is cancelled,
        so the scan_ended audit event names the cause instead of reporting
        the cancel as an operator stop. First fault wins: a disconnect that
        also trips the stall watchdog keeps its own code.
        """
        if self._scan_abort_code:
            logger.info("Scan abort %s (%s) after %s already recorded — keeping the first",
                        code, detail, self._scan_abort_code)
            return
        self._scan_abort_code = code
        self._scan_abort_reason = detail or ""

    def _log_scan_ended_audit(self, subject_id: str, session_label: str,
                              duration_s: Optional[float], outcome: Optional[ScanOutcome],
                              canceled: bool) -> None:
        """Append the ``scan_ended`` audit event (issue #535). Never raises."""
        try:
            self._audit.log("scan_ended", {
                "label": subject_id,
                "session_label": session_label or None,
                "duration_s": round(duration_s, 1) if duration_s is not None else None,
                **audit_scan_ended_details(
                    outcome=outcome, canceled=canceled, abort_code=self._scan_abort_code,
                    abort_reason=self._scan_abort_reason),
            })
        except Exception:
            logger.warning("scan_ended audit log failed", exc_info=True)

    def _emit_or_defer_scan_notes(self) -> None:
        """Open the end-of-scan notes modal, or defer it while a critical-error
        modal is on screen (issue #387).

        On an E-304 / E-303 / E-202 abort the critical modal is raised, then
        the scan unwinds for a second or two before the completion handler
        runs. Opening the notes modal then would pop it UNDER the error modal
        and steal keyboard focus, so the open is held until
        criticalErrorsDismissed() flushes it.

        Called on the pipeline worker thread. The re-check after setting the
        pending flag closes the race where the modal is dismissed between the
        check and the set.
        """
        if not self._critical_modal_active:
            self.scanNotesReady.emit()
            return
        self._scan_notes_pending = True
        if not self._critical_modal_active:
            self._scan_notes_pending = False
            self.scanNotesReady.emit()

    # --- PIPELINE GATES ---

    def _ensure_idle(self) -> str | None:
        """Gate for pipeline-starting slots (capture / configure / check)."""
        # Issue #303: the connect-time bring-up runs async for a few seconds
        # after the handles report READY. A capture/CQ/configure started in
        # that window collides with it: on a sensor, "Failed to program FPGA"
        # and a camera wedged until power-cycle; on the console, a scan before
        # the laser registers are loaded. The UI start gate polls
        # isPipelineIdle and defers past this.
        if self.deviceInitBusy:
            return "The system is still initializing — wait a few seconds and try again"
        if self._cq_quick_running:
            return "Contact-quality check already in progress"
        if self._capture_running:
            return "Scan already running"
        if self._config_running:
            return "Camera configuration already in progress"
        workflow = getattr(self, "_scan_workflow", None)
        if getattr(workflow, "running", False):
            return "Scan already running"
        if getattr(workflow, "config_running", False):
            return "Camera configuration already in progress"
        return None

    @pyqtSlot(result=bool)
    def isPipelineIdle(self) -> bool:
        """QML probe: True when a pipeline-starting slot called right now
        would not be refused by _ensure_idle. The pre-scan CQ check's SDK
        worker keeps unwinding for ~2 s after results are displayed, so
        BloodFlow's scan-start gate polls this instead of firing blind."""
        return self._ensure_idle() is None

    @pyqtSlot(result=bool)
    def isConfigInFlight(self) -> bool:
        """QML probe: True while a camera-sensor configuration (FPGA flash)
        is in flight or still draining after a cancel. A config holds the
        pipeline for ~50 s, so BloodFlow's start gate (issue #283) stretches
        its wait deadline to flash timescale while this is True."""
        workflow = getattr(self, "_scan_workflow", None)
        return bool(self._config_running or getattr(workflow, "config_running", False))

    # --- WATCHDOG (1 Hz while a scan runs) ---

    @pyqtSlot()
    def _on_dropout_check(self):
        """1 Hz watchdog: low storage, per-camera dropouts, all-camera stall."""
        if not self._capture_running:
            return
        # Low-storage stop (issue #506). Ahead of the trigger-state gate: the
        # pipeline writes scans.db for the whole capture, not only while the
        # trigger is ON.
        if (not self._scan_abort_notified and self._storage_check_due()
                and self._check_scan_storage()):
            return
        # Trigger OFF: _capture_running only flips false after the post-scan
        # cleanup, so for 2+ s after trigger-stop frames have legitimately
        # stopped; without this gate every natural scan end fires a spurious
        # 'connection lost' toast on every active camera.
        if self._trigger_state != "ON":
            return
        # Once a scan-ending abort has fired, the critical modal IS the
        # notification: a system-wide unplug would otherwise bury it under up
        # to 16 per-camera cards (issue #489).
        if self._scan_abort_notified:
            return
        now = time.monotonic()
        threshold = self._camera_dropout_threshold_sec
        for key, last_t in list(self._camera_last_seen.items()):
            if key in self._camera_dropped or now - last_t <= threshold:
                continue
            side, cam_id = key
            name = f"Camera {side.upper()} {cam_id + 1}"
            temp = self._camera_last_temp.get(key, float("nan"))
            elapsed_str = self._scan_elapsed_str()
            logger.warning(f"[{elapsed_str}] {name} dropout detected (no frames for "
                           f">{threshold:.0f} s). Last temperature: {temp:.1f}°C")
            self.notify(
                f"{name} connection lost at {elapsed_str} — last temp {temp:.1f}°C",
                type_="warning", duration_ms=30000, tag=f"dropout_{side}_{cam_id}")
            self._camera_dropped.add(key)
            self.cameraDropoutDetected.emit(side, cam_id, elapsed_str)
            # Dropout bar in the PlotViewer. It must sit on the SAME time axis
            # as the plotted samples (SDK-normalized, t=0 at the scan's first
            # frame; issue #284), so anchor it at the camera's own newest
            # sample: exactly where its trace stops. NaN (a camera that never
            # produced a plottable sample) means there is no trace to mark.
            src = self._current_scan_source
            if src is not None and getattr(src, "live", False):
                drop_t = src.last_sample_t(side, cam_id)
                if math.isfinite(drop_t):
                    src.mark_dropped(side=side, cam_id=cam_id, t=drop_t)

        # All-camera stall (issue #248). Per-camera dropouts are fail-soft
        # (the scan keeps running on the remaining cameras); total data loss
        # is not: abort instead of counting down to a hollow "complete".
        stalled_s = _scan_data_stall_decision(
            now, self._trigger_on_mono, self._camera_last_seen,
            self._scan_data_stall_timeout_sec)
        if stalled_s is not None:
            self._abort_scan_data_stall(stalled_s)

    def _dismiss_dropout_toasts(self, side: str | None = None) -> None:
        """Dismiss per-camera 'connection lost' toasts (issue #489).

        With ``side``: that sensor's 8 possible camera tags, on a sensor
        disconnect whose device-level notification subsumes its cameras'
        cards. Without: every camera the current scan flagged dropped, for
        the scan-abort paths. Dismissing a tag with no toast is a no-op.
        """
        keys = sorted(self._camera_dropped) if side is None else [(side, c) for c in range(8)]
        if not keys:
            return
        tags = [f"dropout_{s}_{cam_id}" for s, cam_id in keys]
        # One summary line: dismissNotification's per-call INFO would print 16
        # mostly-no-op lines on a system unplug.
        logger.info("Dismissing per-camera dropout toasts: %s", ", ".join(tags))
        for tag in tags:
            self.notificationDismissByTagRequested.emit(tag)

    def _on_camera_dropout_recovered(self, side: str, cam_id: int) -> None:
        """Frames resumed for a camera flagged Connection Lost: surface it.

        Called from _LivePlotSink on the pipeline runner thread (signal emits
        and notify() are thread-safe). Reuses the dropout toast's tag so the
        "connection lost" toast is replaced in place.
        """
        elapsed_str = self._scan_elapsed_str()
        name = f"Camera {side.upper()} {cam_id + 1}"
        self.notify(f"{name} reconnected at {elapsed_str}",
                    type_="success", duration_ms=8000, tag=f"dropout_{side}_{cam_id}")
        self.captureLog.emit(
            f"[{elapsed_str}] {name} frames resumed — dropout watchdog re-armed.")
        self.cameraDropoutRecovered.emit(side, cam_id, elapsed_str)

    # --- SCAN ABORTS (see _abort_scan) ---

    def _abort_scan_data_stall(self, stalled_s: float) -> None:
        """E-303: data acquisition has completely stopped (issue #248).
        From the 1 Hz watchdog."""
        elapsed_str = self._scan_elapsed_str()
        _abort_scan(
            self, "E-303",
            f"[{elapsed_str}] No data from any camera for "
            f">{self._scan_data_stall_timeout_sec:.0f} s — data acquisition has stopped. "
            f"Stopping the scan; data captured before the loss is saved.",
            f"no camera frames for {stalled_s:.0f} s (scan elapsed {elapsed_str})")

    def _stop_scan_low_storage(self, free: Optional[int]) -> None:
        """E-306: the data drive is nearly full (issue #506). The pipeline
        finalizes scans.db in the space the threshold kept free; the modal
        supersedes the earlier "running low" toast."""
        elapsed_str = self._scan_elapsed_str()
        free_mb = (free or 0) / disk_space.MB
        _abort_scan(
            self, "E-306",
            f"[{elapsed_str}] Data drive almost full ({free_mb:.0f} MB "
            f"free, stop threshold {self._scan_stop_free_disk_mb:.0f} MB). "
            f"Stopping the scan; data captured so far is saved.",
            f"{self._storage_detail(free)}; scan elapsed {elapsed_str}",
            dismiss_tag=_LOW_STORAGE_TOAST_TAG)

    def _abort_scan_device_disconnect(self, name: str) -> None:
        """E-304: a participating device was unplugged mid-scan (issue #387).
        From the connection handler. One-shot: a disconnect after a stall, or
        a second device dropping, must not stack a second modal. The SDK is
        already tearing the scan down; stopCapture() also marks it canceled,
        which suppresses the redundant end-of-scan outcome toast."""
        if self._scan_abort_notified:
            return
        elapsed_str = self._scan_elapsed_str()
        _abort_scan(
            self, "E-304",
            f"[{elapsed_str}] {name} disconnected during the scan — stopping the scan; "
            f"data captured before the disconnection is saved.",
            f"{name} disconnected mid-scan (scan elapsed {elapsed_str})")

    # --- STORAGE (issue #506) ---

    def _storage_detail(self, free: Optional[int]) -> str:
        """The free-space phrase used in the storage error details."""
        return f"{(free or 0) / disk_space.MB:.0f} MB free on the data drive ({self._data_root})"

    def check_startup_storage(self) -> None:
        """Startup free-space check: raise the E-107 critical modal when the
        data drive has under minFreeDiskMb free. Called once by main.py after
        the QML window has loaded."""
        free = disk_space.free_bytes(self._data_root)
        if free is not None:
            logger.info("Startup storage check: %.0f MB free on %s",
                        free / disk_space.MB, self._data_root)
        if disk_space.below(free, self._min_free_disk_mb):
            self._raise_critical("E-107", detail=self._storage_detail(free))

    @pyqtSlot(result=bool)
    def checkStorageForScan(self) -> bool:
        """Start-button free-space check. Returns False, after raising the
        E-305 critical modal, when the data drive has under minFreeDiskMb
        free; the scan (and its pre-scan contact check) must not start. A
        failed free-space query never blocks: the mid-scan stop still
        protects the drive."""
        free = disk_space.free_bytes(self._data_root)
        if not disk_space.below(free, self._min_free_disk_mb):
            return True
        detail = self._storage_detail(free)
        logger.warning("Scan start refused: %s", detail)
        self.captureLog.emit(f"Scan not started: {detail}.")
        self._raise_critical("E-305", detail=detail)
        return False

    def _storage_check_due(self) -> bool:
        """Rate-limit the mid-scan free-space query to one every
        _STORAGE_CHECK_INTERVAL_S; the first watchdog tick of a scan always
        checks."""
        now = time.monotonic()
        if now < self._storage_next_check_mono:
            return False
        self._storage_next_check_mono = now + _STORAGE_CHECK_INTERVAL_S
        return True

    def _check_scan_storage(self) -> bool:
        """Mid-scan free-space check, from the 1 Hz watchdog. Under
        scanStopFreeDiskMb the scan is stopped (returns True); under
        minFreeDiskMb the operator gets one warning toast per scan."""
        free = disk_space.free_bytes(self._data_root)
        if disk_space.below(free, self._scan_stop_free_disk_mb):
            self._stop_scan_low_storage(free)
            return True
        if not self._storage_warned and disk_space.below(free, self._min_free_disk_mb):
            self._storage_warned = True
            free_mb = free / disk_space.MB
            logger.warning(
                "[%s] Data drive below %.0f MB free (%.0f MB) during scan",
                self._scan_elapsed_str(), self._min_free_disk_mb, free_mb)
            self.notify(
                f"Storage is running low: {free_mb:.0f} MB free on the data drive. The scan "
                f"will stop automatically at {self._scan_stop_free_disk_mb:.0f} MB.",
                type_="warning", duration_ms=0, tag=_LOW_STORAGE_TOAST_TAG)
        return False

    # --- TRIGGER-ON CLOCK (issue #201) ---
    # The single scan-time clock behind the notes duration line, the header
    # elapsed counter (scanElapsedSec) and the dropout log timestamps. Opened
    # and closed by _TriggerStateSink from SDK TriggerStateEvents; also closed
    # by the console-disconnect handler, because a dead console can't deliver
    # its OFF event (stop_trigger raises before _emit_trigger_event runs).

    def _trigger_clock_reset(self) -> None:
        """Zero the clock (once per scan, from startCapture)."""
        self._trigger_cumulative_s: float = 0.0
        self._trigger_on_mono: float | None = None
        # Scan-relative timestamp (from the SDK's TriggerStateEvent) of the
        # current ON edge; see _trigger_clock_close.
        self._trigger_on_ts: float | None = None

    def _trigger_clock_open(self, ts_s: float | None) -> None:
        """Start an ON interval. No-op if one is already open."""
        if self._trigger_on_mono is None:
            self._trigger_on_mono = time.monotonic()
            self._trigger_on_ts = ts_s

    def _trigger_clock_close(self, ts_s: float | None = None) -> None:
        """Bank the open ON interval into the cumulative total.

        Prefers the SDK-stamped, scan-relative timestamp pair: the OFF event
        can sit queued behind histogram batches in the runner's diagnostics
        channel and be consumed ~1 s after it was emitted, which inflates a
        receive-time measurement (a 16:00 scan shows as 16:01, issue #201).
        Falls back to host receive-time when the timestamps are unusable
        (the SDK's t0-None path emits 0.0 for both edges, giving a
        non-positive delta) or when closing without an event (console lost).
        No-op when the clock is already closed, so a late OFF event after a
        disconnect-close can't double-count.
        """
        if self._trigger_on_mono is None:
            return
        ts_delta = 0.0
        if ts_s is not None and self._trigger_on_ts is not None:
            ts_delta = ts_s - self._trigger_on_ts
        if ts_delta > 0:
            self._trigger_cumulative_s += ts_delta
        else:
            self._trigger_cumulative_s += time.monotonic() - self._trigger_on_mono
        self._trigger_on_mono = None
        self._trigger_on_ts = None

    def _trigger_elapsed_s(self) -> float:
        """Trigger-ON seconds so far this scan, including any open interval."""
        elapsed = self._trigger_cumulative_s
        if self._trigger_on_mono is not None:
            elapsed += time.monotonic() - self._trigger_on_mono
        return elapsed

    @pyqtSlot(result=int)
    def scanElapsedSec(self) -> int:
        """QML-facing trigger-ON elapsed whole seconds.

        The header counter polls this once a second instead of counting its
        own Timer ticks: a QML Timer fires late under GUI load, and a
        tick-counting clock drifts behind the notes duration line.
        """
        return int(self._trigger_elapsed_s())

    @pyqtSlot(result=str)
    def scanElapsedStr(self) -> str:
        """QML-facing accessor for trigger-ON elapsed time (HH:MM:SS)."""
        return self._scan_elapsed_str()

    def _scan_elapsed_str(self) -> str:
        """Return current scan elapsed trigger-ON time as HH:MM:SS."""
        return hms(self._trigger_elapsed_s())

    # --- CAMERA CONFIGURATION ---

    @pyqtSlot(int, int, result=bool)
    def startConfigureCameraSensors(self, left_camera_mask: int, right_camera_mask: int) -> bool:
        """Start the camera-sensor configuration (FPGA flash); True if kicked off."""
        err = self._ensure_idle()
        if err is not None:
            logger.warning("startConfigureCameraSensors refused: %s", err)
            self.configFinished.emit(False, err)
            return False
        self._config_running = True
        req = ConfigureRequest(
            left_camera_mask=left_camera_mask, right_camera_mask=right_camera_mask,
            power_off_unused_cameras=bool(self._power_off_unused_cameras))
        started = self._interface.start_configure_camera_sensors(
            req,
            on_progress_fn=lambda pct: self.configProgress.emit(int(pct)),
            on_log_fn=lambda msg: self.configLog.emit(msg),
            on_complete_fn=self._on_config_finished)
        if not started:
            self._config_running = False
            self.configFinished.emit(False, "Configuration could not start")
        return bool(started)

    @pyqtSlot()
    def cancelConfigureCameraSensors(self):
        """Cancel an in-flight camera-sensor configuration."""
        if self._config_running:
            self._interface.cancel_configure_camera_sensors()

    def _on_config_finished(self, result):
        # If a disconnect already finalized this run via
        # _on_handle_state_changed, the SDK's late callback must not re-emit
        # configFinished and re-fire the QML flashTask handler.
        if not self._config_running:
            return
        self._config_running = False
        self.configFinished.emit(bool(result.ok), result.error or "")
