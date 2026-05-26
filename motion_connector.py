from PyQt6.QtCore import (
    QObject,
    pyqtSignal,
    pyqtProperty,
    pyqtSlot,
    QVariant,
    QThread,
    QTimer,
    QRecursiveMutex,
)
from pathlib import Path
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
    FpgaModel,
    apply_laser_power_from_config,
    load_laser_params,
    load_tec_params,
)
from utils.resource_path import resource_path
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

# Global loggers - will be configured by _configure_logging method
logger = logging.getLogger("openmotion.bloodflow-app.connector")
run_logger = logging.getLogger("bloodflow-app.runlog")

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
    """Subscribes to the 'live' pipeline channel and emits per-frame Qt
    signals into the QML realtime plot for each active camera.

    The 'live' channel carries a FrameBatch after BfiBvi (and optionally
    SideAveraging in reduced mode). Each batch may contain multiple frames
    and both sides; we iterate frame × side × cam_id and call back into
    the connector's _emit_frame_to_qml helper, which gates on the camera-
    dropout watchdog and fires the Qt signals.
    """

    channels = {"live"}

    def __init__(self, connector: "MOTIONConnector", plot_t0: float):
        self._connector = connector
        self._plot_t0 = plot_t0
        self._temp_alerted: dict[tuple[str, int], bool] = {}

    def on_scan_start(self, meta) -> None:
        self._temp_alerted.clear()

    def consume(self, channel: str, batch) -> None:
        if channel != "live":
            return
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

                # Dropout gate — same logic as the old _on_uncorrected closure.
                _key = (side, cam_id)
                should_emit, recovery_msg = _check_dropped_camera_emit(
                    side, cam_id,
                    connector._camera_dropped,
                    connector._camera_dropped_recovery_logged,
                )
                if recovery_msg is not None:
                    logger.warning(recovery_msg)
                    run_logger.warning(recovery_msg)
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
                    run_logger.warning(msg)
                    logger.warning(msg)

                if not is_dark:
                    # mean / contrast: use display_mean (pedestal-subtracted)
                    # and contrast_sn_rt (shot-noise-corrected) when available.
                    if batch.display_mean is not None:
                        mean_val = float(batch.display_mean[i, side_idx, cam_id])
                        connector.scanMeanSampled.emit(side, cam_id, plot_ts, mean_val)
                    if batch.contrast_sn_rt is not None:
                        contrast_val = float(batch.contrast_sn_rt[i, side_idx, cam_id])
                        connector.scanContrastSampled.emit(side, cam_id, plot_ts, contrast_val)

                connector.scanBfiSampled.emit(side, cam_id, abs_frame_id, plot_ts, bfi)
                connector.scanBviSampled.emit(side, cam_id, abs_frame_id, plot_ts, bvi)
                connector.scanCameraTemperature.emit(side, cam_id, temp_c)

    def on_complete(self) -> None:
        pass


class _FinalBatchSink:
    """Subscribes to the 'final' pipeline channel and emits the
    scanCorrectedBatch Qt signal into the QML realtime plot
    (EmbeddedRealtimePlot.qml). Each batch corresponds to one closed
    dark interval; the QML side overwrites the previously-plotted
    realtime values with these more-accurate values, keyed by frame_id.
    """

    channels = {"final"}

    def __init__(self, connector: "MOTIONConnector", plot_t0: float):
        self._connector = connector
        self._plot_t0 = plot_t0

    def on_scan_start(self, meta) -> None:
        pass

    def consume(self, channel: str, batch) -> None:
        if channel != "final":
            return
        # Current SDK final payloads are EnrichedCorrectedInterval objects with
        # .frames; keep .samples fallback for older corrected-batch shims.
        connector = self._connector
        plot_ts = time.monotonic() - self._plot_t0
        payload = []
        samples = getattr(batch, "frames", None)
        if samples is None:
            samples = getattr(batch, "samples", ())
        for s in samples:
            side = str(getattr(s, "side", ""))
            cam_id = int(getattr(s, "cam_id", -1))
            should_emit, recovery_msg = _check_dropped_camera_emit(
                side, cam_id,
                connector._camera_dropped,
                connector._camera_dropped_recovery_logged,
            )
            if recovery_msg is not None:
                logger.warning(recovery_msg)
                run_logger.warning(recovery_msg)
            if not should_emit:
                continue
            payload.append({
                "side": side,
                "camId": cam_id,
                "frameId": int(getattr(s, "abs_frame_id", getattr(s, "absolute_frame_id", 0))),
                "ts": plot_ts,
                "bfi": float(getattr(s, "bfi", 0.0)),
                "bvi": float(getattr(s, "bvi", 0.0)),
            })
        if payload:
            connector.scanCorrectedBatch.emit(payload)

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

    def __init__(self, connector: "MOTIONConnector"):
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

    def __init__(self, connector: "MOTIONConnector", on_complete_cb):
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


