from PyQt6.QtCore import (
    QObject,
    pyqtSignal,
    pyqtProperty,
    pyqtSlot,
    QVariant,
    QThread,
    QTimer,
)
from pathlib import Path
import logging
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

from omotion.Interface import MOTIONInterface

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
from utils.resource_path import resource_path
import numpy as np
import pandas as pd

# constants for calculations
SCALE_V = 0.0909
SCALE_I = 0.25
V_REF = 2.459  # Should be 2.5V but empirical measurements don't match
R_1 = 18000  # (R221)
R_2 = 8160  # (R224)
R_3 = 49900  # (R225)
R230 = 300e3
R234 = 300e3
R_s = 0.020  # (R217)
TEC_VOLTAGE_DEFAULT = -0.07  # volts (DVT1a=-0.07, EVT2=1.16)
DATA_ACQ_INTERVAL = 1.0

_BFI_CAL = VisualizeBloodflow(left_csv="", right_csv="")
_BFI_C_MIN = _BFI_CAL.C_min
_BFI_C_MAX = _BFI_CAL.C_max
_BFI_I_MIN = _BFI_CAL.I_min
_BFI_I_MAX = _BFI_CAL.I_max

# Global loggers - will be configured by _configure_logging method
logger = logging.getLogger("openmotion.bloodflow-app.connector")
run_logger = logging.getLogger("bloodflow-app.runlog")

