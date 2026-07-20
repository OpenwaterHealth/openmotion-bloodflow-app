import sys
import os
import warnings
import logging
import datetime


# PyInstaller --windowed/--noconsole builds set sys.stdout and sys.stderr
# to None because there's no console attached. Any code that does
# `sys.stdout.write(...)` (including logging.StreamHandler) raises
# AttributeError: 'NoneType' object has no attribute 'write' on the
# first call. The SDK's shutdown path logs from a finally-block, so a
# crash there propagates as a CRITICAL "Unhandled Python exception" and
# terminates the bloodflow process mid-test.
#
# Fix: redirect None streams to a safe sink BEFORE any logging is set up.
# This must happen before any other import that might attach a logger.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8", buffering=1)
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8", buffering=1)


from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMessageBox
from PyQt6.QtQml import (
    QQmlApplicationEngine,
    qmlRegisterSingletonInstance,
    qmlRegisterSingletonType,
)
from PyQt6.QtCore import qInstallMessageHandler, QtMsgType, QUrl

from motion_connector import MotionConnector
from omotion import MotionInterface
from utils.single_instance import check_single_instance, cleanup_single_instance
from version import get_version
from utils.resource_path import resource_path
from utils import app_paths, config_store


APP_VERSION = get_version()

# Shipped baseline (defaults + read-only bundled config), captured at load so
# the connector can diff runtime changes against it when saving overrides.
_APP_CONFIG_BASELINE: dict = {}


logger = logging.getLogger("openmotion.bloodflow-app")
logger.setLevel(logging.INFO)  # or INFO depending on what you want to see

# Suppress PyQt6 DeprecationWarnings related to SIP
warnings.simplefilter("ignore", DeprecationWarning)


# Wire up the things that get logged out of QT app to the proper logs
def qt_message_handler(msg_type, context, message):
    """Forward QML messages to the SDK log at the matching severity.

    `console.log()` in QML is `QtDebugMsg` and is filtered out by default.
    Use `console.warn()` / `console.error()` from QML for things that
    should always reach the run log.
    """
    log_level_map = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }
    log_level = log_level_map.get(msg_type, logging.INFO)
    qml_logger = logging.getLogger("openmotion.bloodflow-app.qml-console")
    qml_logger.log(log_level, "QML: %s", message)


