"""Scan lifecycle: start/stop, watchdogs, abort paths, trigger clock, notes."""
import math
import os
import threading
import time
from typing import Any, Dict, Optional

import random
import string
from PyQt6.QtCore import QTimer, pyqtProperty, pyqtSignal, pyqtSlot
from connector._common import logger
from omotion.ScanWorkflow import ConfigureRequest, ScanRequest
from omotion.contact_quality import CQThresholds, ContactQualityMonitor
from connector.contact_quality import (
    CQ_DEFAULT_DARK_THRESHOLD_DN, CQ_DEFAULT_LIGHT_THRESHOLD_DN, CQ_DEFAULT_LIVE_ACTIVATE_FRAMES,
    CQ_DEFAULT_LIVE_CLEAR_FRAMES, CQ_DEFAULT_ROLLING_WINDOW,
)
from connector.sinks import (
    ScanOutcome, _CompletionSink, _LivePlotSink, _ScanOutcomeSink, _TriggerStateSink,
    audit_scan_ended_details, classify_scan_outcome,
)
from config.app_config import APP_CONFIG as _COMPILED_APP_CONFIG
from plot.data_sources import LiveScanSource
from plot.nan_gap_tracker import NanGapTracker, gap_note_line
from utils import disk_space
from connector.history import _sdk_meta_dict

# Mid-scan free-space poll period (issue #506). At the worst measured write
# rate (16 cameras with raw CSVs, ~2.6 MB/s) this spends ~13 MB of the
# 100 MB stop threshold between polls.
_STORAGE_CHECK_INTERVAL_S = 5.0

# Toast tag shared by the mid-scan low-storage warning and the stop notice,
# so the stop replaces the warning instead of stacking under it (#506).
_LOW_STORAGE_TOAST_TAG = "low_storage"

def _scan_data_stall_decision(
    now_mono: float,
    trigger_on_mono: float | None,
    last_seen: dict,
    timeout_sec: float,
) -> float | None:
    """Decide whether data acquisition has completely stopped (issue #248).

    ``last_seen`` is the dropout watchdog's per-camera arrival map
    (``_camera_last_seen``) — its newest value is the last time a frame
    arrived from ANY camera. ``trigger_on_mono`` (the current trigger-ON
    edge) is the floor for the silence measurement: it covers the case
    where no camera ever delivered a frame, and restarts the clock if the
    trigger re-opens after a pause.

    Returns the stall duration in seconds when EVERY camera has been
    silent for longer than ``timeout_sec``, else None. A non-positive
    ``timeout_sec`` disables the check; a None ``trigger_on_mono`` means
    the trigger clock isn't open (no ON edge yet), so there's no baseline
    to measure silence against.
    """
    if timeout_sec <= 0 or trigger_on_mono is None:
        return None
    newest = max(last_seen.values(), default=trigger_on_mono)
    stalled = now_mono - max(newest, trigger_on_mono)
    return stalled if stalled > timeout_sec else None


