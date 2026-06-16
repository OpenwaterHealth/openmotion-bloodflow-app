from PyQt6.QtCore import (
    QObject,
    pyqtSignal,
    pyqtProperty,
    pyqtSlot,
    QVariant,
    QTimer,
    QRecursiveMutex,
)
from pathlib import Path
from typing import NamedTuple, Optional
import logging
import math
import base58
import threading
import json
import csv
import os
import datetime
import time
import random
import re
import string

from omotion import MotionInterface

from omotion.config import (
    DEBUG_FLAG_USB_PRINTF,
    DEBUG_FLAG_FAKE_DATA,
    DEBUG_FLAG_HISTO_THROTTLE,
    DEBUG_FLAG_HISTO_CMP,
    DEBUG_FLAG_COMM_VERBOSE,
    DEBUG_FLAG_CMD_VERBOSE,
)
from omotion.MotionProcessing import process_bin_file
from omotion.ScanWorkflow import ConfigureRequest, ScanRequest
from processing.visualize_bloodflow import VisualizeBloodflow
from motion_config import (
    load_tec_params,
)
from nan_gap_tracker import NanGapTracker, gap_note_line
from utils.resource_path import resource_path
from data_sources import (
    LiveScanSource, PastScanSource, ScanDataSource, buffers_are_empty,
)
import numpy as np
import pandas as pd

# constants for calculations
SCALE_V = 0.0909
SCALE_I = 0.25
R230 = 300e3
R234 = 300e3
TEC_VOLTAGE_DEFAULT = -0.07  # volts (DVT1a=-0.07, EVT2=1.16)
DATA_ACQ_INTERVAL = 1.0
# TEC ADC conversion constants + RT lookup moved to omotion.console_telemetry_conversions
# (V_REF / R_1 / R_2 / R_3 / R_s / 10K3CG_R-T.csv).

# Contact-quality quick-check defaults (overridable via app_config keys
# cq_dark_threshold_per_camera / cq_light_threshold_per_camera /
# cq_rolling_avg_window).
_CQ_DEFAULT_DARK_THRESHOLD_DN = 3.0
_CQ_DEFAULT_LIGHT_THRESHOLD_DN = 15.0
_CQ_DEFAULT_ROLLING_WINDOW = 10

# ── Developer-mode unlock ────────────────────────────────────────────────
# Hardcoded developer-mode password. Double-clicking the Openwater logo
# opens a prompt; entering this value sets developerMode=true (persisted).
# This is the ONLY place the literal is defined. The check lives in Python
# (not QML) so the literal never ships inside readable QML text.
_DEVELOPER_PASSWORD = "OpenwaterHealth"


def developer_password_matches(pw) -> bool:
    """Return True iff ``pw`` equals the developer-mode password."""
    return isinstance(pw, str) and pw == _DEVELOPER_PASSWORD


# Camera-mask → human config name, mirroring CameraSelectionModal's
# pattern table. Unmapped masks render as hex; -1 (unknown, e.g. a
# reduced-mode scan whose meta lacks sdk_flags) renders as an em dash.
_CONFIG_NAMES = {
    0x00: "None", 0x5A: "Near", 0x66: "Middle", 0xC3: "Far",
    0x99: "Outer", 0x0F: "Left", 0xF0: "Right", 0x42: "Third Row",
    0xFF: "All",
}


def _config_name(mask) -> str:
    if mask is None or mask < 0:
        return "—"
    m = int(mask) & 0xFF
    return _CONFIG_NAMES.get(m, f"0x{m:02X}")


logger = logging.getLogger("openmotion.bloodflow-app.connector")

# Define system states
DISCONNECTED = 0
SENSOR_CONNECTED = 1
CONSOLE_CONNECTED = 2
READY = 3
RUNNING = 4

# Issue #119: a single ``safety_known=False`` poll can be a transient I2C
# miss during a USB disconnect cascade rather than a real safety-chip
# fault. Require a streak before firing the persistent toast. Telemetry
# polls at ~1 Hz, so 3 polls ≈ 3 s — well under the time a user takes to
# reach Check, preserving the #107 contract (latched chip fault must
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


def _check_dropped_camera_emit(
    side: str,
    cam_id: int,
    dropped: set,
    already_logged: set,
) -> tuple[bool, str | None]:
    """Decide whether to emit a per-camera sample to the UI given the
    dropout watchdog's view of which cameras are 'Connection Lost'
    (issue #85).

    Returns ``(should_emit, recovery_warning)``:
      - ``should_emit`` is True when the camera is alive (not in
        ``dropped``); the caller proceeds with the normal emit.
      - ``should_emit`` is False when the camera is in ``dropped``;
        the caller suppresses the sample.
      - ``recovery_warning`` is a non-None string the FIRST time a
        given dropped camera key sends fresh data — the caller should
        log it at WARNING. Subsequent calls return None so a flapping
        camera doesn't spam 40 Hz worth of identical warnings.

    Mutates ``already_logged`` by inserting the key on the first
    suppression, which is what gates the one-per-dropout behavior.
    """
    key = (side, int(cam_id))
    if key not in dropped:
        return True, None
    if key in already_logged:
        return False, None
    already_logged.add(key)
    msg = (
        f"UNEXPECTED: Camera {side.upper()} {int(cam_id) + 1} sent "
        f"fresh data after being marked Connection Lost. Suppressing "
        f"the sample. Investigate the dropout cause — camera flapping, "
        f"firmware glitch, or a too-tight dropout threshold can all "
        f"produce this."
    )
    return False, msg


_SIDE_NAMES = ("left", "right")


class _LivePlotSink:
    """Subscribes to the 'live' pipeline channel and feeds per-frame samples
    into the LiveScanSource backing the PlotViewer, for each active camera.

    The 'live' channel carries a FrameBatch after BfiBvi. Each batch may
    contain multiple frames and both sides; we iterate frame × side × cam_id,
    gate on the camera-dropout watchdog, and append to the source.

    The 'live_side' channel carries one SideAverageSample per capture per side
    (from the SDK's LiveSideAverageStage, reduced mode) — the realtime per-side
    average, appended under cam_id=-1 for the reduced-mode display.
    """

    channels = {"live", "live_side"}

    def __init__(self, connector: "MotionConnector", plot_t0: float,
                 live_source: "LiveScanSource",
                 nan_gap_tracker: "NanGapTracker | None" = None):
        self._connector = connector
        self._plot_t0 = plot_t0
        self._live_source = live_source
        self._temp_alerted: dict[tuple[str, int], bool] = {}
        # Records every finite BFI/BVI sample so the scan-complete handler
        # can report sustained data gaps in the notes footer. Optional so
        # the sink works standalone (tests, future callers).
        self._nan_gap_tracker = nan_gap_tracker

    def on_scan_start(self, meta) -> None:
        self._temp_alerted.clear()

    def consume(self, channel: str, payload) -> None:
        if channel == "live_side":
            self._consume_side_avg(payload)
            return
        if channel != "live":
            return
        batch = payload
        if batch.bfi_live is None:
            return

        n = batch.bfi_live.shape[0]
        connector = self._connector
        threshold = connector._camera_temp_alert_threshold_c
        now_mono = time.monotonic()

        for i in range(n):
            ft = str(batch.frame_type[i]) if batch.frame_type is not None else "light"
            # Skip frames that carry no useful display signal.
            if ft in ("warmup", "stale"):
                continue
            is_dark = (ft == "dark")
            ts = float(batch.timestamp_s[i])
            abs_frame_id = int(batch.abs_frame_ids[i]) if batch.abs_frame_ids is not None else i
            plot_ts = ts

            side_ids = getattr(batch, "side_ids", None)
            cam_ids = getattr(batch, "cam_ids", None)
            if side_ids is not None and cam_ids is not None:
                side_idx = int(side_ids[i])
                cam_id = int(cam_ids[i])
                if side_idx < 0 or side_idx >= len(_SIDE_NAMES) or cam_id < 0 or cam_id >= 8:
                    continue
                side_cam_iter = [(side_idx, _SIDE_NAMES[side_idx], cam_id)]
            else:
                side_cam_iter = [
                    (side_idx, side, cam_id)
                    for side_idx, side in enumerate(_SIDE_NAMES)
                    for cam_id in range(8)
                ]

            for side_idx, side, cam_id in side_cam_iter:
                bfi = float(batch.bfi_live[i, side_idx, cam_id])
                bvi = float(batch.bvi_live[i, side_idx, cam_id])
                temp_c = float(batch.temperature_c[i, side_idx, cam_id])

                # Skip NaN samples — Qt plot would otherwise render them as
                # a spike from baseline to wherever NaN happens to land in
                # the y-mapping. Common cause: the first dark frame, which
                # the dark stage emits with mean_dc_rt=NaN (no prior light
                # to hold over). NaN now propagates cleanly through the
                # pipeline; skip it here for the plot.
                if not (math.isfinite(bfi) and math.isfinite(bvi)):
                    continue

                _key = (side, cam_id)

                # Finite sample — feed the NaN-gap tracker before the
                # dropout gate: a dropout-suppressed camera still sending
                # finite data is not a NaN gap.
                if self._nan_gap_tracker is not None:
                    self._nan_gap_tracker.record(_key, plot_ts)

                # Dropout gate — same logic as the old _on_uncorrected closure.
                should_emit, recovery_msg = _check_dropped_camera_emit(
                    side, cam_id,
                    connector._camera_dropped,
                    connector._camera_dropped_recovery_logged,
                )
                if recovery_msg is not None:
                    logger.warning(recovery_msg)
                if not should_emit:
                    continue

                # Update dropout-watchdog heartbeat (non-dark frames only, since
                # dark frames arrive at ~40x lower rate and would skew the timer).
                if not is_dark:
                    connector._camera_last_seen[_key] = now_mono
                    connector._camera_last_temp[_key] = temp_c

                # Temperature alert (light frames only — dark frames have no
                # meaningful camera temperature reading for display).
                if not is_dark and temp_c >= threshold and _key not in self._temp_alerted:
                    self._temp_alerted[_key] = True
                    msg = (
                        f"ALERT: Camera {cam_id + 1} ({side}) "
                        f"temperature {temp_c:.1f}°C >= {threshold:.0f}°C threshold."
                    )
                    connector.captureLog.emit(msg)
                    logger.warning(msg)

                mean_for_source: Optional[float] = None
                contrast_for_source: Optional[float] = None
                temp_for_source: Optional[float] = None
                if not is_dark:
                    # Record the realtime dark-corrected mean (mean_dc_rt) and
                    # shot-noise-corrected contrast (contrast_sn_rt). The
                    # live display is realtime-only by design; the accurate
                    # corrected record lands in the scan DB via the SDK's
                    # ScanDBSink and is what replay shows. Skip NaN samples —
                    # early light frames before the first dark observation
                    # have NaN mean_dc_rt and shouldn't poison the plot.
                    if batch.mean_dc_rt is not None:
                        mean_val = float(batch.mean_dc_rt[i, side_idx, cam_id])
                        if math.isfinite(mean_val):
                            mean_for_source = mean_val
                    if batch.contrast_sn_rt is not None:
                        contrast_val = float(batch.contrast_sn_rt[i, side_idx, cam_id])
                        if math.isfinite(contrast_val):
                            contrast_for_source = contrast_val
                    # Camera temperature — light frames only (dark-frame
                    # readings are meaningless for display, same rule as the
                    # temperature alert above). Feeds the per-cell dev-mode
                    # readout (issue #165).
                    if math.isfinite(temp_c):
                        temp_for_source = temp_c

                # mean/contrast/temp already filtered for is_dark /
                # non-finite above; None here means "metric not available
                # for this sample".
                self._live_source.append_uncorrected(
                    side=side,
                    cam_id=cam_id,
                    frame_id=abs_frame_id,
                    t=plot_ts,
                    bfi=bfi,
                    bvi=bvi,
                    mean=mean_for_source,
                    contrast=contrast_for_source,
                    temp=temp_for_source,
                )

    def _consume_side_avg(self, sample) -> None:
        """Append one reduced-mode per-side average (SideAverageSample from the
        SDK's LiveSideAverageStage) under cam_id=-1. The stage already emits one
        true spatial average per capture per side at ~40 Hz, so there's no dedup
        or skipping here — we just store what it emits."""
        side_idx = int(getattr(sample, "side", -1))
        if not (0 <= side_idx < len(_SIDE_NAMES)):
            return
        bfi = float(getattr(sample, "bfi", float("nan")))
        bvi = float(getattr(sample, "bvi", float("nan")))
        t = float(getattr(sample, "t", 0.0))
        # The side-average path stores NaN as-is (the renderer skips it),
        # so the tracker must gate on finiteness here — only finite
        # samples count as "data present".
        if (self._nan_gap_tracker is not None
                and math.isfinite(bfi) and math.isfinite(bvi)):
            self._nan_gap_tracker.record((_SIDE_NAMES[side_idx], -1), t)
        self._live_source.append_uncorrected(
            side=_SIDE_NAMES[side_idx],
            cam_id=-1,
            frame_id=int(getattr(sample, "frame_id", -1)),
            t=t,
            bfi=bfi,
            bvi=bvi,
        )

    def on_complete(self) -> None:
        pass


class _TriggerStateSink:
    """Listens for TriggerStateEvent on the diagnostics channel and mirrors
    the laser-trigger state into the connector so:

      * ``_trigger_state`` ("ON" / "OFF") drives the QML ``triggerState``
        property — which gates the scanTimer's `running:` binding on
        BloodFlow.qml, the per-scan camera-dropout watchdog, and any
        other QML element keyed on trigger status during a scan.
      * ``_trigger_on_mono`` / ``_trigger_cumulative_s`` give
        ``_scan_elapsed_str`` and the scan-notes "duration" line a real
        trigger-ON measurement rather than wall-clock.

    Without this sink, scan-time start_trigger() goes straight to the
    firmware via ScanWorkflow without touching the connector's
    ``_trigger_state``, so the timer never ticks.
    """

    channels: set = frozenset({"diagnostics"})

    def __init__(self, connector: "MotionConnector"):
        self._connector = connector

    def on_scan_start(self, meta) -> None:
        # Reset is already done in startCapture before the sink list is
        # constructed; nothing to do here.
        pass

    def consume(self, channel: str, payload) -> None:
        if channel != "diagnostics":
            return
        # Lazy-import the event type so this module doesn't fail if the
        # SDK version pre-dates TriggerStateEvent.
        try:
            from omotion.pipeline.batch import TriggerStateEvent
        except Exception:
            return
        if not isinstance(payload, TriggerStateEvent):
            return
        c = self._connector
        if payload.state == "ON":
            c._trigger_state = "ON"
            if c._trigger_on_mono is None:
                c._trigger_on_mono = time.monotonic()
            c.triggerStateChanged.emit()
        elif payload.state == "OFF":
            c._trigger_state = "OFF"
            if c._trigger_on_mono is not None:
                c._trigger_cumulative_s += time.monotonic() - c._trigger_on_mono
                c._trigger_on_mono = None
            c.triggerStateChanged.emit()

    def on_complete(self) -> None:
        pass