def _load_app_config() -> dict:
    """Load application config from config/app_config.json. Returns defaults if missing or invalid."""
    defaults = {
        "forceLaserFail": False,
        # QA/bench lever: sets DEBUG_FLAG_HISTO_STALL on both sensors so a
        # scan deterministically loses all camera data ~45 s in while USB
        # stays alive (sensor-fw#75) — the #248/#174 repro. Default off.
        "debugHistoStallTest": False,
        # In-app updater source overrides (default None => production GitHub
        # repo). updateRepo swaps the owner/repo; updateApiUrl fully overrides
        # the releases-latest endpoint (used by the local update-test server).
        "updateRepo": None,
        "updateApiUrl": None,
        "cameraTempAlertThresholdC": 105,
        # Whole-scan data-stall watchdog (issue #248): abort the scan with
        # E-303 when no camera delivers a frame for this many seconds while
        # the trigger is ON. <= 0 disables the abort.
        "scanDataStallTimeoutSec": 3,
        # Console over-temp trip (°C) pushed to the console user config on
        # connect. 0/missing disables the firmware trip, so this is validated
        # (1-60 °C) before any write; see motion_config.ensure_tec_trip.
        "tecTripTempC": 40,
        "sensorDebugLogging": False,
        "consoleDebugLogging": False,
        "cameraFakeData": False,
        "histoThrottle": False,
        "histoCmp": False,
        "deferHistoSend": False,
        "powerOffUnusedCameras": False,
        "commVerbose": False,  # Enable cmd id and "." prints from MCU
        "verboseCommandHandling": False,  # Enable printf in MCU command handlers
        "ft_min_mean_per_camera": [0] * 8,
        "ft_min_contrast_per_camera": [0] * 8,
        "ft_min_bfi_per_camera": [0.0] * 8,
        "ft_max_bfi_per_camera": None,
        "ft_min_bvi_per_camera": [0.0] * 8,
        "ft_max_bvi_per_camera": None,
        "ft_max_dark_per_camera": [3.0] * 8,
        "max_calibration_time_sec": 600,
        "calibration_scan_duration_sec": 15,
        "test_scan_duration_sec": 5,
        "calibration_scan_delay_sec": 1,
        "leftMask": 0x66,   # 0b01100110 — cameras 2,3,6,7 (Middle pattern)
        "rightMask": 0x66,
        "uncorrectedOnly": False,
        "engineeringMode": False,
        "showBfiBvi": True,
        "bfiMin": 0.0,
        "bfiMax": 10.0,
        "bviMin": 0.0,
        "bviMax": 10.0,
        "meanMin": 0.0,
        "meanMax": 500.0,
        "contrastMin": 0.0,
        "contrastMax": 1.0,
        "dataDirectory": None,
        "writeRawCsv": True,
        "rawCsvDurationSec": None,
        # Corrected per-cam CSV ({scan_id}.csv) is redundant now that
        # per-cam BFI/BVI lands in scans.db (the new viewer + past replay
        # read from there). Default off; set true to keep exporting it
        # for external analysis tools.
        "writeCorrectedCsv": False,
        # Seconds of live data held in memory per plot buffer before the
        # oldest half is ring-trimmed; older data then lazy-loads from the
        # scan DB (async, off the GUI thread) on pan-into-past. The old
        # 60 s default put the in-memory boundary a mere 30–60 s behind
        # live, so nearly EVERY zoom/pan interaction fell through to the
        # DB tail — with 16 cams × 40 Hz that re-bucketized ~10⁵ rows per
        # interaction and froze the app for seconds (issue #256). 900 s
        # keeps even the largest preset window (5 min) fully in memory
        # after trims (post-trim floor = 450 s) at a bounded cost of
        # ~0.7 MB per buffer (~58 MB for an All/All scan).
        "liveCacheMaxSeconds": 900,
        "autoScale": False,
        "autoScalePerPlot": False,
        # Y-axis tick labels on plot cells; runtime toggle in the ⋯ popup.
        "showAxisLabels": True,
        # Build-time flag: true keeps ALL writable state (config overrides,
        # logs, scan data/db) next to the exe, like the old un-installed
        # layout; false scatters it to %PROGRAMDATA%\Openwater (the
        # installed/MSI layout). Set by the build system per artifact type
        # (portable zip vs installer) — see scripts/build_common.ps1.
        "portableMode": False,
        "clinicalMode": False,
        "clinicalModeLeftMask": 0xC3,
        "clinicalModeRightMask": 0xC3,
        "plotWindowSec": 15,
        "bfiColor": "#E74C3C",
        "bviColor": "#3498DB",
        # 1-pole low-pass on the DISPLAYED BVI stream (live plots +
        # clinical side averages); scans.db/CSVs/replay stay raw. The
        # number is the only control (#228 — no enabled bool, no
        # Settings UI): missing/invalid → 20.0, <= 0 disables.
        "bviLowPassCutoffHz": 20.0,
        "bfiClampLow": 0.0,
        "bfiClampHigh": 10.0,
        "bviClampLow": 0.0,
        "bviClampHigh": 10.0,
        "darkMode": True,
        "cq_check_duration_sec": 1.0,
        "cq_rolling_avg_window": 10,
        "cq_dark_threshold_per_camera": [3.0] * 8,
        "cq_light_threshold_per_camera": [15.0] * 8,
        # Phase 2b: profile HUD overlay on the PlotViewer — sample
        # rate, paint-tick ms, avg canvas-paint ms, total points
        # painted. Gated on `engineeringMode && showProfiling` so clinical
        # users never see it.
        "showProfiling": False,
        # Critical-error bug report (see error_codes.py / CriticalErrorModal).
        "support_email": "support@openwater.health",
        "bug_report_smtp": None,
        # Startup connection watchdog (E-104/E-106).
        "connectionTimeoutSec": 30,
        "requireConsole": True,
        "minSensors": 1,
    }
    baseline, merged = config_store.load_app_config(defaults)

    # Dev-only launch overrides (e.g. Zed tasks for Clinical/Research x
    # always-portable). Packaged builds never set these env vars, so this
    # is a no-op in production; the build-time flip in build_common.ps1
    # remains the source of truth for shipped artifacts.
    if os.environ.get("OPENMOTION_PORTABLE") == "1":
        baseline["portableMode"] = True
        merged["portableMode"] = True
    if "OPENMOTION_CLINICAL" in os.environ:
        clinical = os.environ["OPENMOTION_CLINICAL"] == "1"
        baseline["clinicalMode"] = clinical
        merged["clinicalMode"] = clinical

    _APP_CONFIG_BASELINE.clear()
    _APP_CONFIG_BASELINE.update(baseline)
    logger.info(
        "Loaded app config (overrides from %s)",
        app_paths.local_config_path(bool(baseline.get("portableMode", False))),
    )
    return merged