# Define system states
DISCONNECTED = 0
SENSOR_CONNECTED = 1
CONSOLE_CONNECTED = 2
READY = 3
RUNNING = 4


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
    sessionIdChanged = pyqtSignal()  # Signal to notify QML of session ID changes
    subjectIdChanged = pyqtSignal()  # Deprecated alias — same as sessionIdChanged
    sensorDeviceInfoReceived = pyqtSignal(str, str)  # (fw_version, device_id)
    consoleDeviceInfoReceived = pyqtSignal(str, str)  # (fw_version, device_id)
    temperatureSensorUpdated = pyqtSignal(float)  # Temperature data
    accelerometerSensorUpdated = pyqtSignal(float, float, float)  # (x, y, z)
    gyroscopeSensorUpdated = pyqtSignal(float, float, float)  # (x, y, z)
    rgbStateReceived = pyqtSignal(int, str)  # (state, state_text)
    errorOccurred = pyqtSignal(str)
    vizFinished = pyqtSignal()
    visualizingChanged = pyqtSignal(bool)

    configProgress = pyqtSignal(int)
    configLog = pyqtSignal(str)
    configFinished = pyqtSignal(bool, str)

    # capture signals
    captureProgress = pyqtSignal(int)  # 0..100
    captureLog = pyqtSignal(str)  # log lines
    captureFinished = pyqtSignal(bool, str, str, str)  # ok, error, leftPath, rightPath
    scanNotesChanged = pyqtSignal()
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
    scanBviCorrectedSampled = pyqtSignal(
        str, int, float, float
    )  # side, cam_id, timestamp_s, bvi  (kept for backward compat)
    scanCorrectedBatch = pyqtSignal('QVariantList')  # list of {side,camId,frameId,ts,bfi,bvi}
    scanCameraTemperature = pyqtSignal(str, int, float)  # side, cam_id, temperature_c

    # post-processing signals
    postProgress = pyqtSignal(int)
    postLog = pyqtSignal(str)
    postFinished = pyqtSignal(bool, str, str, str)  # ok, err, leftCsv, rightCsv

    pduMonChanged = pyqtSignal()

    tecStatusChanged = pyqtSignal()
    tecDacChanged = pyqtSignal()
    appConfigChanged = pyqtSignal()

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
        interface: MOTIONInterface,
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
        self._running = False
        self._trigger_state = "OFF"
        self._state = DISCONNECTED
        self._last_fan_status: dict[str, bool | None] = {"left": None, "right": None}
        self.laser_params = self._load_laser_params(config_dir)
        self._tec_voltage_default = self._load_tec_params(config_dir)

        eol_mean     = cfg.get("eol_min_mean_per_camera")
        eol_contrast = cfg.get("eol_min_contrast_per_camera")
        self._eol_min_mean_per_camera     = list(eol_mean)     if isinstance(eol_mean,     (list, tuple)) else None
        self._eol_min_contrast_per_camera = list(eol_contrast) if isinstance(eol_contrast, (list, tuple)) else None

        self._post_thread = None
        self._post_cancel = threading.Event()

        self._capture_thread = None
        self._capture_stop = threading.Event()
        self._capture_running = False
        self._safety_cancel_scheduled = False  # True after scheduling cancel-due-to-safety; cleared when capture ends
        self._capture_left_path = ""
        self._capture_right_path = ""
        self._scan_notes = ""
        self._scan_notes_path = ""  # path to current scan's notes file on disk
        self._scan_workflow.set_realtime_calibration(
            _BFI_C_MIN, _BFI_C_MAX, _BFI_I_MIN, _BFI_I_MAX
        )
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

        self._subject_id = self.generate_session_id()
        logger.info(f"[Connector] Generated session ID: {self._subject_id}")

        # Emit synthetic connect events for devices already connected at startup
        if self._leftSensorConnected:
            self.on_connected("SENSOR_LEFT", "startup")
        if self._rightSensorConnected:
            self.on_connected("SENSOR_RIGHT", "startup")
        if self._consoleConnected:
            self.on_connected("CONSOLE", "startup")

        self._interface.console_module.telemetry.add_listener(self._on_telemetry_update)

    def set_eol_thresholds(
        self,
        min_mean_per_camera=None,
        min_contrast_per_camera=None,
    ):
        """Set EOL test thresholds per camera (index 0-7). None or list of up to 8 numbers."""
        self._eol_min_mean_per_camera = (
            min_mean_per_camera
            if isinstance(min_mean_per_camera, (list, tuple))
            else None
        )
        self._eol_min_contrast_per_camera = (
            min_contrast_per_camera
            if isinstance(min_contrast_per_camera, (list, tuple))
            else None
        )

    def _configure_logging(self, log_level):

        run_logger.propagate = True
        # --- Load RT model (10K3CG_R-T.CSV) for TEC lookup ---
        try:
            # Look for file in the repository's models directory next to this file
            base_dir = os.path.dirname(__file__)
            candidate = os.path.join(base_dir, "models", "10K3CG_R-T.CSV")
            if not os.path.exists(candidate):
                # try lower-case extension variant
                candidate = os.path.join(base_dir, "models", "10K3CG_R-T.csv")

            if os.path.exists(candidate):
                df = pd.read_csv(candidate)
                self._data_RT = np.array(df)
                logger.info(
                    f"Loaded RT model from {candidate} shape={self._data_RT.shape}"
                )
            else:
                self._data_RT = None
                logger.warning(f"RT model file not found at {candidate}")
        except Exception as e:
            self._data_RT = None
            logger.error(f"Failed to load RT model: {e}")

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
            sensor = (
                self._interface.sensors.get(side)
                if self._interface and self._interface.sensors
                else None
            )
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
            sensor = (
                self._interface.sensors.get(side)
                if self._interface and self._interface.sensors
                else None
            )
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
        base_subject = subject_id or self._subject_id or "unknown"
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

        # Initialize CSV telemetry log (same basename as run log)
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
            fw_ver = self._interface.console_module.get_version()
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
    def getSessionId(self) -> str:
        return self._subject_id

    def setSessionId(self, value: str):
        if not value:
            return
        # normalize to "ow" + alphanumerics (uppercase)
        if value.startswith("ow"):
            rest = value[2:]
        else:
            rest = value
        rest = "".join(ch for ch in rest.upper() if ch.isalnum())
        new_val = "ow" + rest
        if new_val != self._subject_id:
            self._subject_id = new_val
            self.sessionIdChanged.emit()
            self.subjectIdChanged.emit()  # keep deprecated alias working

    sessionId = pyqtProperty(
        str, fget=getSessionId, fset=setSessionId, notify=sessionIdChanged
    )

    # Deprecated alias — external code that still uses subjectId keeps working
    def getSubjectId(self) -> str:
        return self._subject_id

    def setSubjectId(self, value: str):
        self.setSessionId(value)

    subjectId = pyqtProperty(
        str, fget=getSubjectId, fset=setSubjectId, notify=subjectIdChanged
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

    @pyqtProperty(int, notify=stateChanged)
    def state(self):
        """Expose state as a QML property."""
        return self._state

    @pyqtProperty(str, notify=triggerStateChanged)
    def triggerState(self):
        return self._trigger_state

    # --- DEVICE CONNECTION / DISCONNECTION / STATE MANAGEMENT METHODS ---
    @pyqtSlot(str, str)
    def on_connected(self, descriptor, port):
        """Handle device connection."""
        logger.info(f"Device connected: {descriptor} on port {port}")
        desc = descriptor.upper()
        if desc == "SENSOR_LEFT":
            self._leftSensorConnected = True
            self._schedule_sensor_init("left")
        elif desc == "SENSOR_RIGHT":
            self._rightSensorConnected = True
            self._schedule_sensor_init("right")
        elif desc == "CONSOLE":
            self._consoleConnected = True
            self._interface.log_console_info()
            if self._interface.console_module.tec_voltage(self._tec_voltage_default):
                logger.info(f"Console TEC voltage set to {self._tec_voltage_default}V")
            else:
                logger.error(
                    f"Failed to set console TEC voltage to {self._tec_voltage_default}V"
                )
            if self._interface.console_module.set_fan_speed(fan_speed=100):
                logger.info("Console fan speed set to 50%")
            else:
                logger.error("Failed to set console fan speed")

        self.signalConnected.emit(descriptor, port)
        self.connectionStatusChanged.emit()
        self.update_state()

    @pyqtSlot(str, str)
    def on_disconnected(self, descriptor, port):
        """Handle device disconnection."""
        if descriptor.upper() == "SENSOR_LEFT":
            self._leftSensorConnected = False
            self._last_fan_status["left"] = None
            try:
                sensor = (
                    self._interface.sensors.get("left")
                    if self._interface and self._interface.sensors
                    else None
                )
                if (
                    sensor is not None
                    and getattr(sensor, "clear_id_cache", None) is not None
                ):
                    sensor.clear_id_cache()
            except Exception:
                pass
        elif descriptor.upper() == "SENSOR_RIGHT":
            self._rightSensorConnected = False
            self._last_fan_status["right"] = None
            try:
                sensor = (
                    self._interface.sensors.get("right")
                    if self._interface and self._interface.sensors
                    else None
                )
                if (
                    sensor is not None
                    and getattr(sensor, "clear_id_cache", None) is not None
                ):
                    sensor.clear_id_cache()
            except Exception:
                pass
        elif descriptor.upper() == "CONSOLE":
            self._consoleConnected = False
            # If console disconnects during an active capture, cancel it to prevent blank screen
            if self._capture_running:
                logger.warning(
                    "Console disconnected during active capture - cancelling scan to prevent UI freeze"
                )
                self.stopCapture()

        logger.info(
            f"Device disconnected: {descriptor} on port {port} and state is {self._state}"
        )
        self.signalDisconnected.emit(descriptor, port)
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

    @pyqtSlot(result=list)
    def get_scan_list(self):
        """Return sorted list of scan IDs.

        Supports two filename formats:
          New: {YYYYMMDD_HHMMSS}_{sessionId}_corrected.csv
          Old: scan_{sessionId}_{YYYYMMDD_HHMMSS}_corrected.csv
        """
        base_path = Path(self._directory)
        if not base_path.exists():
            return []

        ids = []
        for f in base_path.glob("*_corrected.csv"):
            if not f.is_file():
                continue
            stem = f.stem  # strip ".csv" → "..._corrected"
            if not stem.endswith("_corrected"):
                continue
            stem = stem[:-10]  # strip "_corrected"

            if stem.startswith("scan_"):
                # Old format: scan_{sessionId}_{ts}
                stem = stem[5:]

            ids.append(stem)

        def ts_key(s):
            # New format starts with YYYYMMDD (8 digits)
            if re.match(r'^\d{8}_\d{6}', s):
                return s[:15]       # YYYYMMDD_HHMMSS
            # Old format: sessionId_YYYYMMDD_HHMMSS
            parts = s.split("_", 1)
            return parts[1] if len(parts) == 2 else s

        return sorted(ids, key=ts_key, reverse=True)

    @pyqtSlot(str, result=QVariant)
    def get_scan_details(self, scan_id: str):
        """
        scan_id is either:
          New format: 'YYYYMMDD_HHMMSS_sessionId'
          Old format: 'sessionId_YYYYMMDD_HHMMSS'
        """
        base = Path(self._directory)

        # Detect format by checking if it starts with a date
        if re.match(r'^\d{8}_\d{6}_', scan_id):
            # New format: YYYYMMDD_HHMMSS_sessionId
            parts = scan_id.split("_", 2)
            ts = parts[0] + "_" + parts[1]
            subject = parts[2] if len(parts) > 2 else ""
            notes_path = base / f"{scan_id}_notes.txt"
            left      = next(base.glob(f"{scan_id}_left_mask*.csv"), None)
            right     = next(base.glob(f"{scan_id}_right_mask*.csv"), None)
            corrected = next(base.glob(f"{scan_id}_corrected.csv"), None)
        else:
            # Old format: sessionId_YYYYMMDD_HHMMSS
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
            m = re.search(r"_mask([0-9A-Fa-f]+)\.csv$", left.name)
            if m:
                left_mask = m.group(1)
        if right:
            m = re.search(r"_mask([0-9A-Fa-f]+)\.csv$", right.name)
            if m:
                right_mask = m.group(1)

        notes = ""
        try:
            notes = notes_path.read_text(encoding="utf-8")
        except Exception:
            pass

        return {
            "sessionId": subject,
            "subjectId": subject,   # deprecated alias kept for compatibility
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

    def generate_session_id(self):
        suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        return f"ow{suffix}"

    def generate_subject_id(self):  # deprecated alias
        return self.generate_session_id()

    @pyqtSlot()
    def newSession(self):
        """Generate a fresh session ID and clear notes for a new scan."""
        self._subject_id = self.generate_session_id()
        self._scan_notes = ""
        self._scan_notes_path = ""
        self.sessionIdChanged.emit()
        self.subjectIdChanged.emit()
        self.scanNotesChanged.emit()
        logger.info(f"New session started: {self._subject_id}")

    # --- CONSOLE COMMUNICATION METHODS ---
    @pyqtSlot()
    def queryConsoleInfo(self):
        """Fetch and emit device information."""
        try:
            fw_version = self._interface.console_module.get_version()
            logger.info(f"Version: {fw_version}")
            hw_id = self._interface.console_module.get_hardware_id()
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

        if self._capture_running or self._capture_thread is not None:
            self.captureLog.emit("Capture already running.")
            return False

        if self._safetyFailure:
            self.captureLog.emit(
                "Scan cannot start: laser safety system is tripped. Clear the safety interlock first."
            )
            return False

        try:
            os.makedirs(data_dir, exist_ok=True)
        except Exception as e:
            self.captureLog.emit(f"Failed to create data dir: {e}")
            return False

        self._capture_stop = threading.Event()
        self._capture_running = True
        self._capture_start_time = time.time()
        self._capture_left_path = ""
        self._capture_right_path = ""
        self._start_runlog(subject_id=subject_id)

        def _extra_cols():
            snap = self._interface.console_module.telemetry.get_snapshot()
            if snap is not None:
                return [int(snap.tcm), int(snap.tcl), f"{float(snap.pdc):.3f}"]
            return [0, 0, "0.000"]

        temp_alerted_by_side = {"left": set(), "right": set()}

        def _on_uncorrected(sample):
            """Fires for every non-dark frame (~40 Hz). Feeds the realtime plot."""
            current_side = sample.side
            alerted = temp_alerted_by_side.setdefault(current_side, set())
            threshold = self._camera_temp_alert_threshold_c
            if sample.temperature_c >= threshold and sample.cam_id not in alerted:
                alerted.add(sample.cam_id)
                msg = (
                    f"ALERT: Camera {sample.cam_id + 1} ({current_side}) "
                    f"temperature {sample.temperature_c:.1f}°C >= {threshold:.0f}°C threshold."
                )
                self.captureLog.emit(msg)
                run_logger.warning(msg)
                logger.warning(msg)

            self.scanMeanSampled.emit(
                current_side,
                int(sample.cam_id),
                float(sample.timestamp_s),
                float(sample.mean),
            )
            self.scanContrastSampled.emit(
                current_side,
                int(sample.cam_id),
                float(sample.timestamp_s),
                float(sample.contrast),
            )

            self.scanBfiSampled.emit(
                sample.side,
                int(sample.cam_id),
                int(sample.absolute_frame_id),
                float(sample.timestamp_s),
                float(sample.bfi),
            )
            self.scanBviSampled.emit(
                sample.side,
                int(sample.cam_id),
                int(sample.absolute_frame_id),
                float(sample.timestamp_s),
                float(sample.bvi),
            )
            self.scanCameraTemperature.emit(
                sample.side,
                int(sample.cam_id),
                float(sample.temperature_c),
            )

        def _on_corrected_batch(batch):
            """Fires every ~15 s with dark-frame-corrected values for the last interval."""
            payload = []
            for s in batch.samples:
                payload.append({
                    'side': s.side,
                    'camId': int(s.cam_id),
                    'frameId': int(s.absolute_frame_id),
                    'ts': float(s.timestamp_s),
                    'bfi': float(s.bfi),
                    'bvi': float(s.bvi),
                })
            self.scanCorrectedBatch.emit(payload)

        def _on_complete(result):
            if result.ok:
                self.captureLog.emit("Capture session complete.")
            elif result.canceled:
                self.captureLog.emit("Scan stopped.")
            else:
                if result.error:
                    self.captureLog.emit(f"Capture error: {result.error}")

            # Compute scan duration and append to notes
            elapsed = time.time() - self._capture_start_time
            hours = int(elapsed // 3600)
            minutes = int((elapsed % 3600) // 60)
            seconds = int(elapsed % 60)
            duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            status = "completed" if result.ok else ("stopped" if result.canceled else "error")
            duration_line = f"\n---\nScan {status} — duration: {duration_str}"
            self._scan_notes = (self._scan_notes.strip() + duration_line)
            self.scanNotesChanged.emit()

            # Always write the notes file so that the scan is discoverable in
            # the history viewer regardless of whether data CSVs were produced.
            try:
                notes_filename = (
                    f"{result.scan_timestamp}_{subject_id}_notes.txt"
                )
                notes_path = os.path.join(data_dir, notes_filename)
                with open(notes_path, "w", encoding="utf-8") as nf:
                    nf.write(self._scan_notes.strip() + "\n")
                self._scan_notes_path = notes_path
                logger.info(f"Saved scan notes to {notes_path}")
            except Exception as e:
                logger.error(f"Failed to save scan notes: {e}")

            if result.ok:
                try:
                    self._log_scan_image_stats(result.left_path, result.right_path)
                except Exception as e:
                    logger.error(f"Failed to compute scan image stats: {e}")

            self._capture_left_path = result.left_path
            self._capture_right_path = result.right_path
            self._capture_running = False
            self._safety_cancel_scheduled = False
            self._capture_thread = None
            self.captureFinished.emit(
                bool(result.ok), result.error or "", result.left_path, result.right_path
            )
            self._stop_runlog()

        req = ScanRequest(
            subject_id=subject_id,
            duration_sec=duration_sec,
            left_camera_mask=left_camera_mask,
            right_camera_mask=right_camera_mask,
            data_dir=data_dir,
            disable_laser=disable_laser,
            write_raw_csv=self._write_raw_csv,
            raw_csv_duration_sec=self._raw_csv_duration_sec,
        )

        def _on_trigger_state(state: str):
            self._trigger_state = state
            self.triggerStateChanged.emit()

        started = self._interface.start_scan(
            req,
            extra_cols_fn=_extra_cols,
            on_log_fn=lambda msg: self.captureLog.emit(msg),
            on_progress_fn=lambda pct: self.captureProgress.emit(int(pct)),
            on_trigger_state_fn=_on_trigger_state,
            on_uncorrected_fn=_on_uncorrected,
            on_corrected_batch_fn=None if self._uncorrected_only else _on_corrected_batch,
            on_error_fn=lambda e: self.captureLog.emit(f"Capture error: {e}"),
            on_side_stream_fn=lambda side, filepath: self.captureLog.emit(
                f"[{side.upper()}] Streaming to: {os.path.basename(filepath)}"
            ),
            on_complete_fn=_on_complete,
        )
        if not started:
            self._capture_running = False
            self._stop_runlog()
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
        eol_rows = []

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
            sensor = (
                self._interface.sensors.get(side_key)
                if self._interface and self._interface.sensors
                else None
            )
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

            # EOL thresholds: use cam_id (0-7) to index per-camera minimums
            cam_idx = cid if cid >= 0 else idx
            min_mean = None
            min_contrast = None
            if self._eol_min_mean_per_camera and cam_idx < len(
                self._eol_min_mean_per_camera
            ):
                min_mean = self._eol_min_mean_per_camera[cam_idx]
            if self._eol_min_contrast_per_camera and cam_idx < len(
                self._eol_min_contrast_per_camera
            ):
                min_contrast = self._eol_min_contrast_per_camera[cam_idx]

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

            eol_rows.append(
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

        # Write CSV to app-logs/eol-test-csvs
        try:
            eol_dir = os.path.join(self._output_base, "app-logs", "eol-test-csvs")
            os.makedirs(eol_dir, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            eol_path = os.path.join(eol_dir, f"eol-test-{ts}.csv")
            with open(eol_path, "w", newline="", encoding="utf-8") as f:
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
                w.writerows(eol_rows)
            logger.info(f"Scan image stats CSV written to {eol_path}")
            run_logger.info(f"Scan image stats CSV written to {eol_path}")
        except Exception as e:
            logger.warning(f"Failed to write EOL test CSV: {e}")
            run_logger.warning(f"Failed to write EOL test CSV: {e}")

        # Emit a single end-of-scan EOL verdict to the Qt capture log window.
        overall_eol_pass = bool(eol_rows) and all(
            row.get("mean_test") == "PASS" and row.get("contrast_test") == "PASS"
            for row in eol_rows
        )
        eol_result = "PASS" if overall_eol_pass else "FAIL"
        status_emoji = "✅" if overall_eol_pass else "❌"
        eol_msg = f"{status_emoji} EOL criteria result: {eol_result}"
        self.captureLog.emit(eol_msg)
        logger.info(eol_msg)
        run_logger.info(eol_msg)

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
            snap = self._interface.console_module.telemetry.get_snapshot()
        if snap is None or not snap.read_ok:
            return False

        v, i, p, t, ok = (
            snap.tec_v_raw, snap.tec_set_raw,
            snap.tec_curr_raw, snap.tec_volt_raw, snap.tec_good,
        )

        R_TH = (
            1 / ((float(v) / (V_REF / 2 * R_3)) - 1 / R_3 + 1 / R_1) - R_2
        )
        Thermistor_Temp = np.interp(
            R_TH, self._data_RT[:, 1][::-1], self._data_RT[:, 0][::-1]
        )

        R_SET = (
            1 / ((float(i) / (V_REF / 2 * R_3)) - 1 / R_3 + 1 / R_1) - R_2
        )
        SET_Temp = np.interp(
            R_SET, self._data_RT[:, 1][::-1], self._data_RT[:, 0][::-1]
        )

        self._tec_voltage = round(float(Thermistor_Temp), 2)
        self._tec_temp = round(float(SET_Temp), 2)
        self._tec_monC = round((float(p) - 0.5 * V_REF) / (25 * R_s), 3)
        self._tec_monV = round((float(t) - 0.5 * V_REF) * 4, 3)
        self._tec_good = bool(ok)

        self.tecStatusChanged.emit()
        return True

    @pyqtSlot(result=QVariant)
    def pdu_mon(self, snap=None):
        if snap is None:
            snap = self._interface.console_module.telemetry.get_snapshot()
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
            snap = self._interface.console_module.telemetry.get_snapshot()
        if snap is None:
            logger.warning("readSafetyStatus: no telemetry snapshot yet")
            return
        try:
            if snap.safety_ok:
                if self._safetyFailure:
                    self.safetyFailure = False
            else:
                if not self._safetyFailure:
                    self.safetyFailure = True
                    self.stopTrigger()
                    self.laserStateChanged.emit(False)
                    if self._capture_running and not self._safety_cancel_scheduled:
                        self.safetyTripDuringCaptureRequested.emit()
        except Exception as e:
            logger.error(f"readSafetyStatus failed: {e}")
            self.safetyFailure = True
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

            if target == "CONSOLE":
                fpga_data, fpga_data_len = (
                    self._interface.console_module.read_i2c_packet(
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
                    # logger.info(f"Read I2C Success")
                    # logger.info(f"Raw bytes: {fpga_data.hex(' ')}")  # Print as hex bytes separated by spaces
                    return list(fpga_data[:fpga_data_len])

            elif target == "SENSOR_LEFT" or target == "SENSOR_RIGHT":
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
            if self._interface.console_module.set_rgb_led(state) == state:
                logger.info(f"RGB state set to: {state}")
            else:
                logger.error(f"Failed to set RGB state to: {state}")
        except Exception as e:
            logger.error(f"Error setting RGB state: {e}")

    @pyqtSlot()
    def queryRGBState(self):
        """Fetch and emit RGB state."""
        try:
            state = self._interface.console_module.get_rgb_led()
            state_text = {0: "Off", 1: "IND1", 2: "IND2", 3: "IND3"}.get(
                state, "Unknown"
            )

            logger.info(f"RGB State: {state_text}")
            self.rgbStateReceived.emit(state, state_text)  # Emit both values
        except Exception as e:
            logger.error(f"Error querying RGB state: {e}")

    @pyqtSlot(result=QVariant)
    def queryTriggerConfig(self):
        trigger_setting = self._interface.console_module.get_trigger_json()
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

            trigger_setting = self._interface.console_module.set_trigger_json(
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
        success = self._interface.console_module.start_trigger()
        if success:
            self._trigger_state = "ON"
            self.triggerStateChanged.emit()
            logger.info("Trigger started successfully.")
        return success

    @pyqtSlot()
    def stopTrigger(self):
        self._interface.console_module.stop_trigger()
        self._trigger_state = "OFF"
        self.triggerStateChanged.emit()
        self._stop_runlog()
        logger.info("Trigger stopped.")

    @pyqtSlot(result=int)
    def getFsyncCount(self):
        """Get the Fsync count from the console."""
        try:
            fsync_count = self._interface.console_module.get_fsync_pulsecount()
            logger.info(f"Fsync Count: {fsync_count}")
            return fsync_count
        except Exception as e:
            logger.error(f"Error getting Fsync count: {e}")
            return -1

    @pyqtSlot(result=int)
    def getLsyncCount(self):
        """Get the Fsync count from the console."""
        try:
            lsync_count = self._interface.console_module.get_lsync_pulsecount()
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
        logger.info("[Connector] Setting laser power from config...")
        for idx, laser_param in enumerate(self.laser_params, start=1):
            muxIdx = laser_param["muxIdx"]
            channel = laser_param["channel"]
            i2cAddr = laser_param["i2cAddr"]
            offset = laser_param["offset"]
            dataToSend = bytearray(laser_param["dataToSend"])

            logger.debug(
                f"[Connector] ({idx}/{len(self.laser_params)}) "
                f"Writing I2C: muxIdx={muxIdx}, channel={channel}, "
                f"i2cAddr=0x{i2cAddr:02X}, offset=0x{offset:02X}, "
                f"data={list(dataToSend)}"
            )

            if not interface.console_module.write_i2c_packet(
                mux_index=muxIdx,
                channel=channel,
                device_addr=i2cAddr,
                reg_addr=offset,
                data=dataToSend,
            ):
                logger.error(
                    f"Failed to set laser power (muxIdx={muxIdx}, channel={channel})"
                )
                return False
        logger.info("Laser power set successfully.")
        return True

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

            # Get all sensors (left and right)
            sensors = []
            if self._leftSensorConnected and "left" in self._interface.sensors:
                sensors.append(("left", self._interface.sensors["left"]))
            if self._rightSensorConnected and "right" in self._interface.sensors:
                sensors.append(("right", self._interface.sensors["right"]))

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

    @pyqtSlot(int, int)
    def startConfigureCameraSensors(
        self, left_camera_mask: int, right_camera_mask: int
    ):
        if self._config_running:
            return
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

    @pyqtSlot()
    def cancelConfigureCameraSensors(self):
        if self._config_running:
            self._interface.cancel_configure_camera_sensors()

    def _on_config_finished(self, result):
        self._config_running = False
        self.configFinished.emit(bool(result.ok), result.error or "")

    @pyqtSlot(str)
    def querySensorAccelerometer(self, target: str):
        """Fetch and emit Accelerometer data."""
        try:
            if target == "SENSOR_LEFT" or target == "SENSOR_RIGHT":
                sensor_tag = "left" if target == "SENSOR_LEFT" else "right"
            else:
                logger.error(f"Invalid target for sensor info query: {target}")
                return

            # Check if sensor is connected
            if (sensor_tag == "left" and not self._leftSensorConnected) or (
                sensor_tag == "right" and not self._rightSensorConnected
            ):
                logger.error(f"{sensor_tag.capitalize()} sensor not connected")
                return

            sensor = self._interface.sensors[sensor_tag]
            if sensor is None:
                logger.error(f"{sensor_tag.capitalize()} sensor object is None")
                return
            accel = sensor.imu_get_accelerometer()
            logger.info(f"Accel (raw): X={accel[0]}, Y={accel[1]}, Z={accel[2]}")
            self.accelerometerSensorUpdated.emit(accel[0], accel[1], accel[2])
        except Exception as e:
            logger.error(f"Error querying Accelerometer data: {e}")

    @pyqtSlot()
    def querySensorGyroscope(self, target: str):
        """Fetch and emit Gyroscope data."""
        try:
            if target == "SENSOR_LEFT" or target == "SENSOR_RIGHT":
                sensor_tag = "left" if target == "SENSOR_LEFT" else "right"
            else:
                logger.error(f"Invalid target for sensor info query: {target}")
                return

            gyro = self._interface.sensors[sensor_tag].imu_get_gyroscope()
            logger.info(f"Gyro  (raw): X={gyro[0]}, Y={gyro[1]}, Z={gyro[2]}")
            self.gyroscopeSensorUpdated.emit(gyro[0], gyro[1], gyro[2])
        except Exception as e:
            logger.error(f"Error querying Gyroscope data: {e}")

    @pyqtSlot(str)
    def softResetSensor(self, target: str):
        """reset hardware Sensor device."""
        try:
            if target == "CONSOLE":
                if self._interface.console_module.soft_reset():
                    logger.info("Software Reset Sent")
                else:
                    logger.error("Failed to send Software Reset")
            elif target == "SENSOR_LEFT" or target == "SENSOR_RIGHT":
                sensor_tag = "left" if target == "SENSOR_LEFT" else "right"
                if self._interface.sensors[sensor_tag].soft_reset():
                    logger.info("Software Reset Sent")
                else:
                    logger.error("Failed to send Software Reset")
        except Exception as e:
            logger.error(f"Error Sending Software Reset: {e}")

    @pyqtSlot(str)
    def querySensorTemperature(self, target: str):
        """Fetch and emit Temperature data."""
        try:
            if target == "SENSOR_LEFT" or target == "SENSOR_RIGHT":
                sensor_tag = "left" if target == "SENSOR_LEFT" else "right"
            else:
                logger.error(f"Invalid target for sensor info query: {target}")
                return

            # Check if sensor is connected
            if (sensor_tag == "left" and not self._leftSensorConnected) or (
                sensor_tag == "right" and not self._rightSensorConnected
            ):
                logger.error(f"{sensor_tag.capitalize()} sensor not connected")
                return

            sensor = self._interface.sensors[sensor_tag]
            if sensor is None:
                logger.error(f"{sensor_tag.capitalize()} sensor object is None")
                return

            imu_temp = sensor.imu_get_temperature()
            logger.info(f"Temperature Data - IMU Temp: {imu_temp}")
            self.temperatureSensorUpdated.emit(imu_temp)
        except Exception as e:
            logger.error(f"Error querying Temperature data: {e}")

    @pyqtSlot(str)
    def querySensorInfo(self, target: str):
        """Fetch and emit device information."""
        try:
            if target == "SENSOR_LEFT" or target == "SENSOR_RIGHT":
                sensor_tag = "left" if target == "SENSOR_LEFT" else "right"
            else:
                logger.error(f"Invalid target for sensor info query: {target}")
                return

            # Check if sensor is connected
            if (sensor_tag == "left" and not self._leftSensorConnected) or (
                sensor_tag == "right" and not self._rightSensorConnected
            ):
                logger.error(f"{sensor_tag.capitalize()} sensor not connected")
                return

            sensor = self._interface.sensors[sensor_tag]
            if sensor is None:
                logger.error(f"{sensor_tag.capitalize()} sensor object is None")
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
                result = self._interface.sensors["left"].set_fan_control(fan_on)
            elif sensor_side.lower() == "right":
                if not self._rightSensorConnected:
                    logger.error("Right sensor not connected")
                    return False
                result = self._interface.sensors["right"].set_fan_control(fan_on)
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
        try:
            if sensor_side.lower() == "left":
                if not self._leftSensorConnected:
                    logger.error("Left sensor not connected")
                    return False
                status = self._interface.sensors["left"].get_fan_control_status()
            elif sensor_side.lower() == "right":
                if not self._rightSensorConnected:
                    logger.error("Right sensor not connected")
                    return False
                status = self._interface.sensors["right"].get_fan_control_status()
            else:
                logger.error(f"Invalid sensor side: {sensor_side}")
                return False

            if status != self._last_fan_status.get(sensor_side.lower()):
                self._last_fan_status[sensor_side.lower()] = status
                logger.info(
                    f"Fan status for {sensor_side} sensor: {'ON' if status else 'OFF'}"
                )
            return status

        except Exception as e:
            logger.error(f"Error getting fan control status: {e}")
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

    @pyqtSlot(str, str)
    def on_data_received(self, descriptor, message):
        """Handle incoming data from the LIFU device."""
        logger.info(f"Data received from {descriptor}: {message}")
        self.signalDataReceived.emit(descriptor, message)

    def connect_signals(self):
        """Connect LIFUInterface signals to QML."""
        self._interface.signal_connect.connect(self.on_connected)
        self._interface.signal_disconnect.connect(self.on_disconnected)
        self._interface.signal_data_received.connect(self.on_data_received)
        self.safetyTripDuringCaptureRequested.connect(
            self._on_safety_trip_during_capture
        )

    @property
    def interface(self):
        return self._interface


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
            cells = mod._active_cells(df, active_sides)
            row_map, col_map, n_rows, n_cols = mod._collapse(cells)
            self.resultsReady.emit({
                "mod": mod,
                "df": df,
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