class _CompletionSink:
    """Fires the connector's post-scan UI cleanup when the pipeline's
    ScanRunner completes.  Replaces the legacy on_complete_fn callback.

    Wired by startCapture; the scan-done logic runs from the sink's
    on_complete() method.
    """

    channels: set = frozenset()  # no data channels — lifecycle only

    def __init__(self, connector: "MotionConnector", on_complete_cb):
        self._connector = connector
        self._on_complete_cb = on_complete_cb
        self._meta = None

    def on_scan_start(self, meta) -> None:
        self._meta = meta

    def consume(self, channel: str, payload) -> None:
        pass  # no data channels

    def on_complete(self) -> None:
        try:
            self._on_complete_cb(self._meta)
        except Exception:
            logger.exception("_CompletionSink.on_complete callback raised")


# ── Scan-outcome classification (interrupted / empty scans) ────────────────
# Kept Qt-free so it stays unit-testable (tests/test_scan_outcome.py). An
# "interval" is a dark-bounded segment the dark-correction stage emits on the
# pipeline's "final" channel; ScanDBSink persists those frames. The final,
# still-open interval is always discarded (no terminal dark closes it), so an
# unclean shutdown loses its tail — and a scan that ends before *any* interval
# closes persists nothing. classify_scan_outcome turns the two facts the sink
# observes (frames persisted, terminal-dark missing) into a user-facing
# outcome — no wall-clock thresholds.


class ScanOutcome(NamedTuple):
    kind: str       # "ok" | "partial" | "empty" | "skipped"
    severity: str   # "warning" | "error"  ("" when no alert)
    message: str    # "" when no alert should be shown


def classify_scan_outcome(
    *,
    final_frames: int,
    terminal_dark_missing: bool,
    canceled: bool,
    disable_laser: bool,
) -> ScanOutcome:
    """Decide what (if anything) to tell the user after a scan ends.

    - disable_laser scans legitimately produce no BFI/BVI → never alert.
    - final_frames <= 0:
        canceled  → user stopped before any interval closed; not an error.
        otherwise → interrupted before any data (e.g. device disconnect);
                    nothing was saved → error.
    - final_frames > 0:
        terminal_dark_missing and not canceled → final segment could not be
            corrected and was discarded → warning (partial save).
        otherwise → clean → no alert.
    """
    if disable_laser:
        return ScanOutcome("skipped", "", "")
    if final_frames <= 0:
        if canceled:
            return ScanOutcome("skipped", "", "")
        return ScanOutcome(
            "empty", "error",
            "Scan ended unexpectedly and no data was recorded (the device "
            "may have disconnected mid-scan). This scan was not saved.",
        )
    if terminal_dark_missing and not canceled:
        return ScanOutcome(
            "partial", "warning",
            "Scan ended unexpectedly — partial data was saved. The final "
            "segment could not be dark-corrected and was discarded.",
        )
    return ScanOutcome("ok", "", "")


class _ScanOutcomeSink:
    """Pipeline sink that tallies the two signals classify_scan_outcome needs.

    Duck-typed like the other sinks above (channels / on_scan_start / consume /
    on_complete). Holds no connector reference, so the completion handler reads
    .final_frames / .terminal_dark_missing directly off the instance.

      "final"       — corrected intervals (EnrichedCorrectedInterval). Each
                      carries .frames; summing their counts = corrected frames
                      persisted by ScanDBSink (same channel, same payloads).
      "diagnostics" — integrity events; a TerminalDarkResult with found=False
                      means a camera's terminal dark was missing/contaminated.
    """

    channels = frozenset({"final", "diagnostics"})

    def __init__(self) -> None:
        self.final_frames = 0
        self.terminal_dark_missing = False

    def on_scan_start(self, meta) -> None:
        self.final_frames = 0
        self.terminal_dark_missing = False

    def consume(self, channel: str, payload) -> None:
        if channel == "final":
            frames = getattr(payload, "frames", None)
            if frames:
                self.final_frames += len(frames)
            return
        if channel == "diagnostics":
            # Lazy-import the event type so this module loads against an SDK
            # that pre-dates TerminalDarkResult (mirrors _TriggerStateSink).
            try:
                from omotion.pipeline.batch import TerminalDarkResult
            except Exception:
                return
            if isinstance(payload, TerminalDarkResult) and not payload.found:
                self.terminal_dark_missing = True

    def on_complete(self) -> None:
        pass