def _app_icon() -> QIcon:
    """Application icon with a PNG fallback.

    Prefer the multi-size .ico (the taskbar wants the 16-48px frames).
    If the ICO fails to load — e.g. a packaged build missing Qt's ico
    image-format plugin — fall back to the 1024px PNG so the window
    never shows the generic Windows icon.
    """
    icon = QIcon(str(resource_path("assets", "images", "favicon.ico")))
    if icon.isNull() or not icon.availableSizes():
        icon = QIcon(str(resource_path("assets", "images", "favicon.png")))
    return icon


def main():
    # Set the Windows AppUserModelID as the very first thing, before any
    # QApplication (and thus any HWND) is created. Windows binds the taskbar
    # button to the process identity when the first window appears; setting
    # this after QApplication() races the shell and intermittently leaves the
    # taskbar showing the generic Windows icon (Explorer caches one icon per
    # AUMID). This must run before check_single_instance()'s message box too.
    #
    # The ".1" suffix retires the original "Openwater.OpenMotion" AUMID:
    # machines that ever ran a pre-1.4.0-dev.1 build (which set the AUMID
    # after QApplication and could lose the race) have the generic icon
    # cached under the old ID, and Explorer keeps serving that cached icon
    # to fixed builds forever. A new AUMID gets a fresh cache entry.
    # Keep in sync with the ShortcutProperty in installer/app.wxs.
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Openwater.OpenMotion.1"
            )
        except Exception:
            pass  # Ignore if not available

    # Check if another instance is already running
    if not check_single_instance():
        # Create a minimal QApplication to show message box
        app = QApplication(sys.argv)
        app.setWindowIcon(_app_icon())
        msg_box = QMessageBox()
        msg_box.setIcon(QMessageBox.Icon.Warning)
        msg_box.setWindowTitle("Open-Motion")
        msg_box.setText("Another instance of the application is already running.")
        msg_box.setInformativeText(
            "Please close the existing instance before opening a new one."
        )
        msg_box.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg_box.exec()
        sys.exit(1)

    os.environ["QT_QUICK_CONTROLS_STYLE"] = "Material"
    os.environ["QT_QUICK_CONTROLS_MATERIAL_THEME"] = "Dark"
    os.environ["QT_LOGGING_RULES"] = "qt.qpa.fonts=false"


    # Configure logging
    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    )
    # Configure console logging
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Configure file logging
    app_config = _load_app_config()
    # Single output root: dataDirectory, or app_paths.writable_root() (which
    # already applies the frozen/portable/dev-cwd precedence + the
    # ~/Documents fallback for an unwritable candidate — e.g. macOS Finder
    # launch where cwd is "/"). Two fixed children live under it: logs/
    # (this run's log file) and data/ (scans.db, scan CSVs, calibrations,
    # debug-bundles, downloaded updates).
    _data_dir = app_config.get("dataDirectory") or str(
        app_paths.writable_root(bool(app_config.get("portableMode", False)))
    )
    os.makedirs(_data_dir, exist_ok=True)
    run_dir = os.path.join(_data_dir, app_paths.LOGS_DIRNAME)
    os.makedirs(run_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )  # Build timestamp like 20251029_124455
    logfile_path = os.path.join(run_dir, f"open-motion-{ts}.log")

    file_handler = logging.FileHandler(logfile_path, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.info("=" * 64)
    logger.info("Open-Motion %s starting", APP_VERSION)
    logger.info("Log file:       %s", logfile_path)
    logger.info("Data directory: %s", _data_dir)
    logger.info("=" * 64)

    # Configure the SDK logger hierarchy to use the same handlers
    sdk_logger = logging.getLogger("openmotion.sdk")
    sdk_logger.setLevel(logging.INFO)
    sdk_logger.addHandler(console_handler)
    sdk_logger.addHandler(file_handler)
    sdk_logger.propagate = False  # Don't propagate to root, use our handlers

    # Construct the MotionInterface and inject into the connector below.
    # data_dir + scan_db_path point the new pipeline's default CsvSink and
    # ScanDBSink at <_data_dir>/data — the same folder the connector uses
    # for calibrations/debug-bundles (self._data_root).
    _scan_data_dir = os.path.join(_data_dir, app_paths.DATA_DIRNAME)
    os.makedirs(_scan_data_dir, exist_ok=True)
    _scan_db_path = os.path.join(_scan_data_dir, "scans.db")
    motion_interface = MotionInterface(
        data_dir=_scan_data_dir,
        scan_db_path=_scan_db_path,
        operator_id="bloodflow-app",
    )
    motion_interface.log_system_info()

    qInstallMessageHandler(qt_message_handler)

    app = QApplication(sys.argv)

    # AppUserModelID is set at the top of main() (before any QApplication /
    # HWND exists) so the taskbar icon binds reliably.
    app.setWindowIcon(_app_icon())

    # Set application properties for Windows taskbar. Display name reflects
    # the build variant: clinicalMode=false is the Research distribution.
    app_display_name = "Open-Motion" if app_config.get("clinicalMode", False) else "Open-Motion Research"
    app.setApplicationName(app_display_name)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("Openwater")

    engine = QQmlApplicationEngine()

    connector = MotionConnector(
        motion_interface, app_config=app_config, data_dir=_data_dir,
        baseline_config=_APP_CONFIG_BASELINE,
        app_version=APP_VERSION, log_path=logfile_path,
    )
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", connector)
    # AppTheme as a true QML singleton: one QObject with ~40 color
    # bindings instead of one instance per file (~24 of them, each
    # re-evaluating every binding on a darkMode flip). Registered into
    # the same OpenMotion module QML already imports for MotionInterface;
    # instantiated lazily on first use, after both registrations.
    qmlRegisterSingletonType(
        QUrl.fromLocalFile(str(resource_path("components", "AppTheme.qml"))),
        "OpenMotion", 1, 0, "AppTheme",
    )
    engine.rootContext().setContextProperty("appVersion", APP_VERSION)

    # Load the QML file
    engine.load(str(resource_path("main.qml")))

    if not engine.rootObjects():
        logger.error("Error: Failed to load QML file")
        sys.exit(-1)

    # wait=False: the QML window is already visible at this point (main.qml's
    # ApplicationWindow is `visible: true`) and Qt's event loop hasn't started
    # yet (app.exec() is below) — a blocking wait here starves Explorer's
    # taskbar icon/thumbnail negotiation for the freshly-shown window and can
    # leave it showing the generic icon (issue #223). Real console handshakes
    # take ~5s normally, well past the old 2s cap, so this reliably blocked on
    # any hardware-attached launch. Already-attached devices still reach the
    # UI via the same _on_handle_state_changed signal path as any hotplug.
    logger.info("Starting Motion monitoring...")
    motion_interface.start(wait=False)

    def handle_exit():
        logger.info("Application closing...")
        try:
            connector.shutdown()
        except Exception as e:
            logger.warning("Error during connector shutdown: %s", e)
        try:
            motion_interface.stop()
        except Exception as e:
            logger.warning("Error stopping MotionInterface: %s", e)
        engine.deleteLater()
        cleanup_single_instance()
        logger.info("=" * 64)
        logger.info("Open-Motion %s exited cleanly", APP_VERSION)
        logger.info("=" * 64)

    app.aboutToQuit.connect(handle_exit)

    try:
        sys.exit(app.exec())
    except KeyboardInterrupt:
        logger.info("Application interrupted by user.")


if __name__ == "__main__":
    main()