class MOTIONConnector(QObject):
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
    vizFinished = pyqtSignal()
    visualizingChanged = pyqtSignal(bool)

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
    scanMeanSampled = pyqtSignal(
        str, int, float, float
    )  # side, cam_id, timestamp_s, mean
    scanContrastSampled = pyqtSignal(
        str, int, float, float
    )  # side, cam_id, timestamp_s, contrast
    scanBfiSampled = pyqtSignal(
        str, int, int, float, float
    )  # side, cam_id, frame_id, timestamp_s, bfi
    scanBviSampled = pyqtSignal(
        str, int, int, float, float
    )  # side, cam_id, frame_id, timestamp_s, bvi
    scanBfiCorrectedSampled = pyqtSignal(
        str, int, float, float
    )  # side, cam_id, timestamp_s, bfi  (kept for backward compat)

    # Contact-quality quick-check signals.
    contactQualityCheckStarted = pyqtSignal(int)  # expected duration in seconds
    contactQualityCheckFinished = pyqtSignal(bool, str, 'QVariantList')  # ok, error, warnings
    contactQualityWarning = pyqtSignal(str, str, str, float)  # camera, typeKey, typeText, value
    # Mid-scan CQ state change (active=True on activation, False on clear).
    # BloodFlow.qml's ``onContactQualityIssueStateChanged`` consumes the
    # ``False`` edge to clear an entry from the live modal.
    contactQualityIssueStateChanged = pyqtSignal(str, str, str, float, bool)
    contactQualityScanInProgress = pyqtSignal(bool)
    scanBviCorrectedSampled = pyqtSignal(
        str, int, float, float
    )  # side, cam_id, timestamp_s, bvi  (kept for backward compat)
    scanCorrectedBatch = pyqtSignal('QVariantList')  # list of {side,camId,frameId,ts,bfi,bvi}
    scanCameraTemperature = pyqtSignal(str, int, float)  # side, cam_id, temperature_c
    cameraDropoutDetected = pyqtSignal(str, int, str)  # side ("left"/"right"), cam_id (0-7), elapsed HH:MM:SS

    # post-processing signals
    postProgress = pyqtSignal(int)
    postLog = pyqtSignal(str)
    postFinished = pyqtSignal(bool, str, str, str)  # ok, err, leftCsv, rightCsv

    pduMonChanged = pyqtSignal()

    tecStatusChanged = pyqtSignal()
    tecDacChanged = pyqtSignal()
    appConfigChanged = pyqtSignal()

    # App update signals
    updateAvailable = pyqtSignal(str, str)   # (latest_version, download_url)
    updateNotAvailable = pyqtSignal()
    updateCheckFailed = pyqtSignal(str)      # error message

    @staticmethod
    def _default_output_base() -> str:
        """Return a writable base directory for logs and scan data.

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
        output_path=None,
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

        # 1 Hz watchdog timer — started/stopped around the scan lifecycle.
        self._dropout_timer = QTimer(self)
        self._dropout_timer.setInterval(1000)
        self._dropout_timer.timeout.connect(self._on_dropout_check)

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
        self._output_base                 = output_path or cfg.get("output_path") or self._default_output_base()
        self._power_off_unused_cameras    = bool(cfg.get("powerOffUnusedCameras", False))
        self._write_raw_csv               = bool(cfg.get("writeRawCsv", True))
        raw_csv                           = cfg.get("rawCsvDurationSec")
        self._raw_csv_duration_sec        = float(raw_csv) if raw_csv is not None else None
        self._uncorrected_only            = bool(cfg.get("uncorrectedOnly", False))

        # Configure logging with the provided level
        self._configure_logging(log_level)

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
        # Track console connection time for safety grace period (issue #107 follow-up)
        self._console_connected_at: float | None = None

        self.laser_params = load_laser_params(config_dir, force_fault=self._force_laser_fail)
        self._tec_voltage_default = load_tec_params(config_dir)
        # Load FPGA model (preferred JSON, with legacy JS fallback)
        self._fpga = FpgaModel()
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
        self._scan_notes_path = ""  # path to current scan's notes file on disk
        self.connect_signals()
        self._viz_thread = None
        self._viz_worker = None

        self._tec_voltage = 0.0
        self._tec_temp = 0.0
        self._tec_monV = 0.0
        self._tec_monC = 0.0
        self._tec_good = False

        self._tec_dac = 0.0

        self._pdu_raws = [0] * 16
        self._pdu_vals = [0.0] * 16

        # --- per-trigger run log support ---
        self._runlog_handler = None  # logging.FileHandler or None
        self._runlog_path = None  # str or None
        self._runlog_active = False  # bool
        self._runlog_csv_path = None  # str or None
        self._runlog_csv_file = None  # open file handle or None
        self._runlog_csv_writer = None  # csv.writer or None
        self._runlog_csv_lock = threading.Lock()

        configured_data_dir = cfg.get("dataDirectory")
        if configured_data_dir:
            os.makedirs(configured_data_dir, exist_ok=True)
            self._directory = configured_data_dir
        else:
            default_dir = os.path.join(self._output_base, "scan_data")
            os.makedirs(default_dir, exist_ok=True)
            self._directory = default_dir
        logger.info(f"[Connector] Directory initialized to: {self._directory}")

        self._user_label = self.generate_user_label()
        logger.info(f"[Connector] Generated user label: {self._user_label}")

        # Note: synthetic startup connect events for already-attached
        # devices are no longer needed. The new SDK lifecycle is:
        #   1. main.py constructs MotionInterface
        #   2. main.py constructs MOTIONConnector (this); we subscribe
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

    def _configure_logging(self, log_level):

        run_logger.propagate = True
        # TEC RT lookup now lives in omotion.console_telemetry_conversions
        # (lazy-loaded from the SDK wheel's omotion/models/10K3CG_R-T.csv).

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
        # self._interface.log_sensor_info(side)
        self.connectionStatusChanged.emit()

    def _start_runlog(self, subject_id: str = None):
        """
        Create a dedicated run log file and attach it to the global logger
        so that all logger.info / logger.error etc. also go into this file
        while the trigger is running.
        """
        if self._runlog_active:
            # Already running; nothing to do
            return

        # Directory for individual trigger runs
        run_dir = os.path.join(self._output_base, "run-logs")
        os.makedirs(run_dir, exist_ok=True)

        # Timestamped filename for this specific trigger session
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        base_subject = subject_id or self._user_label or "unknown"
        safe_subject = re.sub(r"[^A-Za-z0-9_-]", "", base_subject)
        self._runlog_path = os.path.join(run_dir, f"run-{safe_subject}_{ts}.log")
        self._runlog_csv_path = os.path.join(run_dir, f"run-{safe_subject}_{ts}.csv")

        # Create handler with immediate flushing (delay=False ensures file is opened immediately)
        run_handler = logging.FileHandler(
            self._runlog_path, mode="w", encoding="utf-8", delay=False
        )
        # Match the global formatter you already defined at top of file
        run_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        )

        run_handler.setLevel(logging.INFO)

        # Attach this handler to run_logger ONLY
        run_logger.addHandler(run_handler)

        # Ensure run_logger has a level set (in case it wasn't configured)
        if run_logger.level == logging.NOTSET:
            run_logger.setLevel(logging.INFO)

        # Save so we can remove/close it later
        self._runlog_handler = run_handler
        self._runlog_active = True

        # Initialize CSV telemetry log (same basename as run log).
        # Issue #43: clinical users don't need per-scan TCM / TCL / PDC
        # samples — gate the file creation on developerMode. Skipping
        # the open leaves _runlog_csv_writer as None, which
        # _write_runlog_csv_sample already null-checks, so per-update
        # writes become no-ops.
        if self._app_config.get("developerMode", False):
            try:
                self._runlog_csv_file = open(
                    self._runlog_csv_path, "w", newline="", encoding="utf-8"
                )
                self._runlog_csv_writer = csv.writer(self._runlog_csv_file)
                self._runlog_csv_writer.writerow(
                    ["timestamp", "unix_ms", "tcm", "tcl", "pdc"]
                )
                self._runlog_csv_file.flush()
            except Exception as e:
                logger.error(f"Failed to open run CSV log: {e}")
                self._runlog_csv_file = None
                self._runlog_csv_writer = None
        else:
            self._runlog_csv_file = None
            self._runlog_csv_writer = None
            self._runlog_csv_path = None

        # --- Gather version info for header ---
        # SDK version (MOTION SDK / sensor SDK)
        try:
            sdk_ver = (
                self._interface.get_sdk_version()
            )  # same as get_sdk_version() slot :contentReference[oaicite:4]{index=4}
        except Exception as e:
            sdk_ver = f"ERROR({e})"

        # App version (from constant we defined at top)
        try:
            from main import APP_VERSION

            app_ver = APP_VERSION  # from main.py
        except Exception as e:
            app_ver = f"ERROR({e})"

        # Console firmware version (from console module) :contentReference[oaicite:5]{index=5}
        try:
            fw_ver = self._interface.console.get_version()
        except Exception as e:
            fw_ver = f"ERROR({e})"

        #
        # Write session header into the run log
        #
        run_logger.info("=" * 80)
        run_logger.info("RUN START")
        run_logger.info("=" * 80)
        run_logger.info(f"App Version: {app_ver}")
        run_logger.info(f"SDK Version: {sdk_ver}")
        run_logger.info(f"Console Firmware: {fw_ver}")

        self._read_and_log_camera_uids()

        # Flush the handler to ensure header is written immediately
        try:
            self._runlog_handler.flush()
        except Exception as e:
            logger.error(f"Error flushing run log handler after header: {e}")

        # Also drop a breadcrumb to the main logger so humans see it in console/UI log:
        logger.info(f"[RUNLOG] started -> {self._runlog_path}")

    def _stop_runlog(self):
        """
        Detach and close the per-run file handler.
        """
        if not self._runlog_active or self._runlog_handler is None:
            return

        # Mark end of run in the run log
        run_logger.info(f"[RUNLOG] Trigger run logging stopped -> {self._runlog_path}")
        run_logger.info("========== RUN END ==========")

        # Also note it in the main logger (console/app log)
        logger.info(f"[RUNLOG] stopped -> {self._runlog_path}")

        # Flush the handler before removing it to ensure all data is written
        try:
            self._runlog_handler.flush()
        except Exception as e:
            logger.error(f"Error flushing run log handler: {e}")

        # 1. Remove handler from run_logger
        try:
            run_logger.removeHandler(self._runlog_handler)
        except Exception as e:
            logger.error(f"Error detaching run log handler: {e}")

        # 2. Close the handler so the file is flushed and released
        try:
            self._runlog_handler.close()
        except Exception as e:
            logger.error(f"Error closing run log handler: {e}")

        # 3. Clear state
        self._runlog_handler = None
        self._runlog_path = None
        self._runlog_active = False

        # Close CSV telemetry log
        with self._runlog_csv_lock:
            if self._runlog_csv_file is not None:
                try:
                    self._runlog_csv_file.flush()
                except Exception as e:
                    logger.error(f"Error flushing run CSV log: {e}")
                try:
                    self._runlog_csv_file.close()
                except Exception as e:
                    logger.error(f"Error closing run CSV log: {e}")
            self._runlog_csv_file = None
            self._runlog_csv_writer = None
            self._runlog_csv_path = None

    def _write_runlog_csv_sample(
        self, tcm: int, tcl: int, pdc: float, timestamp: float
    ):
        if not self._runlog_active or self._runlog_csv_writer is None:
            return
        iso_ts = datetime.datetime.fromtimestamp(timestamp).isoformat(
            timespec="milliseconds"
        )
        unix_ms = int(timestamp * 1000)
        with self._runlog_csv_lock:
            if self._runlog_csv_writer is None:
                return
            try:
                self._runlog_csv_writer.writerow(
                    [iso_ts, unix_ms, tcm, tcl, f"{pdc:.3f}"]
                )
                self._runlog_csv_file.flush()
            except Exception as e:
                logger.error(f"Failed to write run CSV sample: {e}")

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
                    else:
                        logger.error("Failed to set console fan speed")
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
            run_logger.info(
                "TEC Status – temp: %.2f set: %.2f tec_c: %.3f tec_v: %.3f good: %s",
                self._tec_voltage, self._tec_temp,
                snap.tec_curr_raw, snap.tec_volt_raw, snap.tec_good,
            )
        except Exception as exc:
            logger.error("_on_telemetry_update TEC error: %s", exc)

        try:
            self.pdu_mon(snap)
            if snap.pdu_volts:
                run_logger.info(
                    "PDU MON ADC0 vals: %s",
                    " ".join(f"{(v / SCALE_V):.3f}" for v in snap.pdu_volts[:8]),
                )
                adc1_scaled = [
                    (v / SCALE_V) if idx == 6 else (v / SCALE_I)
                    for idx, v in enumerate(snap.pdu_volts[8:])
                ]
                run_logger.info(
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
            run_logger.info(
                "Analog Values – TCM: %d, TCL: %d, PDC: %.3f",
                snap.tcm, snap.tcl, snap.pdc,
            )
            self._write_runlog_csv_sample(snap.tcm, snap.tcl, snap.pdc, snap.timestamp)
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

        try:
            self._stop_runlog()
        except Exception as e:
            logger.warning("Error stopping run log: %s", e)

        self._capture_thread = None

    @pyqtSlot()
    def shutdown(self):
        """Shutdown connector. Stops capture, stops monitoring, then disconnects all devices."""
        logger.info("Shutting down MOTIONConnector...")
        self.stopCapture()

        try:
            if self._interface:
                self._interface.stop_monitoring()
                logger.info("USB monitoring stopped.")
        except Exception as e:
            logger.warning("Error stopping monitoring: %s", e)

        try:
            if self._interface:
                self._interface.disconnect()
        except Exception as e:
            logger.warning("Error disconnecting interface: %s", e)

        logger.info("MOTIONConnector shutdown complete.")

    # --- SCAN MANAGEMENT METHODS ---
    @pyqtSlot(result=list)
    def _load_laser_params(self, config_dir):
        filename = (
            "laser_params_fault.json" if self._force_laser_fail else "laser_params.json"
        )
        config_path = (
            resource_path("config", filename)
            if config_dir == "config"
            else Path(config_dir) / filename
        )
        if not config_path.exists():
            logger.error(f"[Connector] Laser parameter file not found: {config_path}")
            return []

        try:
            with open(config_path, "r") as f:
                params = json.load(f)
            logger.info(
                f"[Connector] Loaded {len(params)} laser parameter sets from {config_path}"
            )
            return params
        except FileNotFoundError:
            logger.error(f"[Connector] Laser parameter file not found: {config_path}")
            return []
        except json.JSONDecodeError as e:
            logger.error(f"[Connector] Invalid JSON in {config_path}: {e}")
            return []

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
        """Return sorted list of scan IDs.

        Supports three filename formats for the canonical scan CSV:
          New (post-#44): {YYYYMMDD_HHMMSS}_{sessionId}.csv
          Mid:            {YYYYMMDD_HHMMSS}_{sessionId}_corrected.csv
          Legacy:         scan_{sessionId}_{YYYYMMDD_HHMMSS}_corrected.csv
        """
        base_path = Path(self._directory)
        if not base_path.exists():
            return []

        seen: set[str] = set()
        ids: list[str] = []
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

        notes = ""
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
        }

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
        # Always persist to disk when a notes file path exists, even if the
        # in-memory value didn't change (covers the first save after capture).
        if self._scan_notes_path:
            try:
                with open(self._scan_notes_path, "w", encoding="utf-8") as nf:
                    nf.write(self._scan_notes.strip() + "\n")
                logger.info(f"Notes saved to disk: {self._scan_notes_path}")
            except Exception as e:
                logger.error(f"Failed to update scan notes on disk: {e}")

    @pyqtSlot(str, result=int)
    @pyqtSlot(str, str, result=int)
    @pyqtSlot(str, str, int, result=int)
    @pyqtSlot(str, str, int, bool, result=int)
    @pyqtSlot(str, str, int, bool, str, result=int)
    def notify(self, text: str, type_: str = "info", duration_ms: int = 4000,
               dismissible: bool = True, tag: str = "") -> int:
        """Fire a toast notification. Reachable from QML as MOTIONInterface.notify(...)
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
        self._scan_notes_path = ""
        self.scanNotesChanged.emit()
        self._capture_running = True
        self._capture_start_time = time.time()
        # Per-scan monotonic zero for plot timestamps. sample.timestamp_s comes
        # from each sensor's firmware clock, which resets on sensor reboot — so
        # after a mid-scan unplug/replug, the two sides' clocks diverge and the
        # QML plot's shared `latestTimestamp` prunes the lagging side to empty.
        plot_t0 = time.monotonic()
        self._capture_left_path = ""
        self._capture_right_path = ""
        self._start_runlog(subject_id=subject_id)

        # Camera dropout watchdog state — fresh per scan.
        self._camera_last_seen = {}
        self._camera_last_temp = {}
        self._camera_dropped = set()
        self._camera_dropped_recovery_logged = set()
        self._dropout_timer.start()

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
                duration_source = "trigger"
            else:
                elapsed = time.time() - self._capture_start_time
                duration_source = "wall-clock"
            hours = int(elapsed // 3600)
            minutes = int((elapsed % 3600) // 60)
            seconds = int(elapsed % 60)
            duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            status = "stopped" if canceled else "completed"
            duration_line = (
                f"\n---\nScan {status} — duration: {duration_str} "
                f"({duration_source})"
            )
            self._scan_notes = (self._scan_notes.strip() + duration_line)
            self.scanNotesChanged.emit()

            # Write scan notes file using scan_id from metadata.
            scan_id = getattr(meta, "scan_id", "") if meta else ""
            try:
                notes_filename = f"{scan_id}_{subject_id}_notes.txt"
                notes_path = os.path.join(data_dir, notes_filename)
                with open(notes_path, "w", encoding="utf-8") as nf:
                    nf.write(self._scan_notes.strip() + "\n")
                self._scan_notes_path = notes_path
                logger.info(f"Saved scan notes to {notes_path}")
            except Exception as e:
                logger.error(f"Failed to save scan notes: {e}")

            # New pipeline writes CSVs directly; no .raw→.csv post-processing
            # needed.  Pass empty paths to captureFinished so startPostProcess
            # is a no-op (QML still proceeds to the next scan step).
            self._capture_left_path = ""
            self._capture_right_path = ""
            self._capture_running = False
            self._safety_cancel_scheduled = False
            self._capture_thread = None
            self.captureFinished.emit(True, "", "", "")
            self._stop_runlog()
            self.scanNotesReady.emit()

        req = ScanRequest(
            subject_id=subject_id,
            duration_sec=duration_sec,
            left_camera_mask=left_camera_mask,
            right_camera_mask=right_camera_mask,
            disable_laser=disable_laser,
            reduced_mode=self._app_config.get("reducedMode", False),
            rolling_avg_window=int(
                (self._app_config or {}).get(
                    "cq_rolling_avg_window",
                    _CQ_DEFAULT_ROLLING_WINDOW,
                )
            ),
            # Raw CSV duration forwarded to the pipeline's Tee("raw") gate
            # via raw_save_max_duration_s. None means unbounded (write entire
            # scan); 0 omits raw tee entirely.
            raw_save_max_duration_s=(
                self._raw_csv_duration_sec if self._write_raw_csv else 0
            ),
            sinks=[
                _LivePlotSink(connector=self, plot_t0=plot_t0),
                _FinalBatchSink(connector=self, plot_t0=plot_t0),
                _TriggerStateSink(connector=self),
                _CompletionSink(connector=self, on_complete_cb=_on_pipeline_complete),
            ],
        )

        started = self._interface.start_scan(req)
        if not started:
            self._capture_running = False
            self._stop_runlog()
            # Log at WARNING so this is visible in the run log file —
            # captureLog signal goes through QML console.log which is
            # filtered out by default.
            logger.warning(
                "startCapture aborted: SDK refused to spawn a new scan "
                "(see ScanWorkflow.start_scan log for the underlying "
                "reason — usually a previous worker thread that didn't "
                "exit cleanly)."
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
        run_logger.info("Scan image stats per camera:")

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
                run_logger.info("  %s mean: %.0f", label, mean_val)
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
            ft_dir = os.path.join(self._output_base, "app-logs", "ft-test-csvs")
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
            run_logger.info(f"Scan image stats CSV written to {ft_path}")
        except Exception as e:
            logger.warning(f"Failed to write FT CSV: {e}")
            run_logger.warning(f"Failed to write FT CSV: {e}")

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
        run_logger.info(ft_msg)

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
        self._stop_runlog()
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

    @pyqtSlot(result=bool)
    def setLaserPowerFromConfig(self) -> bool:
        """Apply laser power parameters loaded at startup."""
        try:
            return self.set_laser_power_from_config(self._interface)
        except Exception as e:
            logger.error(f"setLaserPowerFromConfig error: {e}")
            return False

    def set_laser_power_from_config(self, interface):
        return apply_laser_power_from_config(
            interface, self.laser_params, self._fpga, self._console_mutex
        )

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

        if result is None:
            self.contactQualityCheckFinished.emit(False, "CQ check failed", [])
            return

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
                run_logger.warning(
                    "[DROPOUT] side=%s cam=%d temp=%.1f°C threshold=%.0fs at %s",
                    side, cam_id, temp, threshold, elapsed_str,
                )
                self.notify(
                    f"Camera {side.upper()} {cam_id + 1} connection lost at {elapsed_str}"
                    f" — last temp {temp:.1f}°C",
                    type_="warning",
                    duration_ms=30000,
                    tag=f"dropout_{side}_{cam_id}",
                )
                self._camera_dropped.add(key)
                self.cameraDropoutDetected.emit(side, cam_id, elapsed_str)

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
    def _read_and_log_camera_uids(self):
        """
        Read and log security UIDs for all connected cameras.
        This is called at the beginning of a scan.
        Logs to both the main logger and run_logger (if active).
        """
        try:
            logger.info("=== Reading camera security UIDs ===")
            if self._runlog_active:
                run_logger.info("=== Reading camera security UIDs ===")

            # Get all sensors (left and right) — handles are stable, gate
            # on the per-handle connected flag.
            sensors = []
            if self._leftSensorConnected:
                sensors.append(("left", self._interface.left))
            if self._rightSensorConnected:
                sensors.append(("right", self._interface.right))

            if not sensors:
                logger.warning("No sensors connected, cannot read camera UIDs")
                if self._runlog_active:
                    run_logger.warning("No sensors connected, cannot read camera UIDs")
                return

            # Read UIDs for all cameras (0-7) on each connected sensor.
            # Prefer cached values (populated at sensor init) to avoid polling at scan start.
            for sensor_name, sensor in sensors:
                logger.info(f"Reading camera UIDs from {sensor_name} sensor...")
                if self._runlog_active:
                    run_logger.info(f"Reading camera UIDs from {sensor_name} sensor...")
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
                            if self._runlog_active:
                                run_logger.info(
                                    f"  Camera {camera_id + 1}: Not present (UID: {display_uid})"
                                )
                            self.configLog.emit(f"Camera {camera_id + 1}: Not present")
                        else:
                            logger.info(
                                f"  Camera {camera_id + 1}: UID = {display_uid}"
                            )
                            if self._runlog_active:
                                run_logger.info(
                                    f"  Camera {camera_id + 1}: UID = {display_uid}"
                                )
                            self.configLog.emit(
                                f"Camera {camera_id + 1} UID: {display_uid}"
                            )
                    except Exception as e:
                        logger.error(
                            f"Error reading UID for camera {camera_id + 1} on {sensor_name} sensor: {e}"
                        )
                        if self._runlog_active:
                            run_logger.error(
                                f"Error reading UID for camera {camera_id + 1} on {sensor_name} sensor: {e}"
                            )

            logger.info("=== Camera UID read complete ===")
            if self._runlog_active:
                run_logger.info("=== Camera UID read complete ===")
        except Exception as e:
            logger.error(f"Error reading camera UIDs: {e}")
            if self._runlog_active:
                run_logger.error(f"Error reading camera UIDs: {e}")

    @pyqtSlot(int, int, result=bool)
    def startConfigureCameraSensors(
        self, left_camera_mask: int, right_camera_mask: int
    ) -> bool:
        err = self._ensure_idle()
        if err is not None:
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

    # --- BLOODFLOW VISUALIZATION / POST-PROCESSING METHODS ---
    @pyqtSlot(str, str, float, float, bool, result=bool)
    def visualize_bloodflow(
        self,
        left_csv: str,
        right_csv: str,
        t1: float = 0.0,
        t2: float = 120.0,
        plot_contrast: bool = False,
    ) -> bool:
        left_csv = (left_csv or "").strip()
        right_csv = (right_csv or "").strip()
        if left_csv.lower().endswith(".raw"):
            left_csv = left_csv[:-4] + ".csv"
        if right_csv.lower().endswith(".raw"):
            right_csv = right_csv[:-4] + ".csv"

        if not left_csv and not right_csv:
            self.errorOccurred.emit(
                "No files selected. Please pick a left and/or right CSV."
            )
            return False

        missing = []
        if left_csv and not Path(left_csv).exists():
            missing.append(f"Left file not found:\n{left_csv}")
        if right_csv and not Path(right_csv).exists():
            missing.append(f"Right file not found:\n{right_csv}")
        if missing:
            self.errorOccurred.emit("\n\n".join(missing))
            return False

        logger.info(
            f"Visualizing bloodflow: left_csv={left_csv}, right_csv={right_csv}, t1={t1}, t2={t2}, plot_contrast={plot_contrast}"
        )

        # start spinner
        self.visualizingChanged.emit(True)

        # start worker thread (compute only)
        self._viz_thread = QThread(self)
        self._viz_worker = _VizWorker(left_csv, right_csv, t1, t2, plot_contrast)
        self._viz_worker.moveToThread(self._viz_thread)

        # --- connections when starting the worker ---
        self._viz_thread.started.connect(self._viz_worker.run)
        self._viz_worker.resultsReady.connect(self._onVizResults)  # will pass 1 arg
        self._viz_worker.error.connect(self._onVizError)
        self._viz_worker.finished.connect(self._viz_thread.quit)
        self._viz_worker.finished.connect(self._viz_worker.deleteLater)
        self._viz_thread.finished.connect(self._viz_thread.deleteLater)
        self._viz_thread.start()
        return True

    @pyqtSlot(object)
    def _onVizResults(self, payload: dict):
        try:
            import matplotlib.pyplot as plt
            from processing.visualize_bloodflow import VisualizeBloodflow

            # Close any existing matplotlib figures to prevent multiple windows from old scans
            plt.close("all")

            bfi = payload["bfi"]
            bvi = payload["bvi"]
            camera_inds = payload["camera_inds"]
            contrast = payload["contrast"]
            mean = payload["mean"]
            nmodules = payload["nmodules"]
            t1 = payload["t1"]
            t2 = payload["t2"]

            viz = VisualizeBloodflow(left_csv="", right_csv="", t1=t1, t2=t2)
            viz._BFI = bfi
            viz._BVI = bvi
            viz._contrast = contrast
            viz._mean = mean
            viz._camera_inds = camera_inds
            viz._nmodules = nmodules
            viz._sides = payload.get("sides", [])
            plot_contrast = payload.get("plot_contrast", False)

            if plot_contrast:
                fig = viz.plot(("contrast", "mean"))
            else:
                fig = viz.plot(("BFI", "BVI"))
            plt.show(block=False)
        except Exception as e:
            logger.exception("Visualization display failed")
            self.errorOccurred.emit(f"Visualization display failed:\n{e}")
        finally:
            self.visualizingChanged.emit(False)
            self.vizFinished.emit()

    @pyqtSlot(str)
    def _onVizError(self, msg: str):
        self.visualizingChanged.emit(False)
        self.errorOccurred.emit(f"Visualization failed:\n{msg}")

    @pyqtSlot()
    def _onVizFinished(self):
        # Show the figure on the main thread
        try:
            import matplotlib.pyplot as plt

            plt.show(block=False)
        except Exception as e:
            logger.exception("Visualization display failed")
            self.errorOccurred.emit(f"Visualization display failed:\n{e}")
        finally:
            self.visualizingChanged.emit(False)
            self.vizFinished.emit()

    @pyqtSlot(str, result=bool)
    def visualize_corrected(self, corrected_csv: str) -> bool:
        """Plot BFI/BVI from a _corrected.csv using plot_corrected_scan from the SDK."""
        return self._launch_correct_viz(corrected_csv, mode="bfi")

    @pyqtSlot(str, result=bool)
    def visualize_corrected_signal(self, corrected_csv: str) -> bool:
        """Plot contrast/mean from a _corrected.csv using plot_corrected_scan from the SDK."""
        return self._launch_correct_viz(corrected_csv, mode="signal")

    def _launch_correct_viz(self, corrected_csv: str, mode: str) -> bool:
        corrected_csv = (corrected_csv or "").strip()
        if not corrected_csv:
            self.errorOccurred.emit("No corrected CSV file found for this scan.")
            return False
        if not Path(corrected_csv).exists():
            self.errorOccurred.emit(f"Corrected CSV not found:\n{corrected_csv}")
            return False

        logger.info(f"Visualizing corrected scan ({mode}): {corrected_csv}")
        self.visualizingChanged.emit(True)

        self._correct_viz_thread = QThread(self)
        self._correct_viz_worker = _CorrectVizWorker(corrected_csv, mode=mode)
        self._correct_viz_worker.moveToThread(self._correct_viz_thread)

        self._correct_viz_thread.started.connect(self._correct_viz_worker.run)
        self._correct_viz_worker.resultsReady.connect(self._onCorrectVizResults)
        self._correct_viz_worker.error.connect(self._onCorrectVizError)
        self._correct_viz_worker.finished.connect(self._correct_viz_thread.quit)
        self._correct_viz_worker.finished.connect(self._correct_viz_worker.deleteLater)
        self._correct_viz_thread.finished.connect(self._correct_viz_thread.deleteLater)
        self._correct_viz_thread.start()
        return True

    @pyqtSlot(object)
    def _onCorrectVizResults(self, payload: dict):
        try:
            import matplotlib.pyplot as plt
            plt.close("all")
            mod = payload["mod"]
            if payload.get("reduced", False):
                mod._make_reduced_figure(payload["df"], payload["active_sides"])
            else:
                kwargs = dict(
                    cells=payload["cells"],
                    row_map=payload["row_map"],
                    col_map=payload["col_map"],
                    n_rows=payload["n_rows"],
                    n_cols=payload["n_cols"],
                )
                mod._make_figure(payload["df"], mode=payload["mode"], **kwargs)
            plt.show(block=False)
        except Exception as e:
            logger.exception("Corrected scan visualization display failed")
            self.errorOccurred.emit(f"Visualization display failed:\n{e}")
        finally:
            self.visualizingChanged.emit(False)
            self.vizFinished.emit()

    @pyqtSlot(str)
    def _onCorrectVizError(self, msg: str):
        self.visualizingChanged.emit(False)
        self.errorOccurred.emit(f"Corrected visualization failed:\n{msg}")

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
        # scan / CQ flows do via SetTriggerLaserTask. Pass None so
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

        # Issue #108: apply laser-power params to the firmware before
        # calibration runs. The normal scan chain does this via
        # SetTriggerLaserTask in QML (after FlashSensorsTask, before
        # the actual scan), but the calibration path goes directly
        # from runCalibration → SDK CalibrationWorkflow and skips that
        # chain entirely. On a cold start — when no scan or Check has
        # programmed the laser channels yet — the calibration scan
        # would fire its trigger over an unprogrammed laser, every
        # camera would see only dark, and phase 1 would abort with
        # 'zero or negative aggregate'. Applying the params here is
        # idempotent; runs that already had a scan kick the same
        # values back in without harm.
        try:
            ok = self.set_laser_power_from_config(self._interface)
            if not ok:
                logger.warning(
                    "runCalibration: set_laser_power_from_config "
                    "returned False — proceeding anyway, but the "
                    "calibration scan will likely abort with "
                    "'zero or negative aggregate' if this is a cold "
                    "start. See issue #108."
                )
            else:
                logger.info("runCalibration: laser params applied")
        except Exception as e:
            logger.error(
                "runCalibration: applying laser params raised: %s — "
                "proceeding anyway", e
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

        # Same #108 laser-power cold-start guard the Calibrate path uses.
        try:
            ok = self.set_laser_power_from_config(self._interface)
            if not ok:
                logger.warning(
                    "runTestScan: set_laser_power_from_config returned "
                    "False — proceeding anyway, but the test scan will "
                    "likely abort with 'zero or negative aggregate' if "
                    "this is a cold start. See issue #108."
                )
            else:
                logger.info("runTestScan: laser params applied")
        except Exception as e:
            logger.error(
                "runTestScan: applying laser params raised: %s — "
                "proceeding anyway", e
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

        Handles versions like '0.4.3', 'pre-0.4.3', '1.0-pre3'.
        Strips 'pre-' prefix for numeric comparison; pre-releases are
        considered older than the same base version.
        """
        def parse(v):
            # Strip pre- prefix, track it
            is_pre = v.startswith("pre-")
            base = v[4:] if is_pre else v
            # Also handle "1.0-pre3" format
            if "-pre" in base:
                base = base.split("-pre")[0]
                is_pre = True
            try:
                parts = [int(x) for x in base.split(".")]
            except ValueError:
                parts = [0]
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


def _load_plot_corrected_scan():
    """Load plot_corrected_scan.py — bundled in processing/ for deployed builds,
    falling back to the sibling SDK repo for development."""
    import importlib.util
    candidates = [
        # Bundled with the deployed app (PyInstaller) and dev tree alike
        resource_path("processing", "plot_corrected_scan.py"),
        # Dev fallback: sibling openmotion-sdk checkout
        Path(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "openmotion-sdk", "data-processing", "plot_corrected_scan.py",
        )),
    ]
    script_path = next((p for p in candidates if Path(p).is_file()), None)
    if script_path is None:
        searched = "\n  ".join(str(p) for p in candidates)
        raise FileNotFoundError(
            f"plot_corrected_scan.py not found. Looked in:\n  {searched}"
        )
    spec = importlib.util.spec_from_file_location(
        "plot_corrected_scan", str(script_path)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _CorrectVizWorker(QObject):
    finished = pyqtSignal()
    error = pyqtSignal(str)
    resultsReady = pyqtSignal(object)

    def __init__(self, corrected_csv: str, mode: str = "bfi"):
        super().__init__()
        self.corrected_csv = corrected_csv
        self.mode = mode

    @pyqtSlot()
    def run(self):
        try:
            import pandas as pd
            mod = _load_plot_corrected_scan()
            df = pd.read_csv(self.corrected_csv)
            if "timestamp_s" not in df.columns:
                raise ValueError(
                    "'timestamp_s' column not found — is this a _corrected.csv file?"
                )
            active_sides = mod._requested_sides(df, "both")
            if not active_sides:
                raise ValueError("No camera data found in corrected CSV.")

            reduced = mod._is_reduced_mode(df)
            if reduced:
                if self.mode == "signal":
                    raise ValueError(
                        "Contrast/Mean visualization is not available for "
                        "reduced mode scans."
                    )
                self.resultsReady.emit({
                    "mod": mod,
                    "df": df,
                    "reduced": True,
                    "active_sides": active_sides,
                    "mode": self.mode,
                })
            else:
                cells = mod._active_cells(df, active_sides)
                row_map, col_map, n_rows, n_cols = mod._collapse(cells)
                self.resultsReady.emit({
                    "mod": mod,
                    "df": df,
                    "reduced": False,
                    "cells": cells,
                    "row_map": row_map,
                    "col_map": col_map,
                    "n_rows": n_rows,
                    "n_cols": n_cols,
                    "mode": self.mode,
                })
        except Exception as e:
            self.error.emit(str(e))
        finally:
            self.finished.emit()


# --- worker to run visualiztion ---
class _VizWorker(QObject):
    finished = pyqtSignal()
    error = pyqtSignal(str)
    resultsReady = pyqtSignal(object)  # emits a dict with arrays/metadata

    def __init__(self, left_csv, right_csv, t1, t2, plot_contrast=False):
        super().__init__()
        self.left_csv = left_csv
        self.right_csv = right_csv
        self.t1 = t1
        self.t2 = t2
        self.plot_contrast = plot_contrast

    @pyqtSlot()
    def run(self):
        try:
            from processing.visualize_bloodflow import VisualizeBloodflow

            # Convert empty strings to None for optional right_csv, but ensure left_csv is valid
            left_path = self.left_csv if self.left_csv else None
            right_path = self.right_csv if self.right_csv else None

            if not left_path and not right_path:
                self.error.emit("No valid CSV file provided for visualization")
                self.finished.emit()
                return

            viz = VisualizeBloodflow(left_path, right_path, t1=self.t1, t2=self.t2)
            viz.compute()

            # Save results CSV based on left_csv or right_csv naming rule
            if self.left_csv:
                new_file_name = re.sub(
                    r"_left.*\.csv$", "_bfi_results.csv", self.left_csv
                )
            else:
                new_file_name = re.sub(
                    r"_right.*\.csv$", "_bfi_results.csv", self.right_csv
                )
            viz.save_results_csv(new_file_name)
            logger.info(f"Results CSV saved to: {new_file_name}")

            bfi, bvi, cam_inds, contrast, mean = viz.get_results()
            payload = {
                "bfi": bfi,
                "bvi": bvi,
                "camera_inds": cam_inds,
                "contrast": contrast,
                "mean": mean,
                "nmodules": 2 if self.right_csv else 1,
                "sides": viz._sides,
                "freq": viz.frequency_hz,
                "t1": viz.t1,
                "t2": viz.t2,
                "plot_contrast": self.plot_contrast,
            }
            self.resultsReady.emit(payload)
            self.finished.emit()
        except Exception as e:
            logger.exception("VisualizeBloodflow worker failed")
            self.error.emit(str(e))