class MotionConnector(QObject):
    # Ensure signals are correctly defined
    signalConnected = pyqtSignal(str, str)  # (descriptor, port)
    signalDisconnected = pyqtSignal(str, str)  # (descriptor, port)
    signalDataReceived = pyqtSignal(str, str)  # (descriptor, data)

    connectionStatusChanged = pyqtSignal()  # 🔹 New signal for connection updates
    stateChanged = pyqtSignal()  # Signal to notify QML of state changes
    laserStateChanged = pyqtSignal()  # Signal to notify QML of laser state changes
    safetyFailureStateChanged = pyqtSignal()  # Signal to notify QML of safety
    safetyTripDuringCaptureRequested = pyqtSignal()  # Emitted when safety trips while scan running (main-thread slot shows message & schedules cancel)
    triggerStateChanged = pyqtSignal()  # Signal to notify QML of trigger state changes
    directoryChanged = pyqtSignal()  # Signal to notify QML of directory changes
    userLabelChanged = pyqtSignal()  # Signal to notify QML of user label changes
    sensorDeviceInfoReceived = pyqtSignal(str, str)  # (fw_version, device_id)
    consoleDeviceInfoReceived = pyqtSignal(str, str)  # (fw_version, device_id)
    temperatureSensorUpdated = pyqtSignal(float)  # Temperature data
    accelerometerSensorUpdated = pyqtSignal(float, float, float)  # (x, y, z)
    gyroscopeSensorUpdated = pyqtSignal(float, float, float)  # (x, y, z)
    rgbStateReceived = pyqtSignal(int, str)  # (state, state_text)
    errorOccurred = pyqtSignal(str)
    notificationRequested = pyqtSignal('QVariant')  # toast notification payload dict
    notificationDismissByIdRequested = pyqtSignal(int)   # dismiss the toast with this id
    notificationDismissByTagRequested = pyqtSignal(str)  # dismiss the toast with this tag
    notificationDismissAllRequested = pyqtSignal()       # dismiss every active toast

    # Real-time plot viewer source — see data_sources.py.
    currentScanSourceChanged = pyqtSignal()
    liveSourceAvailableChanged = pyqtSignal()
    # History → "View in plot": fires on the GUI thread when an async
    # loadPastScan completes. (session_label, ok). HistoryModal uses it
    # to clear its busy overlay and close only on success (issue #152).
    pastScanLoadFinished = pyqtSignal(str, bool)
    # Private worker→main marshalling for loadPastScan results:
    # (seq, session_label, session_id, buffers-or-None, source_tag)
    _pastScanBuffersReady = pyqtSignal(int, str, int, object, str)
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

    # Calibration procedure signals
    calibrationStateChanged = pyqtSignal()  # any of running/passed/failed/aborted/idle
    _calibrationCompleteSignal = pyqtSignal(object)  # private worker→main marshalling
    testScanStateChanged = pyqtSignal()                # any of running/done/aborted/failed/idle
    _testScanCompleteSignal = pyqtSignal(object)       # private worker→main marshalling
    scanNotesChanged = pyqtSignal()
    # Fires once at the end of _on_complete (after the duration line has
    # been appended to _scan_notes and notes.txt has been written) for any
    # scan that finished normally or was canceled by the user. The UI uses
    # this to auto-open the notes modal at the only moment when scanNotes
    # is guaranteed to reflect the just-completed scan. Not emitted on
    # hard errors — those keep the scan dialog visible with the error.
    scanNotesReady = pyqtSignal()

    # Contact-quality quick-check signals.
    contactQualityCheckStarted = pyqtSignal(int)  # expected duration in seconds
    contactQualityCheckFinished = pyqtSignal(bool, str, 'QVariantList')  # ok, error, warnings
    contactQualityWarning = pyqtSignal(str, str, str, float)  # camera, typeKey, typeText, value
    # Mid-scan CQ state change (active=True on activation, False on clear).
    # BloodFlow.qml's ``onContactQualityIssueStateChanged`` consumes the
    # ``False`` edge to clear an entry from the live modal.
    contactQualityIssueStateChanged = pyqtSignal(str, str, str, float, bool)
    contactQualityScanInProgress = pyqtSignal(bool)
    cameraDropoutDetected = pyqtSignal(str, int, str)  # side ("left"/"right"), cam_id (0-7), elapsed HH:MM:SS

    # post-processing signals
    postProgress = pyqtSignal(int)
    postLog = pyqtSignal(str)
    postFinished = pyqtSignal(bool, str, str, str)  # ok, err, leftCsv, rightCsv

    pduMonChanged = pyqtSignal()

    tecStatusChanged = pyqtSignal()
    tecDacChanged = pyqtSignal()
    appConfigChanged = pyqtSignal()
    consoleFanChanged = pyqtSignal()

    # App update signals
    updateAvailable = pyqtSignal(str, str)   # (latest_version, download_url)
    updateNotAvailable = pyqtSignal()
    updateCheckFailed = pyqtSignal(str)      # error message

    @staticmethod
    def _default_data_dir() -> str:
        """Return a writable directory for logs and scan data.

        Uses the current working directory when it is writable (typical
        for development runs).  When cwd is read-only — e.g. ``/`` on
        macOS when the .app bundle is launched from Finder — falls back
        to ``~/Documents/OpenWater Bloodflow``.
        """
        cwd = os.getcwd()
        if os.access(cwd, os.W_OK):
            return cwd
        return os.path.join(
            os.path.expanduser("~"), "Documents", "OpenWater Bloodflow"
        )

    def __init__(
        self,
        interface: MotionInterface,
        app_config=None,
        data_dir=None,
        config_dir="config",
        parent=None,
        log_level=logging.INFO,
    ):
        super().__init__(parent)
        cfg = app_config or {}

        # Store the full config dict — exposed to QML as appConfig property
        self._app_config = dict(cfg)

        self._interface = interface
        self._scan_workflow = self._interface.scan_workflow

        # Unpack operational settings from config
        self._force_laser_fail            = bool(cfg.get("forceLaserFail", False))
        self._camera_temp_alert_threshold_c = float(cfg.get("cameraTempAlertThresholdC", 105.0))
        self._camera_dropout_threshold_sec = float(cfg.get("cameraDropoutThresholdSec", 2.0))

        # Camera dropout watchdog state — reset at start of each scan.
        self._camera_last_seen: dict[tuple[str, int], float] = {}
        self._camera_last_temp: dict[tuple[str, int], float] = {}
        self._camera_dropped: set[tuple[str, int]] = set()
        # Tracks dropped-camera keys we've already surfaced a 'sent
        # data after Connection Lost' warning for. One log per dropout
        # — at 40 Hz a flapping camera would otherwise spam the logs.
        self._camera_dropped_recovery_logged: set[tuple[str, int]] = set()

        # NaN-gap tracker — replaced with a fresh instance at each scan
        # start. Kept on the instance for debugging/introspection only;
        # the scan path (sink + completion closure in startCapture) uses
        # a closure-local binding of the same object.
        self._nan_gap_tracker = NanGapTracker()

        # 1 Hz watchdog timer — started/stopped around the scan lifecycle.
        self._dropout_timer = QTimer(self)
        self._dropout_timer.setInterval(1000)
        self._dropout_timer.timeout.connect(self._on_dropout_check)
        self._plot_t0: float = 0.0  # set at scan start; consumed by _on_dropout_check

        # Trigger-ON elapsed mirrors — populated from start_capture locals so
        # _on_dropout_check / _scan_elapsed_str can read them off the instance.
        self._trigger_cumulative_s: float = 0.0
        self._trigger_on_mono: float | None = None
        self._sensor_debug_logging        = bool(cfg.get("sensorDebugLogging", False))
        self._camera_fake_data            = bool(cfg.get("cameraFakeData", False))
        self._histo_throttle              = bool(cfg.get("histoThrottle", False))
        self._histo_cmp                   = bool(cfg.get("histoCmp", False))
        self._comm_verbose                = bool(cfg.get("commVerbose", False))
        self._verbose_command_handling    = bool(cfg.get("verboseCommandHandling", False))
        # Single output root: caller-supplied (from main.py) wins, else
        # dataDirectory from app config, else default (cwd or ~/Documents).
        # All sub-paths (app-logs, scan files, scans.db,
        # ft-test-csvs) live under self._directory.
        resolved_dir = data_dir or cfg.get("dataDirectory") or self._default_data_dir()
        self._power_off_unused_cameras    = bool(cfg.get("powerOffUnusedCameras", False))
        # Raw CSV is a developer feature (Settings → Developer → "Save raw
        # CSV"); default False so a config missing the key fails closed for
        # clinical use (#43).
        self._write_raw_csv               = bool(cfg.get("writeRawCsv", False))
        raw_csv                           = cfg.get("rawCsvDurationSec")
        self._raw_csv_duration_sec        = float(raw_csv) if raw_csv is not None else None
        self._uncorrected_only            = bool(cfg.get("uncorrectedOnly", False))

        # Initialize CSV output directory to user's home directory
        self._csv_output_directory = os.path.expanduser("~")

        # Check if console and sensor are connected
        console_connected, left_sensor_connected, right_sensor_connected = (
            self._interface.is_device_connected()
        )

        self._leftSensorConnected = left_sensor_connected
        self._rightSensorConnected = right_sensor_connected
        self._consoleConnected = console_connected
        self._config_running = False
        self._laserOn = False
        self._safetyFailure = False
        self._safety_unknown_streak = 0  # see SAFETY_UNKNOWN_STREAK_THRESHOLD
        self._running = False
        self._trigger_state = "OFF"
        self._state = DISCONNECTED
        self._last_fan_status: dict[str, bool | None] = {"left": None, "right": None}
        # Console fan on/off cache. Seeded True because the connector
        # forces set_fan_speed(100) at console-connect time (see the
        # connect handler below). Surfaced to QML as ``consoleFanOn`` and
        # toggled via ``setConsoleFan``.
        self._console_fan_on: bool = True
        # Track console connection time for safety grace period (issue #107 follow-up)
        self._console_connected_at: float | None = None
        # Real-time plot viewer source — assigned at scan start by startCapture.
        self._current_scan_source: ScanDataSource | None = None
        # The most-recent LiveScanSource is kept alive even after the user
        # navigates to a past scan, so "Back to live" can reassign without
        # reconstructing. None when no scan has run this session.
        self._live_scan_source: LiveScanSource | None = None
        # Monotonic token for async past-scan loads — a result whose seq
        # doesn't match the latest request is stale and dropped.
        self._past_scan_load_seq = 0

        self._tec_voltage_default = load_tec_params(config_dir)
        self._console_mutex = QRecursiveMutex()

        ft_mean     = cfg.get("ft_min_mean_per_camera")
        ft_contrast = cfg.get("ft_min_contrast_per_camera")
        self._ft_min_mean_per_camera     = list(ft_mean)     if isinstance(ft_mean,     (list, tuple)) else None
        self._ft_min_contrast_per_camera = list(ft_contrast) if isinstance(ft_contrast, (list, tuple)) else None

        ft_bfi      = cfg.get("ft_min_bfi_per_camera")
        ft_bfi_max  = cfg.get("ft_max_bfi_per_camera")
        ft_bvi      = cfg.get("ft_min_bvi_per_camera")
        ft_bvi_max  = cfg.get("ft_max_bvi_per_camera")
        self._ft_min_bfi_per_camera = list(ft_bfi)     if isinstance(ft_bfi,     (list, tuple)) else None
        self._ft_max_bfi_per_camera = list(ft_bfi_max) if isinstance(ft_bfi_max, (list, tuple)) else None
        self._ft_min_bvi_per_camera = list(ft_bvi)     if isinstance(ft_bvi,     (list, tuple)) else None
        self._ft_max_bvi_per_camera = list(ft_bvi_max) if isinstance(ft_bvi_max, (list, tuple)) else None
        # #122: per-camera max dark-frame mean — gates FT calibration on
        # ambient light leaking into the validation scan's dark frames.
        ft_dark_max = cfg.get("ft_max_dark_per_camera")
        self._ft_max_dark_per_camera = (
            list(ft_dark_max) if isinstance(ft_dark_max, (list, tuple)) else None
        )
        self._max_calibration_time_sec     = int(cfg.get("max_calibration_time_sec", 600))
        self._calibration_scan_duration_sec = int(cfg.get("calibration_scan_duration_sec", 5))
        self._calibration_scan_delay_sec    = int(cfg.get("calibration_scan_delay_sec", 1))
        self._test_scan_duration_sec = int(
            cfg.get("test_scan_duration_sec", 5)
        )
        self._calibration_status = ""  # "", "running", "passed", "failed", "aborted"
        self._calibration_failure_reason = ""  # populated only on FAIL in dev mode
        self._test_scan_status = ""              # "", "running", "done", "aborted", "failed"
        self._test_scan_failure_reason = ""
        self._test_scan_rows: list[dict] = []

        self._post_thread = None
        self._post_cancel = threading.Event()

        self._capture_thread = None
        self._capture_stop = threading.Event()
        self._capture_running = False
        self._cq_quick_running = False
        self._notification_id_counter = 0  # monotonic id assigned to each notify() call
        self._safety_cancel_scheduled = False  # True after scheduling cancel-due-to-safety; cleared when capture ends
        self._capture_left_path = ""
        self._capture_right_path = ""
        self._scan_notes = ""
        # session_label of the just-completed scan's DB session — notes edits
        # persist there (sessions.session_notes); empty until a scan completes.
        self._scan_notes_session_label = ""
        self.connect_signals()

        self._tec_voltage = 0.0
        self._tec_temp = 0.0
        self._tec_monV = 0.0
        self._tec_monC = 0.0
        self._tec_good = False

        self._tec_dac = 0.0

        self._pdu_raws = [0] * 16
        self._pdu_vals = [0.0] * 16

        # Start times for the scan start/end log banners (monotonic, or
        # None when the flow has never run).
        self._calibration_t0 = None
        self._test_scan_t0 = None
        self._cq_t0 = None

        os.makedirs(resolved_dir, exist_ok=True)
        self._directory = resolved_dir

        # ── Audit log: append-only, machine-readable event record in
        #    scans.db, surfaced via the password-gated Logs modal. See
        #    audit_log.py. No-op if the interface has no scan_db_path.
        from audit_log import AuditLog, gather_host_info
        self._audit = AuditLog(getattr(self._interface, "scan_db_path", None))
        try:
            from version import get_version as _get_app_version
            _app_version = _get_app_version()
        except Exception:
            _app_version = ""
        try:
            _sdk_version = self._interface.get_sdk_version()
            _sdk_version = (
                _sdk_version if isinstance(_sdk_version, str) else ""
            )
        except Exception:
            _sdk_version = ""
        self._audit.log("system_startup", {
            "app_version": _app_version,
            "sdk_version": _sdk_version,
            "data_dir": self._directory,
        })
        self._audit.log("system_info", gather_host_info())
        logger.info(f"[Connector] Directory initialized to: {self._directory}")

        self._user_label = self.generate_user_label()
        logger.info(f"[Connector] Generated user label: {self._user_label}")

        # Note: synthetic startup connect events for already-attached
        # devices are no longer needed. The new SDK lifecycle is:
        #   1. main.py constructs MotionInterface
        #   2. main.py constructs MotionConnector (this); we subscribe
        #      to handle.signal_state_changed in connect_signals()
        #   3. main.py calls motion_interface.start(), which discovers
        #      devices and drives state-machine transitions, firing the
        #      signals we already subscribed to.
        # So every real connection arrives via _on_handle_state_changed.

        self._interface.console.telemetry.add_listener(self._on_telemetry_update)

    def set_ft_thresholds(
        self,
        min_mean_per_camera=None,
        min_contrast_per_camera=None,
    ):
        """Set FT thresholds per camera (index 0-7). None or list of up to 8 numbers."""
        self._ft_min_mean_per_camera = (
            min_mean_per_camera
            if isinstance(min_mean_per_camera, (list, tuple))
            else None
        )
        self._ft_min_contrast_per_camera = (
            min_contrast_per_camera
            if isinstance(min_contrast_per_camera, (list, tuple))
            else None
        )

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
        return flags

    def _schedule_sensor_init(self, side: str):
        """Delay initial sensor commands to allow USB settle."""
        QTimer.singleShot(1000, lambda: self._run_sensor_init(side))

    def _run_sensor_init(self, side: str):
        if side == "left" and not self._leftSensorConnected:
            return
        if side == "right" and not self._rightSensorConnected:
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
                "commVerbose=%s, verboseCommand=%s)",
                flags,
                side,
                self._sensor_debug_logging,
                self._camera_fake_data,
                getattr(self, "_histo_throttle", False),
                getattr(self, "_histo_cmp", False),
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
                        refresh_cache()  # try anyway in case some cameras are already on
                elif refresh_cache:
                    refresh_cache()  # fallback: fill cache without power cycle (may get zeros for off cameras)
        except Exception as e:
            logger.debug("Could not refresh sensor ID cache for %s: %s", side, e)

        # Log camera UIDs once per sensor connect (the cache above just
        # filled), instead of re-logging them at every scan start.
        self._read_and_log_camera_uids(side)

        self.connectionStatusChanged.emit()

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

    @pyqtProperty(bool, notify=consoleFanChanged)
    def consoleFanOn(self) -> bool:
        """Cached last-set console-fan state (True=on). Reflects the
        connect-time 100% default and any setConsoleFan call. Read by the
        Developer settings switch when the Settings modal opens."""
        return self._console_fan_on

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

    @pyqtProperty(bool, notify=laserStateChanged)
    def laserOn(self):
        """Expose Console connection status to QML."""
        return self._laserOn

    @pyqtProperty(bool, notify=safetyFailureStateChanged)
    def safetyFailure(self):
        """Expose Console connection status to QML."""
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
        ``appConfig.developerMode`` is enabled, so end users see a
        friendly message and developers see which fault bits tripped.
        Tagged ``laser_safety`` so re-fires replace rather than stack.
        """
        msg = (
            "Laser safety warning detected. Please restart your "
            "console. If this error persists, please contact support."
        )
        if fault_detail and self._app_config.get("developerMode", False):
            msg += f"\n[dev] {fault_detail}"
        self.notify(
            msg,
            type_="error",
            duration_ms=0,
            dismissible=False,
            tag="laser_safety",
        )

    @pyqtProperty(int, notify=stateChanged)
    def state(self):
        """Expose state as a QML property."""
        return self._state

    @pyqtProperty(str, notify=triggerStateChanged)
    def triggerState(self):
        return self._trigger_state

    # --- Calibration procedure properties (consumed by Settings.qml) ---
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
            # Top-level safety net: this slot is invoked from the SDK
            # connection-monitor thread via Qt signals. Any uncaught
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

        if name == "console":
            self._consoleConnected = is_now_connected
            if is_now_connected:
                # Record connection time for safety grace period
                self._console_connected_at = time.monotonic()
                # Race-guard: by the time this slot fires we observe
                # CONNECTED, but the console can disconnect again before
                # any of these calls return — particularly during the
                # post-power-cycle reconnect storm where a single
                # PermissionError on the COM port immediately drives
                # CONNECTED -> DISCONNECTING. Without the try/except,
                # tec_voltage / set_fan_speed raise
                # "ValueError: Motion Console not connected" out of the
                # Qt slot, which propagates as an unhandled Python
                # exception and terminates the app process.
                try:
                    self._interface.log_console_info()
                    if self._interface.console.tec_voltage(self._tec_voltage_default):
                        logger.info(f"Console TEC voltage set to {self._tec_voltage_default}V")
                    else:
                        logger.error(
                            f"Failed to set console TEC voltage to {self._tec_voltage_default}V"
                        )
                    if self._interface.console.set_fan_speed(fan_speed=100):
                        logger.info("Console fan speed set to 100%")
                        self._console_fan_on = True
                        self.consoleFanChanged.emit()
                    else:
                        logger.error("Failed to set console fan speed")
                    # Apply laser-power params once per console connect —
                    # the FPGA registers are volatile across power cycles,
                    # so every (re)connect needs them. Scans no longer
                    # re-apply per run (was SetTriggerLaserTask + the
                    # issue-#108 cold-start guards).
                    if self.set_laser_power_from_config(self._interface):
                        logger.info("Laser power params applied from config")
                    else:
                        logger.error("Failed to apply laser power params from config")
                except Exception as e:
                    logger.warning(
                        f"Console connect-time setup interrupted "
                        f"(probably mid-flight disconnect): {e}"
                    )
            elif is_now_lost:
                # Clear connection timestamp on disconnect
                self._console_connected_at = None
        elif name == "left":
            if is_now_connected:
                self._leftSensorConnected = True
                self._schedule_sensor_init("left")
            elif is_now_lost:
                self._leftSensorConnected = False
                self._last_fan_status["left"] = None
                try:
                    if getattr(self._interface.left, "clear_id_cache", None):
                        self._interface.left.clear_id_cache()
                except Exception:
                    pass
        elif name == "right":
            if is_now_connected:
                self._rightSensorConnected = True
                self._schedule_sensor_init("right")
            elif is_now_lost:
                self._rightSensorConnected = False
                self._last_fan_status["right"] = None
                try:
                    if getattr(self._interface.right, "clear_id_cache", None):
                        self._interface.right.clear_id_cache()
                except Exception:
                    pass

        if is_now_connected:
            logger.info("Handle %s -> CONNECTED (%s)", name, reason)
            self.signalConnected.emit(name, "")
        elif is_now_lost:
            logger.info(
                "Handle %s -> DISCONNECTED (%s) and state is %s",
                name, reason, self._state,
            )
            self.signalDisconnected.emit(name, "")
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
        # CONNECTING / DISCONNECTING are intermediate; UI doesn't need to
        # fire a connect/disconnect signal for those, and emitting
        # connectionStatusChanged on every intermediate transition would
        # cause QML observers to re-poll device state 4× per disconnect
        # cycle (once for each of CONNECTED→DISCONNECTING→DISCONNECTED→
        # CONNECTING→CONNECTED).
        if is_now_connected or is_now_lost:
            self.connectionStatusChanged.emit()
            self.update_state()

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

    @pyqtSlot(str)
    def handleUpdateCapStatus(self, status_msg: str):
        logger.debug(f"Console status update: {status_msg}")

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

    @pyqtSlot()
    def shutdown(self):
        """Shutdown connector. Stops any capture in flight; main.py's
        handle_exit stops the MotionInterface (monitoring + device
        teardown) right after this returns."""
        logger.info("Shutting down MotionConnector...")
        self.stopCapture()
        try:
            self._audit.log("system_shutdown", {"clean": True})
        except Exception:
            logger.warning("audit shutdown log failed", exc_info=True)
        logger.info("MotionConnector shutdown complete.")

    # --- SCAN MANAGEMENT METHODS ---
    @pyqtSlot(result=list)
    def _load_tec_params(self, config_dir):
        """Load TEC parameters from tec_params.json and return the voltage value."""
        config_path = (
            resource_path("config", "tec_params.json")
            if config_dir == "config"
            else Path(config_dir) / "tec_params.json"
        )

        if not config_path.exists():
            logger.warning(
                f"[Connector] TEC parameter file not found: {config_path}, using default value {TEC_VOLTAGE_DEFAULT}V"
            )
            return TEC_VOLTAGE_DEFAULT

        try:
            with open(config_path, "r") as f:
                params = json.load(f)
            voltage = params.get("TEC_VOLTAGE_DEFAULT", TEC_VOLTAGE_DEFAULT)
            logger.info(
                f"[Connector] Loaded TEC voltage from {config_path}: {voltage}V"
            )
            return voltage
        except FileNotFoundError:
            logger.warning(
                f"[Connector] TEC parameter file not found: {config_path}, using default value {TEC_VOLTAGE_DEFAULT}V"
            )
            return TEC_VOLTAGE_DEFAULT
        except json.JSONDecodeError as e:
            logger.error(
                f"[Connector] Invalid JSON in {config_path}: {e}, using default value {TEC_VOLTAGE_DEFAULT}V"
            )
            return TEC_VOLTAGE_DEFAULT
        except Exception as e:
            logger.error(
                f"[Connector] Error loading TEC parameters: {e}, using default value {TEC_VOLTAGE_DEFAULT}V"
            )
            return TEC_VOLTAGE_DEFAULT

    # Suffix patterns that distinguish the corrected (canonical) CSV
    # from per-scan auxiliary CSVs (raw histo, telemetry). Issue #44:
    # the canonical file dropped its ``_corrected`` suffix so the
    # previous ``*_corrected.csv`` glob no longer matches new scans.
    # Discovery now globs all CSVs and excludes anything matching one
    # of these aux patterns. Order matters: longer / more-specific
    # patterns first so an aux file isn't mistaken for canonical.
    _AUX_CSV_RE = re.compile(
        r"_("
        r"telemetry"                                  # _telemetry.csv
        r"|(?:left|right)_mask[0-9A-Fa-f]+(?:_raw)?"  # raw histo, new + legacy
        r")$"
    )

    @pyqtSlot(result=list)
    def get_scan_list(self):
        """Return sorted list of scan IDs from BOTH the corrected CSVs on disk
        and the scan database's sessions.

        The scan DB is the system of record: reduced-mode scans (and any scan
        with writeCorrectedCsv off) write no corrected CSV, so they exist only
        as DB sessions. A session_label has the same shape as the CSV-derived
        scan id (``YYYYMMDD_HHMMSS_userLabel``), so the two sources merge by id.

        CSV filename formats supported (legacy / corrected-CSV scans):
          New (post-#44): {YYYYMMDD_HHMMSS}_{sessionId}.csv
          Mid:            {YYYYMMDD_HHMMSS}_{sessionId}_corrected.csv
          Legacy:         scan_{sessionId}_{YYYYMMDD_HHMMSS}_corrected.csv
        """
        seen: set[str] = set()
        ids: list[str] = []

        base_path = Path(self._directory)
        if base_path.exists():
            for f in base_path.glob("*.csv"):
                if not f.is_file():
                    continue
                stem = f.stem
                # Skip per-scan auxiliary files (raw histo, telemetry).
                if self._AUX_CSV_RE.search(stem):
                    continue
                # Mid format: strip the ``_corrected`` suffix to get the
                # canonical scan id.
                if stem.endswith("_corrected"):
                    stem = stem[:-10]
                # Legacy format: ``scan_{sessionId}_{ts}`` — strip the
                # ``scan_`` prefix.
                if stem.startswith("scan_"):
                    stem = stem[5:]
                if stem in seen:
                    continue
                seen.add(stem)
                ids.append(stem)

        # DB-backed scans (no corrected CSV on disk). Best-effort: a missing or
        # unreadable DB just leaves the CSV-derived list.
        db_path = getattr(self._interface, "scan_db_path", None)
        if db_path:
            try:
                from omotion.ScanDatabase import ScanDatabase
                db = ScanDatabase(db_path)
                try:
                    for session in db.iter_sessions():
                        label = (session.get("session_label") or "").strip()
                        if label and label not in seen:
                            seen.add(label)
                            ids.append(label)
                finally:
                    db.close()
            except Exception:
                logger.warning("get_scan_list: could not read scan DB sessions",
                               exc_info=True)

        def ts_key(s):
            # New / mid format starts with YYYYMMDD (8 digits)
            if re.match(r'^\d{8}_\d{6}', s):
                return s[:15]       # YYYYMMDD_HHMMSS
            # Legacy format: sessionId_YYYYMMDD_HHMMSS
            parts = s.split("_", 1)
            return parts[1] if len(parts) == 2 else s

        return sorted(ids, key=ts_key, reverse=True)

    @pyqtSlot(str, result=QVariant)
    def get_scan_details(self, scan_id: str):
        """
        scan_id is either:
          New / mid format: 'YYYYMMDD_HHMMSS_userLabel'
          Legacy format:    'userLabel_YYYYMMDD_HHMMSS'

        For each format we try to resolve the canonical CSV across
        all naming generations (#44):
          New:    {scan_id}.csv
          Mid:    {scan_id}_corrected.csv
          Legacy: scan_{scan_id}_corrected.csv
        And the raw histo CSVs across two generations:
          New:    {scan_id}_(left|right)_mask*_raw.csv
          Legacy: {scan_id}_(left|right)_mask*.csv
        """
        base = Path(self._directory)

        # Detect format by checking if it starts with a date
        if re.match(r'^\d{8}_\d{6}_', scan_id):
            # New / mid: YYYYMMDD_HHMMSS_userLabel
            parts = scan_id.split("_", 2)
            ts = parts[0] + "_" + parts[1]
            subject = parts[2] if len(parts) > 2 else ""
            notes_path = base / f"{scan_id}_notes.txt"
            left      = (next(base.glob(f"{scan_id}_left_mask*_raw.csv"), None)
                         or next(base.glob(f"{scan_id}_left_mask*.csv"), None))
            right     = (next(base.glob(f"{scan_id}_right_mask*_raw.csv"), None)
                         or next(base.glob(f"{scan_id}_right_mask*.csv"), None))
            # Prefer new naming (no suffix); fall back to mid format
            # (_corrected). Filter out files that match the raw
            # mask pattern so they aren't picked up as the canonical
            # CSV.
            corrected = next(
                (p for p in base.glob(f"{scan_id}.csv") if p.is_file()),
                None,
            )
            if corrected is None:
                corrected = next(base.glob(f"{scan_id}_corrected.csv"), None)
        else:
            # Legacy: userLabel_YYYYMMDD_HHMMSS
            parts = scan_id.split("_", 1)
            subject = parts[0]
            ts = parts[1] if len(parts) > 1 else ""
            notes_path = base / f"scan_{scan_id}_notes.txt"
            left      = next(base.glob(f"scan_{scan_id}_left_mask*.csv"), None)
            right     = next(base.glob(f"scan_{scan_id}_right_mask*.csv"), None)
            corrected = next(base.glob(f"scan_{scan_id}_corrected.csv"), None)

        left_mask = ""
        right_mask = ""
        if left:
            m = re.search(r"_mask([0-9A-Fa-f]+)(?:_raw)?\.csv$", left.name)
            if m:
                left_mask = m.group(1)
        if right:
            m = re.search(r"_mask([0-9A-Fa-f]+)(?:_raw)?\.csv$", right.name)
            if m:
                right_mask = m.group(1)

        # Notes live in the scan DB (sessions.session_notes, keyed by
        # session_label == scan_id). Scans from before the DB migration
        # only have a *_notes.txt on disk — fall back to that.
        notes = ""
        has_db_rows = False
        db_path = getattr(self._interface, "scan_db_path", None)
        if db_path:
            try:
                from omotion.ScanDatabase import ScanDatabase
                db = ScanDatabase(db_path)
                try:
                    session = db.get_session_by_label(scan_id)
                    if session and session.get("session_notes"):
                        notes = session["session_notes"]
                    if session:
                        row = next(
                            db._connection().execute(
                                "SELECT EXISTS(SELECT 1 FROM session_data "
                                "WHERE session_id = ? LIMIT 1)",
                                (int(session["id"]),),
                            ),
                            None,
                        )
                        has_db_rows = bool(row[0]) if row else False
                finally:
                    db.close()
            except Exception:
                logger.warning("get_scan_details: could not read notes/rows from DB",
                               exc_info=True)
        if not notes:
            try:
                notes = notes_path.read_text(encoding="utf-8")
            except Exception:
                pass

        return {
            "userLabel": subject,
            "sessionId": f"{ts}_{subject}",
            "timestamp": ts,
            "leftMask": left_mask,
            "rightMask": right_mask,
            "leftPath": str(left) if left else "",
            "rightPath": str(right) if right else "",
            "correctedPath": str(corrected) if corrected else "",
            "notesPath": str(notes_path),
            "notes": notes,
            "hasData": bool(has_db_rows or corrected or left or right),
        }

    @staticmethod
    def _friendly_ts(ts: str) -> str:
        """'YYYYMMDD_HHMMSS' -> 'YYYY-MM-DD HH:MM:SS'; pass through."""
        if not ts or len(ts) != 15:
            return ts or "-"
        return (f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} "
                f"{ts[9:11]}:{ts[11:13]}:{ts[13:15]}")

    def _session_to_row(self, s: dict) -> dict:
        """Flatten one ScanDatabase session dict into the QVariantMap the
        History table consumes. Masks/operator come from session_meta
        (written by ScanDBSink); duration from session_start/end."""
        label = (s.get("session_label") or "").strip()
        meta = s.get("session_meta")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        flags = meta.get("sdk_flags") or {}

        left_mask = flags.get("left_camera_mask", -1)
        right_mask = flags.get("right_camera_mask", -1)
        if left_mask is None:
            left_mask = -1
        if right_mask is None:
            right_mask = -1

        user_label = meta.get("subject_id") or ""
        timestamp = ""
        m = re.match(r"^(\d{8}_\d{6})_?(.*)$", label)
        if m:
            timestamp = m.group(1)
            if not user_label:
                user_label = m.group(2)

        start = s.get("session_start")
        end = s.get("session_end")
        if start is not None and end is not None:
            duration = float(end) - float(start)
        else:
            duration = -1.0

        return {
            "sessionId": int(s.get("id")),
            "label": label,
            "userLabel": user_label,
            "operator": meta.get("operator") or "",
            "timestamp": timestamp or label,
            "dateTime": self._friendly_ts(timestamp),
            "durationSec": duration,
            "leftMask": int(left_mask),
            "rightMask": int(right_mask),
            "configL": _config_name(left_mask),
            "configR": _config_name(right_mask),
            "reducedMode": bool(flags.get("reduced_mode", False)),
            "notes": s.get("session_notes") or "",
            "interrupted": end is None,
        }

    @pyqtSlot(result="QVariantList")
    def get_scan_sessions(self):
        """Return one row per scan-DB session, newest first, for the
        History table. DB-only — does not list CSV-derived scans.
        Best-effort: a missing/unreadable DB yields []."""
        db_path = getattr(self._interface, "scan_db_path", None)
        if not db_path:
            return []
        rows = []
        try:
            from omotion.ScanDatabase import ScanDatabase
            db = ScanDatabase(db_path)
            try:
                for s in db.iter_sessions():
                    # Per-row guard: one malformed session must not blank the
                    # whole History view — skip it and keep the rest.
                    try:
                        rows.append(self._session_to_row(s))
                    except Exception:
                        logger.warning(
                            "get_scan_sessions: skipping malformed session %s",
                            s.get("id"), exc_info=True)
            finally:
                db.close()
        except Exception:
            logger.warning("get_scan_sessions: could not read scan DB",
                           exc_info=True)
            return []
        rows.sort(key=lambda r: r["timestamp"], reverse=True)
        return rows

    @pyqtSlot(int, result="QVariantMap")
    def get_session_stats(self, session_id: int):
        """Lazily fetch heavier per-scan stats (row count) when a History
        row is focused, so the list query itself stays cheap."""
        db_path = getattr(self._interface, "scan_db_path", None)
        if not db_path:
            return {"sampleCount": 0}
        try:
            from omotion.ScanDatabase import ScanDatabase
            db = ScanDatabase(db_path)
            try:
                # Raw COUNT via the connection — ScanDatabase exposes no
                # public row-count API; get_scan_details reaches for
                # _connection() the same way for its EXISTS probe.
                row = next(
                    db._connection().execute(
                        "SELECT COUNT(*) FROM session_data"
                        " WHERE session_id = ?",
                        (int(session_id),),
                    ),
                    None,
                )
                return {"sampleCount": int(row[0]) if row else 0}
            finally:
                db.close()
        except Exception:
            logger.warning(
                "get_session_stats failed for %s", session_id,
                exc_info=True)
            return {"sampleCount": 0}

    @pyqtSlot("QVariantList", result=int)
    def deleteScans(self, session_ids):
        """Delete the given scan-DB sessions (CASCADE removes their
        session_data). Returns the count actually deleted. The developer-
        password gate is enforced in QML before this is called."""
        db_path = getattr(self._interface, "scan_db_path", None)
        if not db_path:
            return 0
        deleted = 0
        try:
            from omotion.ScanDatabase import ScanDatabase
            db = ScanDatabase(db_path)
            try:
                for sid in session_ids:
                    try:
                        if db.delete_session(int(sid)):
                            deleted += 1
                            logger.info(
                                "deleteScans: removed session %s", sid)
                    except Exception:
                        logger.warning(
                            "deleteScans: failed to delete %s", sid,
                            exc_info=True)
            finally:
                db.close()
        except Exception:
            logger.warning(
                "deleteScans: could not open scan DB", exc_info=True)
        return deleted

    # ── Audit log (QML-facing) ───────────────────────────────────────────
    @pyqtSlot(result="QVariantList")
    @pyqtSlot(int, result="QVariantList")
    def auditLogEntries(self, limit: int = 500):
        """Audit-log rows (newest first) for the Logs modal. Pure read —
        does not itself log, so refreshing never double-logs."""
        try:
            return self._audit.query(int(limit))
        except Exception:
            logger.warning("auditLogEntries failed", exc_info=True)
            return []

    @pyqtSlot()
    def recordAuditLogViewed(self):
        """Record that the audit log was opened. Called once from
        LogsModal.open()."""
        try:
            n = len(self._audit.query())
        except Exception:
            n = 0
        self._audit.log("audit_log_viewed", {"entry_count": n})

    @pyqtSlot(str, result=str)
    def exportAuditLogCsv(self, dest_path: str) -> str:
        """Export the full audit log to CSV. Accepts a plain path or a
        file:// URL. Records an ``audit_log_exported`` event. Returns the
        written path, or '' on failure."""
        if not dest_path:
            return ""
        path = dest_path.replace("file:///", "").replace("file://", "")
        try:
            n = self._audit.export_csv(path)
            self._audit.log(
                "audit_log_exported", {"dest": path, "row_count": n}
            )
            logger.info("exportAuditLogCsv: wrote %d rows -> %s", n, path)
            return path
        except Exception:
            logger.exception("exportAuditLogCsv failed for %r", path)
            self.errorOccurred.emit("Audit log export failed.")
            return ""

    @pyqtProperty(str, notify=directoryChanged)
    def directory(self):
        return self._directory

    @directory.setter
    def directory(self, path):
        # Normalize incoming QML "file:///" path
        if path.startswith("file:///"):
            path = path[8:] if path[9] != ":" else path[8:]
        self._directory = path
        self._app_config["dataDirectory"] = path
        self._save_app_config()
        logger.debug(f"[Connector] Directory set to: {self._directory}")
        self.directoryChanged.emit()
        self.appConfigChanged.emit()

    # ── App config — generic read/write API ──────────────────────────────────

    @pyqtProperty('QVariantMap', notify=appConfigChanged)
    def appConfig(self):
        return self._app_config

    # Config keys that must always be stored as plain integers
    _INT_CONFIG_KEYS = {"leftMask", "rightMask"}

    def _save_app_config(self):
        """Write the in-memory config dict back to app_config.json."""
        config_path = resource_path("config", "app_config.json")
        # Coerce mask fields to int — QML passes JS numbers as Python float
        out = dict(self._app_config)
        for key in self._INT_CONFIG_KEYS:
            if key in out and out[key] is not None:
                out[key] = int(out[key])
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(out, f, indent=2)
        except OSError as e:
            logger.warning(f"[Connector] Could not write app_config.json: {e}")

    @pyqtSlot(str, result=bool)
    def checkDeveloperPassword(self, pw: str) -> bool:
        """Return True if ``pw`` matches the developer-mode password.

        Comparison lives in Python so the literal is not present in
        shipped QML source. QML calls this from the unlock modal and,
        on True, sets developerMode via setConfig.
        """
        ok = developer_password_matches(pw)
        if not ok:
            logger.info("[Connector] Developer unlock attempt failed")
        return ok

    @pyqtSlot(str, 'QVariant')
    def setConfig(self, key: str, value):
        """Update a single config key, persist to disk, and notify QML."""
        self._app_config[key] = value
        self._save_app_config()
        self.appConfigChanged.emit()
        logger.debug(f"[Connector] Config set: {key} = {value!r}")

    @pyqtSlot('QVariantMap')
    def saveConfigs(self, configs: dict):
        """Update multiple config keys at once, persist to disk, and notify QML."""
        self._app_config.update(configs)
        self._save_app_config()
        self.appConfigChanged.emit()
        logger.debug(f"[Connector] Config saved: {sorted(configs.keys())}")

    @pyqtSlot(bool)
    def setWriteRawCsv(self, enabled: bool) -> None:
        """Update writeRawCsv in both the runtime cache and persisted config."""
        self._write_raw_csv = bool(enabled)
        self._app_config["writeRawCsv"] = self._write_raw_csv
        self._save_app_config()
        self.appConfigChanged.emit()
        logger.debug(f"[Connector] writeRawCsv set to {self._write_raw_csv}")

    @pyqtSlot('QVariant')
    def setRawCsvDurationSec(self, value) -> None:
        """Update rawCsvDurationSec in both the runtime cache and persisted config.

        Pass ``None`` / ``null`` / empty string to disable the limit (full scan duration).
        """
        if value is None or str(value).strip() in ("", "null", "undefined"):
            self._raw_csv_duration_sec = None
        else:
            try:
                self._raw_csv_duration_sec = float(value)
            except (TypeError, ValueError):
                self._raw_csv_duration_sec = None
        self._app_config["rawCsvDurationSec"] = self._raw_csv_duration_sec
        self._save_app_config()
        self.appConfigChanged.emit()
        logger.debug(f"[Connector] rawCsvDurationSec set to {self._raw_csv_duration_sec}")

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

    def _persist_scan_notes(self, session_label: str) -> bool:
        """Write the in-memory notes buffer to sessions.session_notes of the
        scan DB session whose session_label matches. Returns True on success.
        ScanDBSink (critical) creates the session at scan start, so every scan
        that ran has a row to update."""
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
                db.update_session(session["id"],
                                  session_notes=self._scan_notes.strip())
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

    @pyqtProperty(QObject, notify=currentScanSourceChanged)
    def currentScanSource(self) -> ScanDataSource | None:
        """Current ScanDataSource bound to the PlotViewer. Usually the
        live source during a scan; can be a PastScanSource while the
        user is reviewing history via loadPastScan."""
        return self._current_scan_source

    @pyqtProperty(bool, notify=liveSourceAvailableChanged)
    def liveSourceAvailable(self) -> bool:
        """True when a LiveScanSource is held (i.e. at least one scan
        has run this session). PlotToolbar uses this to decide whether
        the "Back to live" button should switch sources back to live."""
        return self._live_scan_source is not None

    def _set_current_scan_source(self, source: ScanDataSource | None) -> None:
        """Replace the active source. Dedupes identical-instance assignments
        so the notify signal only fires on real transitions."""
        if source is self._current_scan_source:
            return
        self._current_scan_source = source
        self.currentScanSourceChanged.emit()

    @pyqtSlot()
    def showLiveSource(self) -> None:
        """Switch the viewer back to the held live source, if any.
        No-op when no LiveScanSource has been created this session."""
        if self._live_scan_source is None:
            return
        self._set_current_scan_source(self._live_scan_source)
        logger.info("[Plot] viewer switched back to live source")

    @pyqtSlot(str)
    def loadPastScan(self, session_label: str) -> None:
        """Open the saved scan with the given session_label (the
        YYYYMMDD_HHMMSS_userLabel string used elsewhere in the UI) and
        display it in the PlotViewer. The held live source is left
        intact so a subsequent showLiveSource() can return to it.

        Asynchronous load (issue #152): the bulk SQLite walk / CSV parse
        is multi-second for long scans (a ~70-min scan measured ~8.3M
        samples ≈ 20 s) and used to freeze the GUI thread. The heavy
        load now runs on a daemon worker thread; the finished buffers
        are marshalled back to the GUI thread via _pastScanBuffersReady,
        where the PastScanSource QObject is constructed and bound to the
        viewer. pastScanLoadFinished(label, ok) fires on completion
        either way so the History modal can clear its busy state."""
        if not session_label:
            logger.warning("loadPastScan: empty session_label")
            self.pastScanLoadFinished.emit("", False)
            return
        db_path = getattr(self._interface, "scan_db_path", None)
        if not db_path:
            logger.warning(
                "loadPastScan: scan_db_path unavailable on interface"
            )
            self.pastScanLoadFinished.emit(session_label, False)
            return
        # Per-cam corrected CSV ({scan_id}.csv, 82-col wide format)
        # is the only source of per-cam BFI/BVI/mean/contrast for
        # past replay — the DB's session_data only holds side-
        # aggregated corrected-frame placeholders. CsvSink writes
        # this CSV unconditionally for every scan, so it's reliably
        # available. Resolving the path is a cheap directory glob —
        # fine on the GUI thread (the modal already does it per
        # selection); the heavy parse happens on the worker.
        details = self.get_scan_details(session_label) or {}
        corrected_csv = details.get("correctedPath") or None
        self._past_scan_load_seq += 1
        seq = self._past_scan_load_seq
        threading.Thread(
            target=self._load_past_scan_worker,
            args=(seq, session_label, str(db_path), corrected_csv),
            name=f"past-scan-load-{seq}",
            daemon=True,
        ).start()

    def _load_past_scan_worker(
        self,
        seq: int,
        session_label: str,
        db_path: str,
        corrected_csv: Optional[str],
    ) -> None:
        """Worker thread: bulk-load a past scan's samples (the heavy,
        Qt-free part of loadPastScan). Opens its own ScanDatabase handle;
        results cross back to the GUI thread via _pastScanBuffersReady
        (cross-thread signal → queued delivery)."""
        try:
            from omotion.ScanDatabase import ScanDatabase
            from data_sources import load_past_scan_buffers
            db = ScanDatabase(db_path)
            try:
                session = db.get_session_by_label(session_label)
                if not session:
                    logger.warning(
                        "loadPastScan: no session found for label %r",
                        session_label,
                    )
                    self._pastScanBuffersReady.emit(
                        seq, session_label, -1, None, ""
                    )
                    return
                session_id = int(session["id"])
                buffers, _ = load_past_scan_buffers(
                    db, session_id, corrected_csv
                )
            finally:
                db.close()
            # Detect which source actually populated per-cam BFI: if
            # any (side, cam_id != -1, bfi) buffer exists, the DB was
            # sufficient and the CSV fallback was skipped.
            db_had_per_cam = any(k[1] >= 0 and k[2] == "bfi" for k in buffers)
            source_tag = (
                "db" if db_had_per_cam
                else ("csv" if corrected_csv else "db-only-sentinel")
            )
            self._pastScanBuffersReady.emit(
                seq, session_label, session_id, buffers, source_tag
            )
        except Exception:
            logger.exception("loadPastScan failed for label %r", session_label)
            self._pastScanBuffersReady.emit(seq, session_label, -1, None, "")

    @pyqtSlot(str, str)
    def _on_scan_outcome_warning(self, message: str, severity: str) -> None:
        """GUI thread: surface an interrupted/empty-scan toast. Errors are
        sticky (duration_ms=0) so a 'not saved' scan can't be missed."""
        duration = 0 if severity == "error" else 8000
        self.notify(message, severity, duration_ms=duration, tag="scan-outcome")

    def _on_past_scan_buffers_ready(
        self,
        seq: int,
        session_label: str,
        session_id: int,
        buffers,
        source_tag: str,
    ) -> None:
        """GUI thread: bind a worker-loaded past scan to the viewer.
        Results from a superseded request (user clicked another scan
        while this one was loading) are dropped."""
        if seq != self._past_scan_load_seq:
            logger.info(
                "loadPastScan: dropping stale result for %r "
                "(seq %d, latest %d)",
                session_label, seq, self._past_scan_load_seq,
            )
            return
        if buffers is None:
            self.errorOccurred.emit(
                f"Could not load scan '{session_label}' for plotting.\n"
                "See the app log for details."
            )
            self.pastScanLoadFinished.emit(session_label, False)
            return
        if buffers_are_empty(buffers):
            logger.info(
                "loadPastScan: %r contains no samples — not displaying",
                session_label,
            )
            self.errorOccurred.emit(
                f"Scan '{session_label}' contains no data.\n"
                "It was interrupted before any data could be recorded."
            )
            self.pastScanLoadFinished.emit(session_label, False)
            return
        try:
            past = PastScanSource(
                scan_db=None,
                session_id=session_id,
                parent=self,
                preloaded_buffers=buffers,
            )
            self._set_current_scan_source(past)
            # Diagnostic — left in for now so we can spot regressions
            # in past-scan loading. Cheap (one log line per click).
            n_buffers = len(past.buffers)
            n_samples = sum(b.n for b in past.buffers.values())
            sample_keys = sorted(past.buffers.keys())[:8]
            logger.info(
                "[Plot] loaded past scan %r (session_id=%d) source=%s: "
                "buffers=%d samples=%d liveEdge=%.3f sample_keys=%s",
                session_label, session_id, source_tag,
                n_buffers, n_samples, past.liveEdge, sample_keys,
            )
            self.pastScanLoadFinished.emit(session_label, True)
        except Exception:
            logger.exception("loadPastScan failed for label %r", session_label)
            self.pastScanLoadFinished.emit(session_label, False)

    @pyqtSlot(str, str, result=bool)
    def exportScanCsv(self, session_label: str, output_path: str) -> bool:
        """Export a scan's session_data to a corrected-format CSV.

        Called from the History modal's "Export CSV" button after the user
        picks a save path via FileDialog.
        """
        if not session_label or not output_path:
            return False
        try:
            from omotion.ScanDatabase import ScanDatabase
            from omotion.SessionPlayback import materialize_corrected_csv

            db_path = getattr(self._interface, "scan_db_path", None)
            if not db_path:
                self.errorOccurred.emit("No scan database available.")
                return False
            db = ScanDatabase(db_path)
            session = db.get_session_by_label(session_label)
            if not session:
                self.errorOccurred.emit(
                    f"No database session found for '{session_label}'."
                )
                return False
            session_id = int(session["id"])
            materialize_corrected_csv(
                str(db_path), session_id, output_path,
                include_quality=True,
            )
            logger.info(
                "exportScanCsv: exported %r (sid=%d) → %s",
                session_label, session_id, output_path,
            )
            return True
        except Exception as exc:
            logger.exception("exportScanCsv failed for %r", session_label)
            self.errorOccurred.emit(f"Export failed:\n{exc}")
            return False

    @pyqtSlot(str, result=int)
    @pyqtSlot(str, str, result=int)
    @pyqtSlot(str, str, int, result=int)
    @pyqtSlot(str, str, int, bool, result=int)
    @pyqtSlot(str, str, int, bool, str, result=int)
    def notify(self, text: str, type_: str = "info", duration_ms: int = 4000,
               dismissible: bool = True, tag: str = "") -> int:
        """Fire a toast notification. Reachable from QML as MotionInterface.notify(...)
        and from any Python code holding the connector instance.

        Args:
            text: message shown in the toast
            type_: one of "info", "success", "warning", "error"
            duration_ms: auto-dismiss after N ms; 0 = sticky until user dismisses
            dismissible: whether to show the ✕ close button
            tag: optional stable identifier. If non-empty, calling notify with the
                same tag again replaces the existing toast (no duplicate stacking),
                and dismissNotification(tag) can later target it.

        Returns:
            The integer id assigned to this notification. Pass to
            dismissNotification(id) to dismiss it later.
        """
        if type_ not in ("info", "success", "warning", "error"):
            logger.warning(f"notify: unknown type '{type_}', falling back to 'info'")
            type_ = "info"
        self._notification_id_counter += 1
        nid = self._notification_id_counter
        self.notificationRequested.emit({
            "id": nid,
            "tag": str(tag),
            "text": text,
            "type": type_,
            "durationMs": int(duration_ms),
            "dismissible": bool(dismissible),
        })
        return nid

    @pyqtSlot(int)
    @pyqtSlot(str)
    def dismissNotification(self, value):
        """Dismiss a single notification by id (int) or tag (str). Animated.

        Safe to call with an id/tag that no longer exists — it's a no-op.
        """
        if isinstance(value, bool):
            # bool is a subclass of int in Python; reject explicitly to avoid
            # surprising callers who pass True/False expecting different semantics.
            logger.warning("dismissNotification: bool is not a valid id/tag")
            return
        if isinstance(value, int):
            self.notificationDismissByIdRequested.emit(value)
        else:
            self.notificationDismissByTagRequested.emit(str(value))

    @pyqtSlot()
    def dismissAllNotifications(self):
        """Dismiss every active toast. Animated."""
        self.notificationDismissAllRequested.emit()

    def generate_user_label(self) -> str:
        suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        return f"ow{suffix}"

    # --- CONSOLE COMMUNICATION METHODS ---
    @pyqtSlot()
    def queryConsoleInfo(self):
        """Fetch and emit device information."""
        try:
            fw_version = self._interface.console.get_version()
            logger.info(f"Version: {fw_version}")
            hw_id = self._interface.console.get_hardware_id()
            device_id = base58.b58encode(bytes.fromhex(hw_id)).decode()
            self.consoleDeviceInfoReceived.emit(fw_version, device_id)
            logger.info(
                f"Console Device Info - Firmware: {fw_version}, Device ID: {device_id}"
            )
        except Exception as e:
            logger.error(f"Error querying device info: {e}")

    @pyqtSlot(str, int, int, int, str, bool, result=bool)
    def startCapture(
        self,
        subject_id: str,
        duration_sec: int,
        left_camera_mask: int,
        right_camera_mask: int,
        data_dir: str,
        disable_laser: bool,
    ) -> bool:
        """Start capture asynchronously; returns True if kicked off."""
        logger.info(
            f"startCapture(subject_id={subject_id}, dur={duration_sec}s, "
            f"left_mask=0x{left_camera_mask:02X}, right_mask=0x{right_camera_mask:02X}, "
            f"dir={data_dir}, disable_laser={disable_laser})"
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
            os.makedirs(data_dir, exist_ok=True)
        except Exception as e:
            self.captureLog.emit(f"Failed to create data dir: {e}")
            return False

        self._capture_stop = threading.Event()
        # Each new scan starts with a fresh notes buffer
        self._scan_notes = ""
        self._scan_notes_session_label = ""
        self.scanNotesChanged.emit()
        self._capture_running = True
        self._capture_start_time = time.time()
        # Per-scan monotonic zero for plot timestamps. sample.timestamp_s comes
        # from each sensor's firmware clock, which resets on sensor reboot — so
        # after a mid-scan unplug/replug, the two sides' clocks diverge and the
        # QML plot's shared `latestTimestamp` prunes the lagging side to empty.
        plot_t0 = time.monotonic()
        self._plot_t0 = plot_t0  # used by _on_dropout_check to compute dropout-marker t
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
        live_source = LiveScanSource(
            plot_t0=plot_t0,
            parent=self,
            scan_db_path=getattr(self._interface, "scan_db_path", None),
            cache_max_samples=_live_cache_samples,
        )
        # Track the live source separately so the user can navigate
        # to a past scan and return; emit so QML rebinds the
        # PlotToolbar's "Back to live" visibility.
        first_live = self._live_scan_source is None
        self._live_scan_source = live_source
        if first_live:
            self.liveSourceAvailableChanged.emit()
        self._set_current_scan_source(live_source)
        self._capture_left_path = ""
        self._capture_right_path = ""

        # Camera dropout watchdog state — fresh per scan.
        self._camera_last_seen = {}
        self._camera_last_temp = {}
        self._camera_dropped = set()
        self._camera_dropped_recovery_logged = set()
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

        # Reset trigger ON-time mirrors so _scan_elapsed_str starts from zero.
        self._trigger_cumulative_s = 0.0
        self._trigger_on_mono = None

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
            trigger_elapsed = self._trigger_cumulative_s
            if self._trigger_on_mono is not None:
                trigger_elapsed += time.monotonic() - self._trigger_on_mono
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
            self._persist_scan_notes(session_label)

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

            # New pipeline writes CSVs directly; no .raw→.csv post-processing
            # needed.  Pass empty paths to captureFinished so startPostProcess
            # is a no-op (QML still proceeds to the next scan step).
            self._capture_left_path = ""
            self._capture_right_path = ""
            self._capture_running = False
            self._safety_cancel_scheduled = False
            self._capture_thread = None
            self.captureFinished.emit(True, "", "", "")
            self.scanNotesReady.emit()

        # Issue #43: telemetry and raw CSVs are developer diagnostics —
        # clinical users must not get them. Default False (fail closed).
        developer_mode = self._app_config.get("developerMode", False)

        req = ScanRequest(
            subject_id=subject_id,
            duration_sec=duration_sec,
            left_camera_mask=left_camera_mask,
            right_camera_mask=right_camera_mask,
            disable_laser=disable_laser,
            reduced_mode=self._app_config.get("reducedMode", False),
            # Corrected CSV is opt-in now that per-cam BFI/BVI lands in
            # the scan DB (the new viewer + past replay read from there).
            # Default False to skip the redundant {scan_id}.csv; flip
            # writeCorrectedCsv:true in app_config to keep exporting it.
            # The SDK still forces it on if no DB is configured.
            write_corrected_csv=self._app_config.get("writeCorrectedCsv", False),
            # Issue #43 (regression): the SDK defaults write_telemetry_csv
            # to True, so the per-scan {scan_id}_{subject}_telemetry.csv
            # must be explicitly gated on developerMode here. The original
            # gate was dropped in the sink-refactor follow-up (93c2feb).
            write_telemetry_csv=developer_mode,
            # Raw CSV duration forwarded to the pipeline's Tee("raw") gate
            # via raw_save_max_duration_s. None means unbounded (write entire
            # scan); 0 omits raw tee entirely. The writeRawCsv toggle lives
            # in the developer-only Settings card, so its persisted value is
            # additionally gated on developerMode (#43) — flipping dev mode
            # off must stop raw output even if the toggle was left on.
            raw_save_max_duration_s=(
                self._raw_csv_duration_sec
                if (developer_mode and self._write_raw_csv) else 0
            ),
            sinks=[
                _LivePlotSink(connector=self, plot_t0=plot_t0, live_source=live_source,
                              nan_gap_tracker=nan_gap_tracker),
                _TriggerStateSink(connector=self),
                outcome_sink,
                _CompletionSink(connector=self, on_complete_cb=_on_pipeline_complete),
            ],
        )

        started = self._interface.start_scan(req)
        if started:
            logger.info(
                "=== Full scan started: subject=%s duration=%ss "
                "left=0x%02X right=0x%02X laser=%s ===",
                subject_id, duration_sec, left_camera_mask, right_camera_mask,
                "off" if disable_laser else "on",
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
            self._set_current_scan_source(None)  # release the orphaned LiveScanSource — scan never started
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
            else:
                logger.warning(
                    "startCapture aborted: SDK refused to spawn a new scan "
                    "(usually a previous worker thread that didn't exit cleanly)."
                )
                self.captureLog.emit("Capture already running.")
        return bool(started)

    def _log_scan_image_stats(self, left_csv: str, right_csv: str) -> None:
        left_csv = (left_csv or "").strip()
        right_csv = (right_csv or "").strip()
        if left_csv.lower().endswith(".raw"):
            left_csv = left_csv[:-4] + ".csv"
        if right_csv.lower().endswith(".raw"):
            right_csv = right_csv[:-4] + ".csv"

        if left_csv and not Path(left_csv).exists():
            logger.warning(f"Scan stats skipped; left CSV not found: {left_csv}")
            left_csv = ""
        if right_csv and not Path(right_csv).exists():
            logger.warning(f"Scan stats skipped; right CSV not found: {right_csv}")
            right_csv = ""

        if not left_csv and not right_csv:
            logger.warning("Scan stats skipped; no CSV files available.")
            return

        try:
            viz = VisualizeBloodflow(left_csv, right_csv)
            viz.compute()
        except Exception:
            logger.exception("Scan stats failed during VisualizeBloodflow.compute()")
            return
        _, _, camera_inds, contrast, mean = viz.get_results()
        if mean is None or mean.size == 0:
            logger.warning("Scan stats skipped; mean array was empty.")
            return

        per_cam_mean = np.mean(mean, axis=1)
        per_cam_contrast = np.mean(contrast, axis=1) if contrast is not None else None
        sides = getattr(viz, "_sides", None)

        logger.info("Scan image stats per camera:")

        # Build rows for CSV export (same data as log output)
        ft_rows = []

        for idx in range(len(per_cam_mean)):
            cam_id = None
            if camera_inds is not None and idx < len(camera_inds):
                try:
                    cam_id = int(camera_inds[idx])
                except Exception:
                    cam_id = None
            side = None
            if sides is not None and idx < len(sides):
                side = str(sides[idx])

            if cam_id is None:
                label = f"camera[{idx}]"
            elif side:
                label = f"camera {cam_id} ({side})"
            else:
                label = f"camera {cam_id}"

            mean_val = float(per_cam_mean[idx])
            avg_contrast = (
                float(per_cam_contrast[idx]) if per_cam_contrast is not None else None
            )

            if per_cam_contrast is None:
                logger.info("  %s mean: %.0f", label, mean_val)
            else:
                logger.info(
                    "  %s mean: %.0f, avg contrast: %.3f",
                    label,
                    mean_val,
                    avg_contrast,
                )

            # Get cached security UID and HWID from SDK (sensor retains these)
            side_key = (side or "").lower()
            cid = int(cam_id) if cam_id is not None and cam_id != "" else -1
            sensor = getattr(self._interface, side_key, None) if self._interface else None
            if (
                sensor is not None
                and hasattr(sensor, "get_cached_camera_security_uid")
                and hasattr(sensor, "get_cached_hardware_id")
            ):
                security_id = (
                    sensor.get_cached_camera_security_uid(cid) if cid >= 0 else ""
                )
                hwid = sensor.get_cached_hardware_id()
            else:
                security_id = ""
                hwid = ""

            # FT thresholds: use cam_id (0-7) to index per-camera minimums
            cam_idx = cid if cid >= 0 else idx
            min_mean = None
            min_contrast = None
            if self._ft_min_mean_per_camera and cam_idx < len(
                self._ft_min_mean_per_camera
            ):
                min_mean = self._ft_min_mean_per_camera[cam_idx]
            if self._ft_min_contrast_per_camera and cam_idx < len(
                self._ft_min_contrast_per_camera
            ):
                min_contrast = self._ft_min_contrast_per_camera[cam_idx]

            if min_mean is not None and not isinstance(min_mean, (int, float)):
                min_mean = None
            if min_contrast is not None and not isinstance(min_contrast, (int, float)):
                min_contrast = None

            mean_test = "PASS" if (min_mean is None or mean_val >= min_mean) else "FAIL"
            if min_contrast is None:
                contrast_test = "PASS"
            elif avg_contrast is None:
                contrast_test = "FAIL"
            else:
                contrast_test = "PASS" if avg_contrast >= min_contrast else "FAIL"

            ft_rows.append(
                {
                    "camera_index": idx,
                    "side": side or "",
                    "cam_id": cam_id if cam_id is not None else "",
                    "mean": mean_val,
                    "avg_contrast": avg_contrast if avg_contrast is not None else "",
                    "mean_test": mean_test,
                    "contrast_test": contrast_test,
                    "security_id": security_id or "",
                    "hwid": hwid or "",
                }
            )

        # Write CSV to app-logs/ft-test-csvs
        try:
            ft_dir = os.path.join(self._directory, "app-logs", "ft-test-csvs")
            os.makedirs(ft_dir, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            ft_path = os.path.join(ft_dir, f"ft-test-{ts}.csv")
            with open(ft_path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(
                    f,
                    fieldnames=[
                        "camera_index",
                        "side",
                        "cam_id",
                        "mean",
                        "avg_contrast",
                        "mean_test",
                        "contrast_test",
                        "security_id",
                        "hwid",
                    ],
                )
                w.writeheader()
                w.writerows(ft_rows)
            logger.info(f"Scan image stats CSV written to {ft_path}")
        except Exception as e:
            logger.warning(f"Failed to write FT CSV: {e}")

        # Emit a single end-of-scan FT verdict to the Qt capture log window.
        overall_ft_pass = bool(ft_rows) and all(
            row.get("mean_test") == "PASS" and row.get("contrast_test") == "PASS"
            for row in ft_rows
        )
        ft_result = "PASS" if overall_ft_pass else "FAIL"
        status_emoji = "✅" if overall_ft_pass else "❌"
        ft_msg = f"{status_emoji} FT criteria result: {ft_result}"
        self.captureLog.emit(ft_msg)
        logger.info(ft_msg)

    def _on_safety_trip_during_capture(self):
        """Called on main thread when safety tripped while scan was running: show message and cancel scan in 5 s."""
        if not self._capture_running or self._safety_cancel_scheduled:
            return
        self._safety_cancel_scheduled = True
        self.captureLog.emit(
            "Laser safety system tripped. Scan will be cancelled in 5 seconds."
        )
        QTimer.singleShot(5000, self.stopCapture)

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
                    # Decode which safety bits tripped so the developer-
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
                    if self._capture_running and not self._safety_cancel_scheduled:
                        self.safetyTripDuringCaptureRequested.emit()
        except Exception as e:
            logger.error(f"readSafetyStatus failed: {e}")
            self.safetyFailure = True
            self._fire_safety_notification(f"telemetry exception: {e}")
            if self._capture_running and not self._safety_cancel_scheduled:
                self.safetyTripDuringCaptureRequested.emit()

    @pyqtSlot(str, int, int, int, int, int, result=QVariant)
    def i2cReadBytes(
        self,
        target: str,
        mux_idx: int,
        channel: int,
        i2c_addr: int,
        offset: int,
        data_len: int,
    ):
        """Send i2c read to device"""
        try:
            # logger.info(f"I2C Read Request -> target={target}, mux_idx={mux_idx}, channel={channel}, "
            # f"i2c_addr=0x{int(i2c_addr):02X}, offset=0x{int(offset):02X}, read_len={int(data_len)}"
            # )

            if target == "console":
                fpga_data, fpga_data_len = (
                    self._interface.console.read_i2c_packet(
                        mux_index=mux_idx,
                        channel=channel,
                        device_addr=i2c_addr,
                        reg_addr=offset,
                        read_len=data_len,
                    )
                )
                if fpga_data is None or fpga_data_len == 0:
                    logger.error("readI2CBytes failed (I2C read error)")
                    return []
                else:
                    return list(fpga_data[:fpga_data_len])

            elif target in ("left", "right"):
                logger.error("I2C Read Not Implemented")
                return []
        except Exception as e:
            logger.error(f"Error sending i2c read command: {e}")
            return []

    @pyqtSlot(int)
    def setRGBState(self, state):
        """Set the RGB state using integer values."""
        try:
            valid_states = [0, 1, 2, 3]
            if state not in valid_states:
                logger.error(f"Invalid RGB state value: {state}")
                return
            if self._interface.console.set_rgb_led(state) == state:
                logger.info(f"RGB state set to: {state}")
            else:
                logger.error(f"Failed to set RGB state to: {state}")
        except Exception as e:
            logger.error(f"Error setting RGB state: {e}")

    @pyqtSlot()
    def queryRGBState(self):
        """Fetch and emit RGB state."""
        try:
            state = self._interface.console.get_rgb_led()
            state_text = {0: "Off", 1: "IND1", 2: "IND2", 3: "IND3"}.get(
                state, "Unknown"
            )

            logger.info(f"RGB State: {state_text}")
            self.rgbStateReceived.emit(state, state_text)  # Emit both values
        except Exception as e:
            logger.error(f"Error querying RGB state: {e}")

    @pyqtSlot(result=QVariant)
    def queryTriggerConfig(self):
        trigger_setting = self._interface.console.get_trigger_json()
        if trigger_setting:
            if isinstance(trigger_setting, str):
                updateTrigger = json.loads(trigger_setting)
            else:
                updateTrigger = trigger_setting
            if updateTrigger["TriggerStatus"] == 2:
                self._trigger_state = "ON"
                self.triggerStateChanged.emit()
                return trigger_setting or {}

        self._trigger_state = "OFF"
        self.triggerStateChanged.emit()

        return trigger_setting or {}

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

    @pyqtSlot(result=int)
    def getFsyncCount(self):
        """Get the Fsync count from the console."""
        try:
            fsync_count = self._interface.console.get_fsync_pulsecount()
            logger.info(f"Fsync Count: {fsync_count}")
            return fsync_count
        except Exception as e:
            logger.error(f"Error getting Fsync count: {e}")
            return -1

    @pyqtSlot(result=int)
    def getLsyncCount(self):
        """Get the Fsync count from the console."""
        try:
            lsync_count = self._interface.console.get_lsync_pulsecount()
            logger.debug(f"Lsync Count: {lsync_count}")
            return lsync_count
        except Exception as e:
            logger.error(f"Error getting Lsync count: {e}")
            return -1

    def set_laser_power_from_config(self, interface):
        # Laser-power config now lives in the SDK (omotion.laser): it bundles
        # the FPGA register map + the laser param set and writes them over I2C.
        # force_fault honors the forceLaserFail app flag (loads the
        # safety-trip param set to exercise the interlock).
        return interface.apply_laser_power(force_fault=self._force_laser_fail)

    # ------------------------------------------------------------------
    # Contact-quality quick-check
    # ------------------------------------------------------------------
    @staticmethod
    def _camera_label(side: str, cam_id: int) -> str:
        prefix = "L" if side == "left" else "R"
        return f"{prefix}{int(cam_id) + 1}"

    @staticmethod
    def _warning_text(type_key: str) -> str:
        return {
            "ambient_light": "Ambient light detected",
            "poor_contact": "Poor sensor contact",
        }.get(type_key, type_key)

    @staticmethod
    def _threshold_for(thresholds, cam_id: int, default_value: float) -> float:
        if isinstance(thresholds, (list, tuple)) and 0 <= int(cam_id) < len(thresholds):
            try:
                return float(thresholds[int(cam_id)])
            except Exception:
                return float(default_value)
        return float(default_value)

    def _ensure_idle(self) -> str | None:
        """Gate for pipeline-starting slots (capture / configure / check)."""
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

    # ──────────────────────────────────────────────────────────────────
    @pyqtSlot()
    def runContactQualityCheck(self):
        """Run the contact-quality check via the SDK's ContactQualityWorkflow.

        Delegates to interface.contact_quality_workflow.check(), which runs
        a short scan internally and returns a ContactQualityResult with
        per-camera BFI statistics. The SDK call is synchronous/blocking, so
        we run it in a background thread and marshal results back via a
        private signal.
        """
        err = self._ensure_idle()
        if err is not None:
            self.contactQualityCheckFinished.emit(False, err, [])
            return

        cfg = self._app_config or {}
        duration_s = float(cfg.get("cq_check_duration_sec", 1.0))
        dark_thresholds = list(
            cfg.get("cq_dark_threshold_per_camera") or [_CQ_DEFAULT_DARK_THRESHOLD_DN] * 8
        )
        light_thresholds = list(
            cfg.get("cq_light_threshold_per_camera") or [_CQ_DEFAULT_LIGHT_THRESHOLD_DN] * 8
        )
        rolling_window = int(cfg.get("cq_rolling_avg_window", _CQ_DEFAULT_ROLLING_WINDOW))

        if not (self._leftSensorConnected or self._rightSensorConnected):
            self.contactQualityCheckFinished.emit(False, "No sensors connected", [])
            return

        self._cq_quick_running = True
        self.contactQualityScanInProgress.emit(True)
        self.contactQualityCheckStarted.emit(int(round(duration_s + 3)))

        left_mask = 0xFF if self._leftSensorConnected else 0x00
        right_mask = 0xFF if self._rightSensorConnected else 0x00

        self._cq_t0 = time.monotonic()
        logger.info(
            "=== Contact-quality check started: duration=%.1fs "
            "left=0x%02X right=0x%02X ===",
            duration_s, left_mask, right_mask,
        )

        def _worker():
            try:
                result = self._interface.contact_quality_workflow.check(
                    duration_sec=duration_s,
                    rolling_window=rolling_window,
                    dark_threshold_per_camera=dark_thresholds,
                    light_threshold_per_camera=light_thresholds,
                    left_camera_mask=left_mask,
                    right_camera_mask=right_mask,
                )
                self._cq_result_signal.emit(result)
            except Exception as exc:
                logger.exception("CQ workflow check raised: %s", exc)
                self._cq_result_signal.emit(None)

        t = threading.Thread(target=_worker, daemon=True, name="CQWorkflow-check")
        t.start()

    # Private signal used to marshal ContactQualityResult from the CQ worker
    # thread back to the main Qt thread (emitted by the _worker closure in
    # runContactQualityCheck, consumed by _on_cq_result_ready).
    _cq_result_signal = pyqtSignal(object)

    @pyqtSlot(object)
    def _on_cq_result_ready(self, result):
        """Main-thread slot: convert ContactQualityResult → UI warning list and
        emit contactQualityCheckFinished.  Connected to _cq_result_signal in
        __init__ (via connect_signals).
        """
        cfg = self._app_config or {}
        dark_thresholds = list(
            cfg.get("cq_dark_threshold_per_camera") or [_CQ_DEFAULT_DARK_THRESHOLD_DN] * 8
        )
        light_thresholds = list(
            cfg.get("cq_light_threshold_per_camera") or [_CQ_DEFAULT_LIGHT_THRESHOLD_DN] * 8
        )

        self._cq_quick_running = False
        self.contactQualityScanInProgress.emit(False)

        elapsed_s = (
            time.monotonic() - self._cq_t0 if self._cq_t0 is not None else 0.0
        )
        if result is None:
            logger.info(
                "=== Contact-quality check ended: failed after %.1fs ===",
                elapsed_s,
            )
            self.contactQualityCheckFinished.emit(False, "CQ check failed", [])
            return
        logger.info(
            "=== Contact-quality check ended: completed after %.1fs ===",
            elapsed_s,
        )

        # Convert CamCQResult.reason → (typeKey, warning dict) that the QML
        # ContactQualityModal expects.
        warnings_by_key: dict[tuple[str, str], dict] = {}
        table_rows: list[dict] = []

        # Iterate over active cameras in mask order for consistent logging.
        for side_idx, (side, mask) in enumerate((("left", 0xFF), ("right", 0xFF))):
            for cam_id in range(8):
                cam_res = result.per_camera.get((side, cam_id))
                if cam_res is None:
                    continue  # camera not evaluated (outside mask)
                camera = self._camera_label(side, cam_id)
                dark_threshold = (
                    dark_thresholds[cam_id] if cam_id < len(dark_thresholds)
                    else _CQ_DEFAULT_DARK_THRESHOLD_DN
                )
                light_threshold = (
                    light_thresholds[cam_id] if cam_id < len(light_thresholds)
                    else _CQ_DEFAULT_LIGHT_THRESHOLD_DN
                )
                light_avg_dn = cam_res.light_avg_dn
                dark_max_dn = cam_res.dark_max_dn
                reason = cam_res.reason
                warn_tags = []

                if reason == "ambient_light":
                    warnings_by_key[(camera, "ambient_light")] = {
                        "camera": camera,
                        "typeKey": "ambient_light",
                        "typeText": self._warning_text("ambient_light"),
                        "value": float(dark_max_dn) if dark_max_dn == dark_max_dn else 0.0,
                    }
                    warn_tags.append("ambient_light")
                elif reason in ("poor_contact", "no_signal"):
                    warnings_by_key[(camera, "poor_contact")] = {
                        "camera": camera,
                        "typeKey": "poor_contact",
                        "typeText": self._warning_text("poor_contact"),
                        "value": float(light_avg_dn) if light_avg_dn == light_avg_dn else 0.0,
                    }
                    warn_tags.append("poor_contact")

                table_rows.append({
                    "camera": camera,
                    "light_avg_dn": f"{light_avg_dn:.2f}" if light_avg_dn == light_avg_dn else "n/a",
                    "dark_max_dn":  f"{dark_max_dn:.2f}"  if dark_max_dn  == dark_max_dn  else "n/a",
                    "dark_threshold": dark_threshold,
                    "light_threshold": light_threshold,
                    "reason": reason,
                    "warnings": ",".join(warn_tags) if warn_tags else "-",
                })

        logger.info("CQ Final Compare (DN, pedestal-subtracted; rolling-avg light, max dark):")
        logger.info(
            "| Camera | LightAvg | DarkMax | DarkThr | LightThr | Reason         | Warnings |"
        )
        logger.info(
            "|--------|----------|---------|---------|----------|----------------|----------|"
        )
        for row in table_rows:
            logger.info(
                "| %-6s | %8s | %7s | %7.2f | %8.2f | %-14s | %-8s |",
                row["camera"],
                row["light_avg_dn"],
                row["dark_max_dn"],
                row["dark_threshold"],
                row["light_threshold"],
                row["reason"],
                row["warnings"],
            )

        warning_list = list(warnings_by_key.values())
        ok = result.passed
        err_msg = "" if ok else "Contact quality check failed"
        self.contactQualityCheckFinished.emit(ok, err_msg, warning_list)

    @pyqtSlot()
    def _on_dropout_check(self):
        """1 Hz watchdog: emit cameraDropoutDetected for any camera silent > threshold."""
        if not self._capture_running:
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
                    f"(no data for >{threshold:.0f} s). Last temperature: {temp:.1f}°C"
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
                # Also feed the new LiveScanSource so Phase 2+'s PlotViewer can
                # render a dropout bar. Time is relative to plot_t0 to match the
                # per-sample t axis (sample timestamps from the SDK use the same
                # plot_t0-anchored monotonic origin).
                src = self._current_scan_source
                if src is not None and getattr(src, "live", False):
                    src.mark_dropped(side=side, cam_id=cam_id, t=now - self._plot_t0)

    @pyqtSlot(result=str)
    def scanElapsedStr(self) -> str:
        """QML-facing accessor for trigger-ON elapsed time (HH:MM:SS)."""
        return self._scan_elapsed_str()

    def _scan_elapsed_str(self) -> str:
        """Return current scan elapsed trigger-ON time as HH:MM:SS."""
        elapsed = self._trigger_cumulative_s
        if self._trigger_on_mono is not None:
            elapsed += time.monotonic() - self._trigger_on_mono
        total_s = int(elapsed)
        h = total_s // 3600
        m = (total_s % 3600) // 60
        s = total_s % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    # --- SENSOR COMMUNICATION METHODS ---
    def _read_and_log_camera_uids(self, side: str):
        """
        Read and log security UIDs for the given sensor's cameras.

        Called once per sensor connect (after the ID cache fill in
        _run_sensor_init), not at every scan start.
        """
        try:
            connected = (
                self._leftSensorConnected
                if side == "left"
                else self._rightSensorConnected
            )
            sensor = getattr(self._interface, side, None) if self._interface else None
            if not connected or sensor is None:
                logger.warning("%s sensor not connected, cannot read camera UIDs", side)
                return

            # Prefer cached values (populated by refresh_id_cache) over
            # per-camera hardware reads.
            logger.info(f"Camera UIDs on {side} sensor:")
            cache_populated = (
                getattr(sensor, "_cached_camera_uids", None) is not None
            )
            get_cached = getattr(sensor, "get_cached_camera_security_uid", None)
            read_uid = getattr(sensor, "read_camera_security_uid", None)
            for camera_id in range(8):
                try:
                    if cache_populated and get_cached:
                        uid_str = get_cached(camera_id)
                        uid_hex = uid_str.replace("0x", "") if uid_str else ""
                    elif read_uid:
                        uid_bytes = read_uid(camera_id)
                        time.sleep(0.05)
                        uid_hex = "".join(f"{b:02X}" for b in uid_bytes)
                    else:
                        continue
                    display_uid = (
                        f"0x{uid_hex}"
                        if uid_hex and not uid_hex.startswith("0x")
                        else (uid_hex or "0x000000000000")
                    )
                    if not uid_hex or set(uid_hex.replace("0x", "").upper()) <= {
                        "0"
                    }:
                        logger.info(
                            f"  Camera {camera_id + 1}: Not present (UID: {display_uid})"
                        )
                        self.configLog.emit(f"Camera {camera_id + 1}: Not present")
                    else:
                        logger.info(
                            f"  Camera {camera_id + 1}: UID = {display_uid}"
                        )
                        self.configLog.emit(
                            f"Camera {camera_id + 1} UID: {display_uid}"
                        )
                except Exception as e:
                    logger.error(
                        f"Error reading UID for camera {camera_id + 1} on {side} sensor: {e}"
                    )
        except Exception as e:
            logger.error(f"Error reading camera UIDs: {e}")

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

    @pyqtSlot(str)
    def querySensorAccelerometer(self, target: str):
        """Fetch and emit Accelerometer data. ``target`` is "left" or "right"."""
        try:
            if target not in ("left", "right"):
                logger.error(f"Invalid target for sensor info query: {target}")
                return

            # Check if sensor is connected
            if (target == "left" and not self._leftSensorConnected) or (
                target == "right" and not self._rightSensorConnected
            ):
                logger.error(f"{target.capitalize()} sensor not connected")
                return

            sensor = getattr(self._interface, target)
            if sensor is None:
                logger.error(f"{target.capitalize()} sensor object is None")
                return
            accel = sensor.imu_get_accelerometer()
            logger.info(f"Accel (raw): X={accel[0]}, Y={accel[1]}, Z={accel[2]}")
            self.accelerometerSensorUpdated.emit(accel[0], accel[1], accel[2])
        except Exception as e:
            logger.error(f"Error querying Accelerometer data: {e}")

    @pyqtSlot()
    def querySensorGyroscope(self, target: str):
        """Fetch and emit Gyroscope data. ``target`` is "left" or "right"."""
        try:
            if target not in ("left", "right"):
                logger.error(f"Invalid target for sensor info query: {target}")
                return

            gyro = getattr(self._interface, target).imu_get_gyroscope()
            logger.info(f"Gyro  (raw): X={gyro[0]}, Y={gyro[1]}, Z={gyro[2]}")
            self.gyroscopeSensorUpdated.emit(gyro[0], gyro[1], gyro[2])
        except Exception as e:
            logger.error(f"Error querying Gyroscope data: {e}")

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

    @pyqtSlot(str)
    def querySensorTemperature(self, target: str):
        """Fetch and emit Temperature data. ``target`` is "left" or "right"."""
        try:
            if target not in ("left", "right"):
                logger.error(f"Invalid target for sensor info query: {target}")
                return

            if (target == "left" and not self._leftSensorConnected) or (
                target == "right" and not self._rightSensorConnected
            ):
                logger.error(f"{target.capitalize()} sensor not connected")
                return

            sensor = getattr(self._interface, target)
            if sensor is None:
                logger.error(f"{target.capitalize()} sensor object is None")
                return

            imu_temp = sensor.imu_get_temperature()
            logger.info(f"Temperature Data - IMU Temp: {imu_temp}")
            self.temperatureSensorUpdated.emit(imu_temp)
        except Exception as e:
            logger.error(f"Error querying Temperature data: {e}")

    @pyqtSlot(str)
    def querySensorInfo(self, target: str):
        """Fetch and emit device information. ``target`` is "left" or "right"."""
        try:
            if target not in ("left", "right"):
                logger.error(f"Invalid target for sensor info query: {target}")
                return

            if (target == "left" and not self._leftSensorConnected) or (
                target == "right" and not self._rightSensorConnected
            ):
                logger.error(f"{target.capitalize()} sensor not connected")
                return

            sensor = getattr(self._interface, target)
            if sensor is None:
                logger.error(f"{target.capitalize()} sensor object is None")
                return

            fw_version = sensor.get_version()
            logger.info(f"Version: {fw_version}")
            hw_id = sensor.get_hardware_id()
            device_id = base58.b58encode(bytes.fromhex(hw_id)).decode()
            self.sensorDeviceInfoReceived.emit(fw_version, device_id)
            logger.info(
                f"Sensor Device Info - Firmware: {fw_version}, Device ID: {device_id}"
            )
        except Exception as e:
            logger.error(f"Error querying device info: {e}")

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
        try:
            if sensor_side.lower() == "left":
                if not self._leftSensorConnected:
                    logger.error("Left sensor not connected")
                    return False
                result = self._interface.left.set_fan_control(fan_on)
            elif sensor_side.lower() == "right":
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
                logger.info(
                    f"Fan status for {sensor_side} sensor: {'ON' if status else 'OFF'}"
                )
            return status
        except Exception as e:
            # Disconnect raced with the in-flight call. Log at DEBUG;
            # the SDK's state machine already logs the disconnect at INFO.
            logger.debug(f"Fan status read on {side} failed during disconnect: {e}")
            return False

    # --- POST-PROCESSING METHODS ---
    @pyqtSlot(str, str, result=bool)
    def startPostProcess(self, left_raw: str, right_raw: str) -> bool:
        """
        Convert left/right .raw to .csv in-place (same directory).
        Returns False if a post job is already running.
        """
        if self._post_thread is not None:
            self.postLog.emit("Post-process already running.")
            return False

        left_raw = left_raw or ""
        right_raw = right_raw or ""
        self._post_cancel = threading.Event()

        def _worker():
            ok = True
            err = ""
            left_csv = ""
            right_csv = ""

            try:
                def _to_csv_path(p):
                    base, ext = os.path.splitext(p)
                    return base + ".csv" if base else ""

                # Process LEFT
                if left_raw and os.path.isfile(left_raw):
                    self.postLog.emit(f"Processing LEFT: {os.path.basename(left_raw)}")
                    self.postProgress.emit(5)
                    left_csv = _to_csv_path(left_raw)
                    process_bin_file(left_raw, left_csv)
                    self.postLog.emit(f"LEFT → {os.path.basename(left_csv)}")
                    self.postProgress.emit(50)
                else:
                    if left_raw:
                        self.postLog.emit(f"LEFT missing: {left_raw}")
                    self.postProgress.emit(50)

                # Cancel check between files
                if self._post_cancel.is_set():
                    ok = False
                    err = "Canceled"
                    return

                # Process RIGHT
                if right_raw and os.path.isfile(right_raw):
                    self.postLog.emit(
                        f"Processing RIGHT: {os.path.basename(right_raw)}"
                    )
                    self.postProgress.emit(55)
                    right_csv = _to_csv_path(right_raw)
                    process_bin_file(right_raw, right_csv)
                    self.postLog.emit(f"RIGHT → {os.path.basename(right_csv)}")
                    self.postProgress.emit(95)
                else:
                    if right_raw:
                        self.postLog.emit(f"RIGHT missing: {right_raw}")
                    self.postProgress.emit(95)

                self.postProgress.emit(100)

            except Exception as e:
                ok = False
                err = str(e)
                self.postLog.emit(f"Post-process error: {err}")
            finally:
                # clear thread handle before emitting
                self._post_thread = None
                self.postFinished.emit(ok, err, left_csv or "", right_csv or "")
                logger.info(
                    f"Post-process finished: ok={ok}, err={err}, left_csv={left_csv}, right_csv={right_csv}"
                )

        self._post_thread = threading.Thread(target=_worker, daemon=True)
        self._post_thread.start()
        return True

    @pyqtSlot()
    def cancelPostProcess(self):
        """Request cancel; takes effect between files."""
        if self._post_thread is None:
            return
        self.postLog.emit("Cancel requested.")
        self._post_cancel.set()

    # --- ERROR HANDLING METHODS / MISCELLANEOUS METHODS ---
    @pyqtSlot(str)
    def emitError(self, msg):
        self.errorOccurred.emit(msg)

    @pyqtSlot(result=str)
    def get_sdk_version(self):
        return self._interface.get_sdk_version()

    @pyqtSlot(str)
    def copyToClipboard(self, text: str) -> None:
        """Push a string to the system clipboard via Qt — used by the
        Test Results window's Copy button. Centralised here so QML
        doesn't need a direct dependency on PyQt6.QtGui."""
        from PyQt6.QtGui import QGuiApplication
        cb = QGuiApplication.clipboard()
        if cb is not None:
            cb.setText(text)

    def connect_signals(self):
        """Subscribe to per-handle state changes on the SDK interface."""
        for handle in (
            self._interface.console,
            self._interface.left,
            self._interface.right,
        ):
            handle.signal_state_changed.connect(self._on_handle_state_changed)
        self.safetyTripDuringCaptureRequested.connect(
            self._on_safety_trip_during_capture
        )
        # Worker → Qt main thread for the calibration completion callback.
        self._calibrationCompleteSignal.connect(self._on_calibration_complete)
        self._testScanCompleteSignal.connect(self._on_test_scan_complete)
        # Worker → Qt main thread for the CQ workflow result.
        self._cq_result_signal.connect(self._on_cq_result_ready)
        # Worker → Qt main thread for async past-scan loads (issue #152).
        self._pastScanBuffersReady.connect(self._on_past_scan_buffers_ready)
        # Auto-queued: emitted on the pipeline worker thread, runs the toast
        # on the GUI thread (same marshalling pattern as _pastScanBuffersReady).
        self._scanOutcomeWarningSignal.connect(self._on_scan_outcome_warning)

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
        """
        from omotion import CalibrationRequest, CalibrationThresholds

        if self._calibration_status == "running":
            return

        if not self._consoleConnected:
            self.captureLog.emit("⚠️ Cannot calibrate: console not connected.")
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
            if target == "left" and not self._leftSensorConnected:
                self.captureLog.emit("⚠️ Cannot calibrate: left sensor not connected.")
            elif target == "right" and not self._rightSensorConnected:
                self.captureLog.emit("⚠️ Cannot calibrate: right sensor not connected.")
            else:
                self.captureLog.emit("⚠️ Cannot calibrate: no sensors connected.")
            return

        thresholds = CalibrationThresholds(
            min_mean_per_camera=list(self._ft_min_mean_per_camera or [0.0]*8),
            min_contrast_per_camera=list(self._ft_min_contrast_per_camera or [0.0]*8),
            min_bfi_per_camera=list(self._ft_min_bfi_per_camera or [0.0]*8),
            min_bvi_per_camera=list(self._ft_min_bvi_per_camera or [0.0]*8),
            max_bfi_per_camera=(
                list(self._ft_max_bfi_per_camera)
                if self._ft_max_bfi_per_camera is not None else None
            ),
            max_bvi_per_camera=(
                list(self._ft_max_bvi_per_camera)
                if self._ft_max_bvi_per_camera is not None else None
            ),
            max_dark_per_camera=(
                list(self._ft_max_dark_per_camera)
                if self._ft_max_dark_per_camera is not None else None
            ),
        )
        output_dir = os.path.join(self._directory, "calibrations")
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
        self.calibrationStateChanged.emit()
        self.captureLog.emit("Calibration: starting…")
        self._calibration_t0 = time.monotonic()
        logger.info(
            "=== Calibration started: target=%s left=0x%02X right=0x%02X "
            "scan_duration=%ss ===",
            target, left_mask, right_mask, self._calibration_scan_duration_sec,
        )

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
    @pyqtSlot(str)
    def runTestScan(self, target: str = "both"):
        """Run just the calibration scan (phase 1) as a Test diagnostic.
        Mirrors runCalibration but does NOT write calibration to the
        console EEPROM and does NOT run a validation scan. Idempotent
        if a calibration or test scan is already in flight.

        ``target`` selects which side(s) to test: ``"left"``,
        ``"right"``, or ``"both"`` (default). Issue #117 — test stations
        with only one static phantom need to test one side at a time.
        """
        from omotion import CalibrationRequest, CalibrationThresholds

        # Mutual exclusion with the Calibrate flow.
        if self._test_scan_status == "running":
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
            if target == "left" and not self._leftSensorConnected:
                self.captureLog.emit("⚠️ Cannot run Test scan: left sensor not connected.")
            elif target == "right" and not self._rightSensorConnected:
                self.captureLog.emit("⚠️ Cannot run Test scan: right sensor not connected.")
            else:
                self.captureLog.emit("⚠️ Cannot run Test scan: no sensors connected.")
            return

        thresholds = CalibrationThresholds(
            min_mean_per_camera=list(self._ft_min_mean_per_camera or [0.0]*8),
            min_contrast_per_camera=list(self._ft_min_contrast_per_camera or [0.0]*8),
            min_bfi_per_camera=list(self._ft_min_bfi_per_camera or [0.0]*8),
            min_bvi_per_camera=list(self._ft_min_bvi_per_camera or [0.0]*8),
            max_bfi_per_camera=(
                list(self._ft_max_bfi_per_camera)
                if self._ft_max_bfi_per_camera is not None else None
            ),
            max_bvi_per_camera=(
                list(self._ft_max_bvi_per_camera)
                if self._ft_max_bvi_per_camera is not None else None
            ),
            max_dark_per_camera=(
                list(self._ft_max_dark_per_camera)
                if self._ft_max_dark_per_camera is not None else None
            ),
        )
        output_dir = os.path.join(self._directory, "calibrations")
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
        self._test_scan_failure_reason = ""
        if result.canceled:
            self._test_scan_status = "aborted"
            self.captureLog.emit(
                f"⚠️ Test scan aborted: {result.error or 'canceled'}"
            )
        elif not result.ok:
            self._test_scan_status = "aborted"
            self.captureLog.emit(
                f"⚠️ Test scan aborted: {result.error or 'unknown error'}"
            )
        elif result.passed:
            self._test_scan_status = "done"
            self.captureLog.emit(
                f"✅ Test scan: PASS  (CSV: {result.csv_path})"
            )
        else:
            self._test_scan_status = "failed"
            if self._app_config.get("developerMode", False):
                tests = (("mean", "mean_test"), ("contrast", "contrast_test"),
                         ("ambient", "dark_test"))
                breakdown = "; ".join(
                    f"{'L' if r.side == 'left' else 'R'}{r.cam_id + 1}:"
                    f"{','.join(n for n, a in tests if getattr(r, a) == 'FAIL')}"
                    for r in result.rows
                    if any(getattr(r, a) == "FAIL" for _, a in tests)
                )
                if any(r.dark_test == "FAIL" for r in result.rows):
                    breakdown = f"too much ambient light — {breakdown}"
                self._test_scan_failure_reason = breakdown
            self.captureLog.emit(
                f"❌ Test scan: FAIL  (CSV: {result.csv_path})"
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
        if result.canceled:
            self._calibration_status = "aborted"
            self.captureLog.emit(
                f"⚠️ Calibration aborted: {result.error or 'canceled'}"
            )
        elif not result.ok:
            self._calibration_status = "aborted"
            self.captureLog.emit(
                f"⚠️ Calibration aborted: {result.error or 'unknown error'}"
            )
        elif result.passed:
            self._calibration_status = "passed"
            self.captureLog.emit(
                f"✅ Calibration: PASS  (CSV: {result.csv_path})"
            )
        else:
            self._calibration_status = "failed"
            if self._app_config.get("developerMode", False):
                tests = (("mean", "mean_test"), ("contrast", "contrast_test"),
                         ("bfi", "bfi_test"), ("bvi", "bvi_test"),
                         ("ambient", "dark_test"))
                breakdown = "; ".join(
                    f"{'L' if r.side == 'left' else 'R'}{r.cam_id + 1}:"
                    f"{','.join(n for n, a in tests if getattr(r, a) == 'FAIL')}"
                    for r in result.rows
                    if any(getattr(r, a) == "FAIL" for _, a in tests)
                )
                # #122: dev-mode message must explicitly call out ambient-
                # light failures so operators don't misread an "ambient"
                # tag in the breakdown as a generic test name.
                if any(r.dark_test == "FAIL" for r in result.rows):
                    breakdown = f"too much ambient light — {breakdown}"
                self._calibration_failure_reason = breakdown
            self.captureLog.emit(
                f"❌ Calibration: FAIL  (CSV: {result.csv_path})"
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
        self.calibrationStateChanged.emit()

    @property
    def interface(self):
        return self._interface

    # ── App update checking ─────────────────────────────────────────────

    _GITHUB_REPO = "OpenwaterHealth/openmotion-bloodflow-app"

    @pyqtSlot()
    def checkForUpdates(self):
        """Check GitHub releases for a newer version (runs in a background thread)."""
        t = threading.Thread(target=self._check_for_updates_worker, daemon=True)
        t.start()

    def _check_for_updates_worker(self):
        import urllib.request
        from version import get_version

        api_url = f"https://api.github.com/repos/{self._GITHUB_REPO}/releases/latest"
        try:
            req = urllib.request.Request(api_url, headers={"Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())

            remote_tag = data.get("tag_name", "").lstrip("v")
            if not remote_tag:
                self.updateCheckFailed.emit("Could not determine latest release tag.")
                return

            # Find the .zip asset download URL
            download_url = data.get("html_url", "")
            for asset in data.get("assets", []):
                if asset["name"].endswith(".zip"):
                    download_url = asset["browser_download_url"]
                    break

            local_version = get_version()
            # Strip local metadata for comparison (e.g. "+3.gabc1234.dirty")
            local_base = local_version.split("+")[0]

            if self._version_newer(remote_tag, local_base):
                logger.info(f"Update available: {remote_tag} (current: {local_base})")
                self.updateAvailable.emit(remote_tag, download_url)
            else:
                logger.info(f"App is up to date ({local_base} >= {remote_tag})")
                self.updateNotAvailable.emit()

        except Exception as e:
            logger.warning(f"Update check failed: {e}")
            self.updateCheckFailed.emit(str(e))

    @staticmethod
    def _version_newer(remote: str, local: str) -> bool:
        """Return True if remote version is strictly newer than local.

        Handles versions like '0.4.3', 'pre-0.4.3', '1.0-pre3', and
        setuptools_scm-style dev strings like '1.2.0-dev.1-0-gabc1234-dirty'.
        Compares the leading dotted-numeric core; any suffix after it
        marks a pre-release, which counts as older than the same base
        version. (The old int() parse raised on dev suffixes and fell
        back to [0], making every remote release look newer.)
        """
        def parse(v):
            is_pre = v.startswith("pre-")
            base = v[4:] if is_pre else v
            m = re.match(r"(\d+(?:\.\d+)*)(.*)", base)
            if not m:
                return [0], is_pre
            parts = [int(x) for x in m.group(1).split(".")]
            if m.group(2):
                is_pre = True
            return parts, is_pre

        r_parts, r_pre = parse(remote)
        l_parts, l_pre = parse(local)

        if r_parts != l_parts:
            return r_parts > l_parts
        # Same base version: non-pre > pre
        if l_pre and not r_pre:
            return True
        return False

    @pyqtSlot(str)
    def openDownloadUrl(self, url: str):
        """Open the download URL in the system browser."""
        import webbrowser
        webbrowser.open(url)