class ScanMixin:
    """Scan lifecycle: start/stop, watchdogs, abort paths, trigger clock, notes."""

    def _setup_scan(self, cfg: dict) -> None:
        # One threshold, compiled in config/app_config.py (#702): no
        # literal fallback here to drift from it.
        self._camera_temp_alert_threshold_c = float(cfg.get(
            "cameraTempAlertThresholdC",
            _COMPILED_APP_CONFIG["cameraTempAlertThresholdC"]))
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
        # Next monotonic time the running scan re-checks free space; the
        # 1 Hz watchdog polls it every _STORAGE_CHECK_INTERVAL_S.
        self._storage_next_check_mono = 0.0
        self._storage_warned = False  # this scan has shown the low-storage toast

        # Camera dropout watchdog state, reset at the start of each scan.
        # _camera_last_seen is refreshed on every FRAME ARRIVAL for the
        # camera (see _LivePlotSink.consume), so membership in
        # _camera_dropped means "frames stopped arriving": a camera
        # streaming unlit frames (covered sensor) is NOT a dropout.
        # Dropped cameras re-arm automatically when frames resume
        # (_rearm_dropped_camera), so the set holds currently-silent
        # cameras, not a permanent per-scan record.
        self._camera_last_seen: dict[tuple[str, int], float] = {}
        self._camera_last_temp: dict[tuple[str, int], float] = {}
        self._camera_dropped: set[tuple[str, int]] = set()
        # Shared one-shot guard for a scan-ending abort modal: set when the
        # all-camera stall fires E-303 (issue #248) OR a participating device
        # is unplugged mid-scan and fires E-304 (issue #387). Whichever fires
        # first wins so the two can't stack two modals. Cleared at the next
        # startCapture.
        self._scan_abort_notified = False
        # Termination cause of the CURRENT scan for the audit trail (issue
        # #535): the E-code + detail of the fault that ended it, or None when
        # it ran to completion / the operator stopped it. Set by
        # _note_scan_abort from every scan-ending fault path, cleared at
        # startCapture, consumed by the scan_ended audit event.
        self._scan_abort_code: Optional[str] = None
        self._scan_abort_reason: str = ""
        # Subject label of the CURRENT scan, for the E-301 scan_ended record
        # (that path never reaches the completion closure that has it).
        self._capture_subject_id: str = ""
        # Devices participating in the CURRENT scan: {"console"} plus any
        # sensor with a non-zero camera mask at scan start. Consulted by the
        # disconnect handler so unplugging an idle / masked-out sensor
        # doesn't abort a good scan (issue #387).
        self._capture_active_sides: set[str] = set()
        # Replaced with a fresh instance at each scan start. Kept on the
        # instance for introspection only; the scan path uses a
        # closure-local binding of the same object.
        self._nan_gap_tracker = NanGapTracker()
        # 1 Hz watchdog timer, started/stopped around the scan lifecycle.
        self._dropout_timer = QTimer(self)
        self._dropout_timer.setInterval(1000)
        self._dropout_timer.timeout.connect(self._on_dropout_check)

        # Trigger-ON clock (issue #201): the single scan-time source of
        # truth for the notes duration line, the header elapsed counter and
        # the dropout log timestamps. Managed exclusively through the
        # _trigger_clock_reset/_open/_close helpers.
        self._trigger_cumulative_s: float = 0.0
        self._trigger_on_mono: float | None = None
        # Scan-relative timestamp (from the SDK's TriggerStateEvent) of the
        # current ON edge. Preferred over _trigger_on_mono for the final
        # duration because it is immune to host-side event-queue latency.
        self._trigger_on_ts: float | None = None

        self._config_running = False
        self._capture_thread = None
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

    scanWorkerFailedDuringCapture = pyqtSignal(str)  # Emitted from the SDK scan worker thread when it aborts after start_scan returned True (main-thread slot raises E-301 & tears down capture). Issue #213.
    userLabelChanged = pyqtSignal()  # Signal to notify QML of user label changes
    # Worker->main: interrupted/empty-scan outcome toast (message, severity).
    # Emitted from _on_pipeline_complete (pipeline worker thread); delivered
    # on the GUI thread via the auto-queued connection wired in connect_signals.
    _scanOutcomeWarningSignal = pyqtSignal(str, str)
    configProgress = pyqtSignal(int)
    configLog = pyqtSignal(str)
    configFinished = pyqtSignal(bool, str)
    # capture signals
    captureProgress = pyqtSignal(int)  # 0..100
    captureLog = pyqtSignal(str)  # log lines
    captureFinished = pyqtSignal(bool, str, str, str)  # ok, error, leftPath, rightPath
    scanNotesChanged = pyqtSignal()
    # Fires once at the end of _on_complete (after the duration line has
    # been appended to _scan_notes and notes.txt has been written) for any
    # scan that finished normally or was canceled by the user. The UI uses
    # this to auto-open the notes modal at the only moment when scanNotes
    # is guaranteed to reflect the just-completed scan. Not emitted on
    # hard errors — those keep the scan dialog visible with the error.
    scanNotesReady = pyqtSignal()
    cameraDropoutDetected = pyqtSignal(str, int, str)  # side ("left"/"right"), cam_id (0-7), elapsed HH:MM:SS
    # Frames resumed for a camera previously flagged Connection Lost — the
    # watchdog was re-armed and display resumed. Mirror of cameraDropoutDetected.
    cameraDropoutRecovered = pyqtSignal(str, int, str)  # side, cam_id (0-7), elapsed HH:MM:SS

    # --- GETTERS/SETTERS FOR Qt PROPERTIES ---
    def getUserLabel(self) -> str:
        return self._user_label

    def setUserLabel(self, value: str):
        if not value:
            return
        # normalize to "ow" + alphanumerics (uppercase)
        if value.startswith("ow"):
            rest = value[2:]
        else:
            rest = value
        rest = "".join(ch for ch in rest.upper() if ch.isalnum())
        new_val = "ow" + rest
        if new_val != self._user_label:
            self._user_label = new_val
            self.userLabelChanged.emit()

    userLabel = pyqtProperty(
        str, fget=getUserLabel, fset=setUserLabel, notify=userLabelChanged
    )

    @pyqtSlot()
    def stopCapture(self):
        """Stop capture (Cancel button or app close). Ceases scan, disables cameras, waits for worker."""
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

        self._capture_thread = None

    @pyqtProperty(str, notify=scanNotesChanged)  # <-- add notify
    def scanNotes(self):
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

    def _persist_scan_notes(
        self, session_label: str, actual_duration_sec: float | None = None
    ) -> bool:
        """Write the in-memory notes buffer to sessions.session_notes of the
        scan DB session whose session_label matches. Returns True on success.
        ScanDBSink (critical) creates the session at scan start, so every scan
        that ran has a row to update.

        When ``actual_duration_sec`` is given, merge it into session_meta as
        ``actual_duration_sec`` so History reports the actual trigger-ON scan
        time rather than the pipeline wall-clock (issue #335). This runs from
        the app's _CompletionSink, which fires *after* the SDK's ScanDBSink has
        finalised session_end + any diagnostics meta, so a read-modify-write
        here is race-free and preserves the existing meta."""
        db_path = getattr(self._interface, "scan_db_path", None)
        if not db_path or not session_label:
            return False
        try:
            from omotion.ScanDatabase import ScanDatabase
            db = ScanDatabase(db_path)
            try:
                session = db.get_session_by_label(session_label)
                if session is None:
                    logger.error(
                        "Scan notes: no DB session with label %r", session_label
                    )
                    return False
                update_kwargs: dict = {
                    "session_notes": self._scan_notes.strip()
                }
                if actual_duration_sec is not None:
                    meta = _sdk_meta_dict(session)
                    meta["actual_duration_sec"] = float(actual_duration_sec)
                    update_kwargs["session_meta"] = meta
                db.update_session(session["id"], **update_kwargs)
                logger.info("Scan notes saved to DB session %r",
                            session_label)
                return True
            finally:
                db.close()
        except Exception:
            logger.exception(
                "Failed to save scan notes to DB (label %r)", session_label
            )
            return False

    @pyqtSlot(str, str)
    def _on_scan_outcome_warning(self, message: str, severity: str) -> None:
        """GUI thread: surface an interrupted/empty-scan toast. Errors are
        sticky (duration_ms=0) so a 'not saved' scan can't be missed."""
        duration = 0 if severity == "error" else 8000
        self.notify(message, severity, duration_ms=duration, tag="scan-outcome")

    def generate_user_label(self) -> str:
        suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        return f"ow{suffix}"

    @pyqtSlot(str, int, int, int, bool, result=bool)
    def startCapture(
        self,
        subject_id: str,
        duration_sec: int,
        left_camera_mask: int,
        right_camera_mask: int,
        disable_laser: bool,
    ) -> bool:
        """Start capture asynchronously; returns True if kicked off."""
        logger.info(
            f"startCapture(subject_id={subject_id}, dur={duration_sec}s, "
            f"left_mask=0x{left_camera_mask:02X}, right_mask=0x{right_camera_mask:02X}, "
            f"disable_laser={disable_laser})"
        )

        # Issue #352: a disconnected side's mask must be recorded as 0x00
        # ("None"), not the UI's pending selection — the SDK persists the
        # ScanRequest masks verbatim into session_meta.sdk_flags, which
        # History and the replay grid trust (#175), so an ungated mask shows
        # a phantom config and empty panes for hardware that was never
        # there. The UI keeps the pending selection (it applies if the
        # sensor is plugged in before scanning; zeroing it would regress the
        # #127 preserve-across-power-cycle behavior), so the gate lives
        # here — mirroring runContactQualityCheck and calibration/test.
        left_camera_mask = (
            int(left_camera_mask) if self._leftSensorConnected else 0x00
        )
        right_camera_mask = (
            int(right_camera_mask) if self._rightSensorConnected else 0x00
        )

        if duration_sec <= 0:
            logger.warning("duration_sec was %s, clamping to 3600", duration_sec)
            duration_sec = 3600

        if self._capture_running or self._capture_thread is not None:
            self.captureLog.emit("Capture already running.")
            return False

        if self._safetyFailure:
            self.captureLog.emit(
                "Scan cannot start: laser safety system is tripped. Clear the safety interlock first."
            )
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
        if not self.checkStorageForScan():
            return False

        self._capture_stop = threading.Event()
        # Each new scan starts with a fresh notes buffer
        self._scan_notes = ""
        self._scan_notes_session_label = ""
        self.scanNotesChanged.emit()
        self._capture_running = True
        self._capture_start_time = time.time()
        self._audit.log("scan_started", {
            "label": subject_id,
            "left_mask": int(left_camera_mask),
            "right_mask": int(right_camera_mask),
        })
        # Per-scan monotonic zero for plot timestamps. sample.timestamp_s comes
        # from each sensor's firmware clock, which resets on sensor reboot — so
        # after a mid-scan unplug/replug, the two sides' clocks diverge and the
        # QML plot's shared `latestTimestamp` prunes the lagging side to empty.
        plot_t0 = time.monotonic()
        self._plot_t0 = plot_t0  # scan-start anchor (NOT the sample t axis —
        # samples carry SDK-normalized timestamps, t=0 at the first frame,
        # which arrives seconds after this line; see issue #284)
        # Real-time plot viewer: construct a fresh LiveScanSource for this scan
        # and install it on the connector. Phase 1 has no QML consumer yet —
        # the sinks (added in subsequent tasks) accumulate samples here in
        # parallel with the legacy Qt signal emissions.
        # Pass the scan DB path so the live source can lazily load older
        # samples from the DB tail when the user pans before the in-memory
        # ring-trim window (>30 min into a long scan). None-safe: if the
        # interface has no scan_db_path the source stays in-memory-only.
        # In-memory cache size before ring-trim → DB tail. Default 30 min
        # (1800 s × 40 Hz). Shrink liveCacheMaxSeconds in app_config to
        # exercise the DB lazy-load quickly (e.g. 60 → eviction after 1 min).
        _live_cache_sec = self._app_config.get("liveCacheMaxSeconds", 1800)
        _live_cache_samples = max(2, int(float(_live_cache_sec) * 40))
        # BVI display low-pass (#228, #552): the compiled cutoff, gated by
        # the research-only Settings switch (see _bvi_lpf_cutoff_hz).
        # Applied at live ingest only; scans.db / CSVs / replay stay raw.
        _bvi_lpf_cutoff = self._bvi_lpf_cutoff_hz()
        logger.info("BVI display low-pass: %s",
                    self._describe_bvi_lpf(_bvi_lpf_cutoff))
        live_source = LiveScanSource(
            plot_t0=plot_t0,
            parent=self,
            scan_db_path=getattr(self._interface, "scan_db_path", None),
            cache_max_samples=_live_cache_samples,
            bvi_lpf_cutoff_hz=_bvi_lpf_cutoff,
        )
        # Track the live source separately so the user can navigate
        # to a past scan and return; emit so QML rebinds the
        # PlotToolbar's "Back to live" visibility. The previous scan's
        # live source is retired below — release() is idempotent, so it
        # doesn't matter if _set_current_scan_source already retired it
        # as the outgoing current source.
        prev_live = self._live_scan_source
        self._live_scan_source = live_source
        if prev_live is None:
            self.liveSourceAvailableChanged.emit()
        self._set_current_scan_source(live_source)
        self._retire_scan_source(prev_live)
        self._capture_left_path = ""
        self._capture_right_path = ""

        # Camera dropout watchdog state — fresh per scan.
        self._camera_last_seen = {}
        self._camera_last_temp = {}
        self._camera_dropped = set()
        self._scan_abort_notified = False
        self._scan_abort_code = None
        self._scan_abort_reason = ""
        self._capture_subject_id = subject_id
        # Snapshot the devices participating in THIS scan for the mid-scan
        # disconnect abort (issue #387): the console always participates;
        # a sensor participates only when its gated camera mask is non-zero.
        self._capture_active_sides = (
            {"console"}
            | ({"left"} if left_camera_mask else set())
            | ({"right"} if right_camera_mask else set())
        )
        self._storage_next_check_mono = 0.0
        self._storage_warned = False
        self._dropout_timer.start()

        # NaN-gap tracker — fresh per scan (same lifecycle as the watchdog).
        # Bound to a local so the completion closure and the sink provably
        # share THIS scan's tracker even if the attribute is reset elsewhere.
        nan_gap_tracker = NanGapTracker()
        self._nan_gap_tracker = nan_gap_tracker

        # Interrupted-scan outcome tracker — bound to a local so the
        # completion closure and the sink list provably share THIS scan's
        # instance (same pattern as nan_gap_tracker above).
        outcome_sink = _ScanOutcomeSink()

        # Reset the trigger-ON clock so _scan_elapsed_str starts from zero.
        self._trigger_clock_reset()

        # _CompletionSink calls this from its on_complete() method once the
        # ScanRunner finishes.
        def _on_pipeline_complete(meta):
            """Fires from _CompletionSink.on_complete() at the end of the scan."""
            # Determine whether the user requested a stop (cancellation).
            canceled = self._capture_stop.is_set()

            if canceled:
                self.captureLog.emit("Scan stopped.")
            else:
                self.captureLog.emit("Capture session complete.")

            # Trigger-ON duration sourced from _TriggerStateSink so notes
            # report the actual laser-on time, not wall-clock (which includes
            # pre-scan setup + post-scan USB drain). Falls back to wall-clock
            # if the sink never saw a TriggerStateEvent (e.g. cancel before
            # trigger fired).
            if self._trigger_on_mono is not None:
                # No OFF event and no disconnect-close reached us before
                # completion — close here as a last resort, knowing the
                # measurement now includes scan-teardown time.
                logger.warning(
                    "Scan completed with the trigger clock still open "
                    "(no TriggerStateEvent OFF received); duration may "
                    "include teardown time."
                )
                self._trigger_clock_close()
            trigger_elapsed = self._trigger_cumulative_s
            if trigger_elapsed > 0:
                elapsed = trigger_elapsed
            else:
                elapsed = time.time() - self._capture_start_time
            hours = int(elapsed // 3600)
            minutes = int((elapsed % 3600) // 60)
            seconds = int(elapsed % 60)
            duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            status = "stopped" if canceled else "completed"
            logger.info(
                "=== Full scan ended: %s — duration %s ===", status, duration_str
            )
            duration_line = f"\n---\nScan {status} — duration: {duration_str}"
            self._scan_notes = (self._scan_notes.strip() + duration_line)
            # Data-gap footer: aggregate ranges where any camera went >1 s
            # without a finite BFI/BVI sample (sustained NaN-fill/dropout).
            # Empty string when the scan was clean. Fail-soft like the rest
            # of this handler — a tracker bug must never block notes
            # persistence.
            try:
                gap_line = gap_note_line(nan_gap_tracker)
                if gap_line:
                    logger.warning("Scan data gaps detected: %s", gap_line.strip())
                    self._scan_notes += gap_line
            except Exception:
                logger.exception("NaN-gap footer computation failed")
            self.scanNotesChanged.emit()

            # Persist notes to the scan DB session ScanDBSink created at
            # scan start (label = scan_id_subject_id). Later edits via the
            # Notes modal update the same row through the scanNotes setter.
            scan_id = getattr(meta, "scan_id", "") if meta else ""
            session_label = f"{scan_id}_{subject_id}" if scan_id else ""
            self._scan_notes_session_label = session_label
            # Stamp the actual trigger-ON duration (same value as the notes
            # line above) so History shows the real scan time, not the pipeline
            # wall-clock session_end - session_start (issue #335).
            self._persist_scan_notes(session_label, actual_duration_sec=elapsed)

            # Interrupted-scan outcome. An interrupted scan loses its open
            # interval; one that ends before any interval closes saves
            # nothing. Surface a toast; ScanDBSink deletes the empty session
            # row and the History viewer guards against loading it.
            try:
                outcome = classify_scan_outcome(
                    final_frames=outcome_sink.final_frames,
                    terminal_dark_missing=outcome_sink.terminal_dark_missing,
                    canceled=canceled,
                    disable_laser=disable_laser,
                )
                if outcome.severity:
                    logger.warning(
                        "Scan outcome: %s — %s", outcome.kind, outcome.message
                    )
                    self._scanOutcomeWarningSignal.emit(
                        outcome.message, outcome.severity
                    )
            except Exception:
                logger.exception("scan-outcome classification failed")

            # The pipeline writes its own output; there is no .raw→.csv
            # post-processing step, so captureFinished carries empty paths.
            self._capture_left_path = ""
            self._capture_right_path = ""
            self._capture_running = False
            self._safety_cancel_scheduled = False
            self._capture_thread = None
            try:
                _outcome = outcome
            except Exception:
                _outcome = None  # classification raised above
            try:
                _dur = (time.time() - self._capture_start_time
                        if self._capture_start_time else None)
            except Exception:
                _dur = None
            # Termination cause + data verdict (issue #535): a fault-aborted
            # scan is recorded as "aborted" with its error code, never as an
            # operator stop.
            self._log_scan_ended_audit(
                subject_id, session_label, _dur, _outcome, canceled,
            )
            self.captureFinished.emit(True, "", "", "")
            # Open the notes modal now, UNLESS a critical-error modal is on
            # screen (e.g. an E-304 device-disconnect abort raised it seconds
            # ago): defer the open until the error is dismissed so the error
            # modal stays on top and doesn't lose focus to the notes field
            # behind it (issue #387).
            self._emit_or_defer_scan_notes()

        # Issue #43: telemetry CSVs are engineering diagnostics — clinical
        # users must not get them. Default False (fail closed). Raw
        # histogram CSVs are research data (#234): allowed whenever the
        # build is not clinical (clinicalMode false) or engineeringMode is
        # unlocked — never on a plain clinical build.
        engineering_mode = self._app_config.get("engineeringMode", False)
        raw_csv_allowed = (
            not self._app_config.get("clinicalMode", False)
            or engineering_mode
        )

        # Contact-quality config for the live monitor sink below (issue
        # #364). Nothing in Settings writes these keys today, so the only
        # way to hit a bad value is a hand-edited config — but a malformed
        # one (wrong type, a null/non-numeric element, a null scalar) must
        # degrade to the shipped defaults rather than raise on the clinical
        # scan path, before the SDK's own non-critical/safe-consume
        # protections for this sink ever get a chance to apply.
        try:
            _cq_thresholds = CQThresholds.from_sequences(
                self._app_config.get("cq_dark_threshold_per_camera")
                or [CQ_DEFAULT_DARK_THRESHOLD_DN] * 8,
                self._app_config.get("cq_light_threshold_per_camera")
                or [CQ_DEFAULT_LIGHT_THRESHOLD_DN] * 8,
            )
        except Exception as exc:
            logger.warning(
                "Invalid cq_dark_threshold_per_camera / "
                "cq_light_threshold_per_camera config (%s); falling back "
                "to default CQ thresholds", exc,
            )
            _cq_thresholds = CQThresholds.from_sequences(
                [CQ_DEFAULT_DARK_THRESHOLD_DN] * 8,
                [CQ_DEFAULT_LIGHT_THRESHOLD_DN] * 8,
            )

        def _cq_int_or(key, default):
            """int(app_config[key]) with a fallback for a missing/null
            config key (value is None — cfg.get(key, default) only
            substitutes default when the key is *absent*, not when it's
            present as JSON null) and for a present-but-malformed one
            (non-numeric string, wrong type) alike. Checking `is not None`
            rather than `value or default` also means an explicit 0
            converts to 0, not default — ContactQualityMonitor's own
            max(1, int(...)) clamp is what turns a deliberate 0 into "no
            debounce", not this fallback.
            """
            value = self._app_config.get(key)
            if value is None:
                return default
            try:
                return int(value)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid %s config value %r; using default %s",
                    key, value, default,
                )
                return default

        _cq_rolling_window = _cq_int_or(
            "cq_rolling_avg_window", CQ_DEFAULT_ROLLING_WINDOW)
        # Asymmetric debounce (#364): RAISE the warning fast, CLEAR it slow.
        _cq_light_activate = _cq_int_or(
            "cq_live_activate_frames", CQ_DEFAULT_LIVE_ACTIVATE_FRAMES)
        _cq_light_clear = _cq_int_or(
            "cq_live_clear_frames", CQ_DEFAULT_LIVE_CLEAR_FRAMES)

        # Engineering-mode alternative camera + laser settings (#446/#449),
        # shared with the preflight signal-quality check (#510).
        self._sync_alt_scan_settings(left_camera_mask, right_camera_mask)

        req = ScanRequest(
            subject_id=subject_id,
            duration_sec=duration_sec,
            left_camera_mask=left_camera_mask,
            right_camera_mask=right_camera_mask,
            disable_laser=disable_laser,
            reduced_mode=self._app_config.get("clinicalMode", False),
            # Corrected CSV is opt-in now that per-cam BFI/BVI lands in
            # the scan DB (the new viewer + past replay read from there).
            # Default False to skip the redundant {scan_id}.csv; flip
            # writeCorrectedCsv:true in app_config to keep exporting it.
            # The SDK still forces it on if no DB is configured.
            write_corrected_csv=self._app_config.get("writeCorrectedCsv", False),
            # Issue #43 (regression): the SDK defaults write_telemetry_csv
            # to True, so the per-scan {scan_id}_{subject}_telemetry.csv
            # must be explicitly gated on engineeringMode here. The original
            # gate was dropped in the sink-refactor follow-up (93c2feb).
            # #471 made it additionally opt-in: the persisted toggle is
            # re-checked at scan start alongside the mode flag, so a stale
            # writeTelemetryCsv=true never writes on a plain clinical build.
            write_telemetry_csv=engineering_mode and self._write_telemetry_csv,
            # Raw CSV duration forwarded to the pipeline's Tee("raw") gate
            # via raw_save_max_duration_s. None means unbounded (write entire
            # scan); 0 omits raw tee entirely. The persisted writeRawCsv
            # toggle is additionally gated on the mode flags (#234: research
            # users get raw CSVs) so a Clinical build never writes raw CSVs
            # even if a prior research/engineering session left the toggle
            # on (#43 invariant).
            raw_save_max_duration_s=(
                self._raw_csv_duration_sec
                if (raw_csv_allowed and self._write_raw_csv) else 0
            ),
            sinks=[
                _LivePlotSink(connector=self, plot_t0=plot_t0, live_source=live_source,
                              nan_gap_tracker=nan_gap_tracker),
                _TriggerStateSink(connector=self),
                outcome_sink,
                _CompletionSink(connector=self, on_complete_cb=_on_pipeline_complete),
                # Live mid-scan contact-quality warnings (issue #364). This
                # is the sink that was never written when the app's
                # callbacks were ported to sink classes in 2026-05 — the
                # QML modal and both Qt signals have been waiting for it
                # since. Not `critical`: a contact fault must never abort a
                # clinical scan in progress.
                ContactQualityMonitor(
                    thresholds=_cq_thresholds,
                    on_transition=self._on_cq_transition,
                    rolling_window=_cq_rolling_window,
                    light_activate_debounce=_cq_light_activate,
                    light_clear_debounce=_cq_light_clear,
                ),
            ],
            # Async backstop (#213): if the worker aborts after start_scan
            # returned True (e.g. a critical sink dies because the scan DB
            # became unwritable past the preflight), it can't ride the return
            # value. Fires on the worker thread, so just hop to the main thread
            # via a queued signal — see _on_scan_worker_failed.
            on_error=lambda exc: self.scanWorkerFailedDuringCapture.emit(str(exc)),
        )

        started = self._interface.start_scan(req)
        if started:
            logger.info(
                "=== Full scan started: subject=%s duration=%ss "
                "left=0x%02X right=0x%02X laser=%s ===",
                subject_id, duration_sec, left_camera_mask, right_camera_mask,
                "off" if disable_laser else "on",
            )
            if self._sensor_debug_logging:
                # DEBUG_FLAG_USB_PRINTF is set, so this scan carries an
                # elevated camera-dropout risk on firmware without
                # sensor-fw#115: the firmware's queue-full diagnostic is
                # transmitted over COMM, and if the host stops draining COMM
                # that send busy-waits the firmware main loop, starving the
                # 25 ms camera DMA re-arm. Logged at WARNING so a scan taken
                # in this state is identifiable after the fact.
                logger.warning(
                    "Scan started with sensor debug logging ENABLED "
                    "(DEBUG_FLAG_USB_PRINTF) — elevated camera-dropout risk; "
                    "not recommended for clinical acquisition"
                )
            # Bind the live source's DB tail to THIS scan's session row by its
            # exact label (set synchronously inside start_scan), so a later
            # pan-into-past resolves the right session instead of guessing the
            # newest one.
            label = getattr(self._scan_workflow, "current_scan_label", None)
            if label:
                live_source.set_scan_label(label)
        if not started:
            self._capture_running = False
            # Unbind the viewer. The orphaned LiveScanSource itself stays
            # held as _live_scan_source (it is NOT freed by dropping the
            # current-source reference — it's parented to the connector)
            # until the next startCapture retires it.
            self._set_current_scan_source(None)
            # start_scan refuses for two reasons: a prior worker still alive,
            # or a pre-flight failure (e.g. the scan DB couldn't be opened, in
            # which case the scan is aborted before the laser fires so its data
            # isn't silently lost). last_scan_error carries the specific reason
            # when it's the latter.
            reason = getattr(self._scan_workflow, "last_scan_error", None)
            # Log at WARNING so this is visible in the run log file —
            # captureLog signal goes through QML console.log which is
            # filtered out by default.
            if reason:
                logger.warning("startCapture aborted: %s", reason)
                self.captureLog.emit(f"Scan aborted: {reason}")
                self.errorOccurred.emit(reason)
                self._raise_critical("E-301", detail=reason)
            else:
                logger.warning(
                    "startCapture aborted: SDK refused to spawn a new scan "
                    "(usually a previous worker thread that didn't exit cleanly)."
                )
                self.captureLog.emit("Capture already running.")
                self._raise_critical("E-302")
        return bool(started)

    @pyqtSlot(str)
    def _on_scan_worker_failed(self, detail: str):
        """Main-thread handler for an async scan-worker abort (#213).

        The SDK worker invokes ScanRequest.on_error after start_scan already
        returned True — typically a critical sink (ScanDBSink) dying because
        the scan DB became unwritable in the race window past the synchronous
        preflight. Surface E-301 and tear the capture down so the app doesn't
        sit 'capturing' forever with a dead worker.
        """
        if not self._capture_running:
            return  # already torn down (e.g. user cancelled first)
        logger.warning("scan worker aborted mid-capture: %s", detail)
        self.captureLog.emit(f"Scan aborted: {detail}")
        self._note_scan_abort("E-301", detail)
        self._raise_critical("E-301", detail=detail)
        # Tear down: stop the dropout watchdog and cancel the (dead) scan, then
        # clear running state and unblock QML, mirroring the completion path.
        self.stopCapture()
        self._capture_running = False
        self._safety_cancel_scheduled = False
        self._set_current_scan_source(None)
        # The pipeline never completed, so _on_pipeline_complete — which
        # records scan_ended for every other ending — will not run for this
        # scan: record the abort here or the audit trail carries a
        # scan_started with no end (issue #535). No double-log risk: when the
        # runner does reach the completion sink (mid-scan exception, or the
        # critical-sink rollback) it does so synchronously on the worker
        # BEFORE on_error fires, and that handler clears _capture_running,
        # which the guard at the top of this slot honours.
        try:
            _dur = (time.time() - self._capture_start_time
                    if self._capture_start_time else None)
        except Exception:
            _dur = None
        self._log_scan_ended_audit(
            self._capture_subject_id, "", _dur, None, True,
        )
        self.captureFinished.emit(False, detail, "", "")

    def _ensure_idle(self) -> str | None:
        """Gate for pipeline-starting slots (capture / configure / check)."""
        # Issue #303: the connect-time bring-up runs async for a few
        # seconds after the handles report READY. A capture/CQ/configure
        # started inside that window collides with it: on a sensor,
        # "Failed to program FPGA" and a camera wedged until power-cycle;
        # on the console, a scan before the laser registers are loaded.
        # The UI start gate polls isPipelineIdle and defers past this.
        if self.deviceInitBusy:
            return ("The system is still initializing — wait a few seconds "
                    "and try again")
        if self._cq_quick_running:
            return "Contact-quality check already in progress"
        if self._capture_running or self._capture_thread is not None:
            return "Scan already running"
        if self._config_running:
            return "Camera configuration already in progress"
        workflow = getattr(self, "_scan_workflow", None)
        if workflow is not None and getattr(workflow, "running", False):
            return "Scan already running"
        if workflow is not None and getattr(workflow, "config_running", False):
            return "Camera configuration already in progress"
        return None

    @pyqtSlot(result=bool)
    def isPipelineIdle(self) -> bool:
        """QML probe: True when a pipeline-starting slot called right now
        would not be refused by _ensure_idle. The pre-scan CQ check's SDK
        worker keeps unwinding for ~2 s after results are displayed, so a
        fast Start Scan click used to be refused synchronously and silently
        — BloodFlow's scan-start gate polls this instead of firing blind."""
        return self._ensure_idle() is None

    @pyqtSlot(result=bool)
    def isConfigInFlight(self) -> bool:
        """QML probe: True while a camera-sensor configuration (FPGA
        flash) is in flight or still draining after a cancel. Unlike the
        other _ensure_idle busy states (a few seconds at most), a config
        holds the pipeline for ~50 s — BloodFlow's start gate (issue #283)
        stretches its wait deadline to flash timescale while this is True
        instead of erroring out at the short deadline."""
        if self._config_running:
            return True
        workflow = getattr(self, "_scan_workflow", None)
        return bool(workflow is not None and getattr(workflow, "config_running", False))

    @pyqtSlot()
    def _on_dropout_check(self):
        """1 Hz watchdog: emit cameraDropoutDetected for any camera silent > threshold."""
        if not self._capture_running:
            return
        # Low-storage stop (issue #506). Ahead of the trigger-state gate
        # below: the pipeline writes scans.db for the whole capture, not
        # only while the trigger is ON.
        if not self._scan_abort_notified and self._storage_check_due():
            if self._check_scan_storage():
                return
        # Also bail when the trigger is OFF. _capture_running only
        # flips false in _on_complete after the post-scan cleanup
        # finishes, so between trigger-stop and that flip there's a
        # 2+ s window where samples have legitimately stopped arriving
        # but the watchdog still considers the cameras 'live'. Without
        # this gate, every natural scan end fires a spurious 'Camera
        # X connection lost at HH:MM:SS' toast on every active cam.
        if self._trigger_state != "ON":
            return
        # Once a scan-ending abort has fired (E-303 stall / E-304 device
        # disconnect), the critical modal IS the notification — stop
        # emitting per-camera 'connection lost' toasts for a scan that is
        # already tearing down. Without this gate a system-wide unplug
        # buries the device-level cards under up to 16 camera cards
        # (issue #489).
        if self._scan_abort_notified:
            return
        now = time.monotonic()
        threshold = self._camera_dropout_threshold_sec
        for key, last_t in list(self._camera_last_seen.items()):
            if key in self._camera_dropped:
                continue
            if now - last_t > threshold:
                side, cam_id = key
                temp = self._camera_last_temp.get(key, float("nan"))
                elapsed_str = self._scan_elapsed_str()
                msg = (
                    f"[{elapsed_str}] Camera {side.upper()} {cam_id + 1} dropout detected "
                    f"(no frames for >{threshold:.0f} s). Last temperature: {temp:.1f}°C"
                )
                logger.warning(msg)
                self.notify(
                    f"Camera {side.upper()} {cam_id + 1} connection lost at {elapsed_str}"
                    f" — last temp {temp:.1f}°C",
                    type_="warning",
                    duration_ms=30000,
                    tag=f"dropout_{side}_{cam_id}",
                )
                self._camera_dropped.add(key)
                self.cameraDropoutDetected.emit(side, cam_id, elapsed_str)
                # Also feed the LiveScanSource so the PlotViewer renders a
                # dropout bar. The marker must live on the SAME time axis
                # as the plotted samples — SDK-normalized timestamps, t=0
                # at the scan's FIRST FRAME. (`now - self._plot_t0` counted
                # from startCapture instead, which runs seconds of sensor
                # setup before the first frame, so the marker landed past
                # the live edge — clipped invisible while following live —
                # and at a wrong x afterwards; issue #284.) Anchor at the
                # camera's own newest sample: exactly where its trace
                # stops. NaN (no plottable samples ever, e.g. a covered
                # camera) means there is no trace to mark — skip the bar,
                # the toast/signal above still surface the dropout.
                src = self._current_scan_source
                if src is not None and getattr(src, "live", False):
                    drop_t = src.last_sample_t(side, cam_id)
                    if math.isfinite(drop_t):
                        src.mark_dropped(side=side, cam_id=cam_id, t=drop_t)

        # ── All-camera stall (issue #248) ─────────────────────────────
        # Per-camera dropouts above are informational (fail-soft: the
        # scan keeps running on the remaining cameras). Total data loss
        # is NOT fail-soft: if no camera has delivered a frame for
        # scanDataStallTimeoutSec the scan is dead air — abort it and
        # tell the user instead of counting down to a hollow "complete".
        # (An already-notified abort can't reach here — the issue-#489
        # early return above bailed, and nothing in the per-camera loop
        # sets the flag synchronously.)
        stalled_s = _scan_data_stall_decision(
            now,
            self._trigger_on_mono,
            self._camera_last_seen,
            self._scan_data_stall_timeout_sec,
        )
        if stalled_s is not None:
            self._abort_scan_data_stall(stalled_s)

    def _dismiss_dropout_toasts(self, side: str | None = None) -> None:
        """Dismiss per-camera 'connection lost' toasts (issue #489).

        With ``side`` given, clears that sensor's 8 possible camera tags —
        used on a sensor disconnect, where the device-level notification
        subsumes its cameras' cards (including a card lingering from a scan
        that already ended, and cards that fired in the window before the
        USB drop was detected). With no ``side``, clears every camera the
        current scan flagged dropped — used by the scan-abort paths, where
        the E-303/E-304 modal is the notification. Dismissing a tag with no
        matching toast is a no-op, so over-clearing is harmless.
        """
        if side is not None:
            keys = [(side, cam_id) for cam_id in range(8)]
        else:
            keys = sorted(self._camera_dropped)
        if not keys:
            return
        tags = [f"dropout_{s}_{cam_id}" for s, cam_id in keys]
        # One summary line, emitting directly instead of routing through
        # dismissNotification's per-call INFO — a system unplug would
        # otherwise print 16 mostly-no-op dismiss lines.
        logger.info("Dismissing per-camera dropout toasts: %s",
                    ", ".join(tags))
        for tag in tags:
            self.notificationDismissByTagRequested.emit(tag)

    def _note_scan_abort(self, code: str, detail: str = "") -> None:
        """Record that a fault is ending the current scan (issue #535).

        Called from every scan-ending fault path — E-303 stall, E-304
        device disconnect, E-202 safety trip, E-301 worker death — right
        before the critical modal is raised and the scan is cancelled, so
        the scan_ended audit event can name the cause instead of reporting
        the cancel it triggers as an operator stop. First fault wins: a
        disconnect that also trips the stall watchdog keeps its own code.
        """
        if self._scan_abort_code:
            logger.info(
                "Scan abort %s (%s) after %s already recorded — keeping "
                "the first", code, detail, self._scan_abort_code,
            )
            return
        self._scan_abort_code = code
        self._scan_abort_reason = detail or ""

    def _log_scan_ended_audit(
        self,
        subject_id: str,
        session_label: str,
        duration_s: Optional[float],
        outcome: Optional[ScanOutcome],
        canceled: bool,
    ) -> None:
        """Append the ``scan_ended`` audit event (issue #535). Never raises —
        the audit log is fail-soft and so is this."""
        try:
            details: Dict[str, Any] = {
                "label": subject_id,
                "session_label": session_label or None,
                "duration_s": (round(duration_s, 1)
                               if duration_s is not None else None),
            }
            details.update(audit_scan_ended_details(
                outcome=outcome,
                canceled=canceled,
                abort_code=self._scan_abort_code,
                abort_reason=self._scan_abort_reason,
            ))
            self._audit.log("scan_ended", details)
        except Exception:
            logger.warning("scan_ended audit log failed", exc_info=True)

    def _abort_scan_data_stall(self, stalled_s: float) -> None:
        """Abort the running scan because data acquisition has completely
        stopped (issue #248). Runs on the main thread from the 1 Hz
        dropout watchdog. Mirrors the safety-trip path: capture-log line,
        critical-error modal (E-303), then stopCapture() — which cancels
        the SDK scan; the pipeline's completion sink then finalizes the
        data files and emits captureFinished, returning the QML scan flow
        (and the state machine) to idle exactly like a user Stop.
        """
        self._scan_abort_notified = True
        # The modal supersedes any per-camera 'connection lost' toasts the
        # watchdog already fired for this scan (issue #489).
        self._dismiss_dropout_toasts()
        elapsed_str = self._scan_elapsed_str()
        msg = (
            f"[{elapsed_str}] No data from any camera for "
            f">{self._scan_data_stall_timeout_sec:.0f} s — data acquisition "
            f"has stopped. Stopping the scan; data captured before the loss "
            f"is saved."
        )
        logger.error(msg)
        self.captureLog.emit(msg)
        detail = (
            f"no camera frames for {stalled_s:.0f} s "
            f"(scan elapsed {elapsed_str})"
        )
        self._note_scan_abort("E-303", detail)
        self._raise_critical("E-303", detail=detail)
        self.stopCapture()

    def _storage_detail(self, free: Optional[int]) -> str:
        return (f"{(free or 0) / disk_space.MB:.0f} MB free on the data "
                f"drive ({self._data_root})")

    def check_startup_storage(self) -> None:
        """Startup free-space check (issue #506): raise the E-107 critical
        modal when the data drive has under minFreeDiskMb free. Called
        once by main.py after the QML window has loaded."""
        free = disk_space.free_bytes(self._data_root)
        if free is not None:
            logger.info("Startup storage check: %.0f MB free on %s",
                        free / disk_space.MB, self._data_root)
        if disk_space.below(free, self._min_free_disk_mb):
            self._raise_critical("E-107", detail=self._storage_detail(free))

    @pyqtSlot(result=bool)
    def checkStorageForScan(self) -> bool:
        """Start-button free-space check (issue #506). Returns False —
        after raising the E-305 critical modal — when the data drive has
        under minFreeDiskMb free; the scan (and its pre-scan contact
        check) must not start. A failed free-space query never blocks:
        the mid-scan stop still protects the drive."""
        free = disk_space.free_bytes(self._data_root)
        if not disk_space.below(free, self._min_free_disk_mb):
            return True
        detail = self._storage_detail(free)
        logger.warning("Scan start refused: %s", detail)
        self.captureLog.emit(f"Scan not started: {detail}.")
        self._raise_critical("E-305", detail=detail)
        return False

    def _storage_check_due(self) -> bool:
        """Rate-limit the mid-scan free-space query (issue #506) to one
        every _STORAGE_CHECK_INTERVAL_S; the first watchdog tick of a scan
        always checks."""
        now = time.monotonic()
        if now < self._storage_next_check_mono:
            return False
        self._storage_next_check_mono = now + _STORAGE_CHECK_INTERVAL_S
        return True

    def _check_scan_storage(self) -> bool:
        """Mid-scan free-space check (issue #506), from the 1 Hz watchdog.
        Under scanStopFreeDiskMb the scan is stopped (returns True); under
        minFreeDiskMb the operator gets one warning toast per scan."""
        free = disk_space.free_bytes(self._data_root)
        if disk_space.below(free, self._scan_stop_free_disk_mb):
            self._stop_scan_low_storage(free)
            return True
        if not self._storage_warned and disk_space.below(
                free, self._min_free_disk_mb):
            self._storage_warned = True
            free_mb = free / disk_space.MB
            logger.warning(
                "[%s] Data drive below %.0f MB free (%.0f MB) during scan",
                self._scan_elapsed_str(), self._min_free_disk_mb, free_mb)
            self.notify(
                f"Storage is running low: {free_mb:.0f} MB free on the data "
                f"drive. The scan will stop automatically at "
                f"{self._scan_stop_free_disk_mb:.0f} MB.",
                type_="warning", duration_ms=0,
                tag=_LOW_STORAGE_TOAST_TAG,
            )
        return False

    def _stop_scan_low_storage(self, free: Optional[int]) -> None:
        """Stop the running scan because the data drive is nearly full
        (issue #506). Twin of _abort_scan_data_stall: capture-log line,
        the E-306 critical modal, then stopCapture() — the pipeline
        finalizes scans.db in the space the threshold kept free. Shares
        the one-shot abort guard with E-303/E-304. The modal is the
        notification, so it supersedes the earlier "running low" toast
        and any per-camera dropout toasts.
        """
        self._scan_abort_notified = True
        self._dismiss_dropout_toasts()
        self.notificationDismissByTagRequested.emit(_LOW_STORAGE_TOAST_TAG)
        elapsed_str = self._scan_elapsed_str()
        free_mb = (free or 0) / disk_space.MB
        msg = (
            f"[{elapsed_str}] Data drive almost full ({free_mb:.0f} MB "
            f"free, stop threshold {self._scan_stop_free_disk_mb:.0f} MB). "
            f"Stopping the scan; data captured so far is saved."
        )
        logger.error(msg)
        self.captureLog.emit(msg)
        detail = f"{self._storage_detail(free)}; scan elapsed {elapsed_str}"
        self._note_scan_abort("E-306", detail)
        self._raise_critical("E-306", detail=detail)
        self.stopCapture()

    def _abort_scan_device_disconnect(self, name: str) -> None:
        """Abort the running scan because a participating device was
        unplugged mid-scan (issue #387). Runs on the main thread from the
        connection handler. Twin of _abort_scan_data_stall: it shares the
        one-shot guard (so a disconnect + a stall — or two devices dropping
        — can't stack two modals), writes a capture-log line, raises the
        E-304 critical modal, then stopCapture(). The SDK is already tearing
        the scan down via its own mid-scan disconnect subscription; the
        stopCapture() here cancels it app-side too and, by marking the scan
        canceled, suppresses the redundant end-of-scan outcome toast — the
        modal is the notification.
        """
        if self._scan_abort_notified:
            return
        self._scan_abort_notified = True
        # The modal supersedes any per-camera 'connection lost' toasts the
        # watchdog already fired for this scan (issue #489).
        self._dismiss_dropout_toasts()
        elapsed_str = self._scan_elapsed_str()
        msg = (
            f"[{elapsed_str}] {name} disconnected during the scan — "
            f"stopping the scan; data captured before the disconnection "
            f"is saved."
        )
        logger.error(msg)
        self.captureLog.emit(msg)
        detail = f"{name} disconnected mid-scan (scan elapsed {elapsed_str})"
        self._note_scan_abort("E-304", detail)
        self._raise_critical("E-304", detail=detail)
        self.stopCapture()

    def _emit_or_defer_scan_notes(self) -> None:
        """Open the end-of-scan notes modal, or defer it if a critical-error
        modal is currently on screen (issue #387).

        On an E-304 device-disconnect abort (and E-303 / E-202) the critical
        modal is raised, then the scan unwinds for a second or two before
        this completion handler runs. The notes modal opening now would pop
        UNDER the error modal and steal keyboard focus. Deferring holds the
        open until criticalErrorsDismissed() flushes it, so the error modal
        stays on top and only one modal is on screen at a time.

        Called from the pipeline completion handler (worker thread). The
        re-check after setting the pending flag closes the tiny race where
        the modal is dismissed between the check and the set.
        """
        if not self._critical_modal_active:
            self.scanNotesReady.emit()
            return
        self._scan_notes_pending = True
        if not self._critical_modal_active:
            self._scan_notes_pending = False
            self.scanNotesReady.emit()

    def _on_camera_dropout_recovered(self, side: str, cam_id: int) -> None:
        """Frames resumed for a camera the watchdog had flagged Connection
        Lost — surface the recovery. Called from _LivePlotSink.consume on
        the pipeline runner thread right after _rearm_dropped_camera
        un-marked the key (signal emits and notify() are thread-safe: both
        go through queued signal delivery).

        Reuses the dropout toast's tag so the "connection lost" toast is
        replaced in place rather than stacking a second one.
        """
        elapsed_str = self._scan_elapsed_str()
        self.notify(
            f"Camera {side.upper()} {cam_id + 1} reconnected at {elapsed_str}",
            type_="success",
            duration_ms=8000,
            tag=f"dropout_{side}_{cam_id}",
        )
        self.captureLog.emit(
            f"[{elapsed_str}] Camera {side.upper()} {cam_id + 1} frames "
            f"resumed — dropout watchdog re-armed."
        )
        self.cameraDropoutRecovered.emit(side, cam_id, elapsed_str)

    # --- TRIGGER-ON CLOCK (issue #201) ---
    # Single scan-time clock backing the notes duration line, the header
    # elapsed counter (scanElapsedSec) and dropout log timestamps. Opened /
    # closed by _TriggerStateSink from SDK TriggerStateEvents; also closed
    # by the console-disconnect handler because a dead console can't deliver
    # its OFF event (stop_trigger raises before _emit_trigger_event runs).

    def _trigger_clock_reset(self) -> None:
        """Zero the clock. Called once per scan from startCapture."""
        self._trigger_cumulative_s = 0.0
        self._trigger_on_mono = None
        self._trigger_on_ts = None

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
        receive-time measurement (a 16:00 scan shows as 16:01 — issue #201).
        Falls back to host receive-time when the timestamps are unusable
        (the SDK's t0-None path emits 0.0 for both edges, giving a
        non-positive delta) or when closing without an event (console lost).
        No-op when the clock is already closed, so a late OFF event after a
        disconnect-close can't double-count.
        """
        if self._trigger_on_mono is None:
            return
        ts_delta = (
            ts_s - self._trigger_on_ts
            if ts_s is not None and self._trigger_on_ts is not None
            else 0.0
        )
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
        """QML-facing accessor for trigger-ON elapsed whole seconds.

        The header counter polls this once a second instead of counting its
        own Timer ticks — a QML Timer fires late under GUI load and a
        tick-counting clock drifts behind real time, disagreeing with the
        notes duration line written from this same clock.
        """
        return int(self._trigger_elapsed_s())

    @pyqtSlot(result=str)
    def scanElapsedStr(self) -> str:
        """QML-facing accessor for trigger-ON elapsed time (HH:MM:SS)."""
        return self._scan_elapsed_str()

    def _scan_elapsed_str(self) -> str:
        """Return current scan elapsed trigger-ON time as HH:MM:SS."""
        total_s = int(self._trigger_elapsed_s())
        h = total_s // 3600
        m = (total_s % 3600) // 60
        s = total_s % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    # --- SENSOR COMMUNICATION METHODS ---
    @pyqtSlot(int, int, result=bool)
    def startConfigureCameraSensors(
        self, left_camera_mask: int, right_camera_mask: int
    ) -> bool:
        err = self._ensure_idle()
        if err is not None:
            logger.warning("startConfigureCameraSensors refused: %s", err)
            self.configFinished.emit(False, err)
            return False
        self._config_running = True
        req = ConfigureRequest(
            left_camera_mask=left_camera_mask,
            right_camera_mask=right_camera_mask,
            power_off_unused_cameras=bool(self._power_off_unused_cameras),
        )
        started = self._interface.start_configure_camera_sensors(
            req,
            on_progress_fn=lambda pct: self.configProgress.emit(int(pct)),
            on_log_fn=lambda msg: self.configLog.emit(msg),
            on_complete_fn=self._on_config_finished,
        )
        if not started:
            self._config_running = False
            self.configFinished.emit(False, "Configuration could not start")
        return bool(started)

    @pyqtSlot()
    def cancelConfigureCameraSensors(self):
        if self._config_running:
            self._interface.cancel_configure_camera_sensors()

    def _on_config_finished(self, result):
        # If a disconnect already finalized this run via _on_handle_state_changed,
        # the SDK's late callback would otherwise re-emit configFinished and
        # re-fire the QML flashTask handler. Guard so we emit exactly once.
        if not self._config_running:
            return
        self._config_running = False
        self.configFinished.emit(bool(result.ok), result.error or "")
