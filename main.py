import sys
import os
import asyncio
import json
import warnings
import logging
import datetime

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMessageBox
from PyQt6.QtQml import QQmlApplicationEngine, qmlRegisterSingletonInstance
from PyQt6.QtCore import qInstallMessageHandler, QtMsgType
from qasync import QEventLoop

from motion_connector import MOTIONConnector
from utils.single_instance import check_single_instance, cleanup_single_instance
from version import get_version
from utils.resource_path import resource_path


APP_VERSION = get_version()


logger = logging.getLogger("openmotion.bloodflow-app")
logger.setLevel(logging.INFO)  # or INFO depending on what you want to see

# Suppress PyQt6 DeprecationWarnings related to SIP
warnings.simplefilter("ignore", DeprecationWarning)


# Wire up the things that get logged out of QT app to the proper logs
def qt_message_handler(msg_type, context, message):
    """Custom Qt message handler to forward QML console.log() messages to the run log."""
    # Map Qt message types to logging levels
    log_level_map = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    # Get the logging level (default to INFO for console.log)
    log_level = log_level_map.get(msg_type, logging.INFO)

    qml_message = f"QML: {message}"

    logger = logging.getLogger("openmotion.bloodflow-app.qml-console")
    logger.setLevel(logging.INFO)  # or INFO depending on what you want to see
    logger.info(qml_message)


def _load_app_config() -> dict:
    """Load application config from config/app_config.json. Returns defaults if missing or invalid."""
    defaults = {
        "forceLaserFail": False,
        "cameraTempAlertThresholdC": 105,
        "sensorDebugLogging": False,
        "cameraFakeData": False,
        "output_path": None,  # None = use cwd; str = base directory for scan_data, app-logs, run-logs
        "histoThrottle": False,
        "histoCmp": False,
        "powerOffUnusedCameras": False,
        "commVerbose": False,  # Enable cmd id and "." prints from MCU
        "verboseCommandHandling": False,  # Enable printf in MCU command handlers
        "eol_min_mean_per_camera": [0] * 8,
        "eol_min_contrast_per_camera": [0] * 8,
        "leftMask": 0x66,   # 0b01100110 — cameras 2,3,6,7 (Middle pattern)
        "rightMask": 0x66,
        "uncorrectedOnly": False,
        "autoConfigureOnStartup": True,
        "developerMode": False,
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
        "invertPlotAxes": True,
        "autoScale": False,
        "autoScalePerPlot": False,
        "fdaMode": False,
        "plotWindowSec": 15,
        "bfiColor": "#E74C3C",
        "bviColor": "#3498DB",
    }
    config_path = resource_path("config", "app_config.json")
    if not config_path.exists():
        logger.info("No app_config.json found at %s, using defaults", config_path)
        return defaults
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        out = {
            **defaults,
            **{k: v for k, v in loaded.items() if k in defaults or k == "output_path"},
        }
        # Ensure mask fields are always integers (guard against float drift from JSON)
        for key in ("leftMask", "rightMask"):
            if key in out and out[key] is not None:
                out[key] = int(out[key])
        logger.info("Loaded app config from %s", config_path)
        return out
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(
            "Could not load app config from %s: %s; using defaults", config_path, e
        )
        return defaults


def main():
    # Check if another instance is already running
    if not check_single_instance():
        # Create a minimal QApplication to show message box
        app = QApplication(sys.argv)
        msg_box = QMessageBox()
        msg_box.setIcon(QMessageBox.Icon.Warning)
        msg_box.setWindowTitle("OpenWater Bloodflow")
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
    output_base = app_config.get("output_path")
    if not output_base:
        # Default to cwd, but fall back to ~/Documents/OpenWater Bloodflow
        # if cwd is not writable (e.g. launched from Finder where cwd is "/")
        candidate = os.getcwd()
        if os.access(candidate, os.W_OK):
            output_base = candidate
        else:
            output_base = os.path.join(
                os.path.expanduser("~"), "Documents", "OpenWater Bloodflow"
            )
    run_dir = os.path.join(output_base, "app-logs")
    os.makedirs(run_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )  # Build timestamp like 20251029_124455
    logfile_path = os.path.join(run_dir, f"ow-bloodflowapp-{ts}.log")

    file_handler = logging.FileHandler(logfile_path, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.info(f"logging to {logfile_path}")

    # Configure the SDK logger hierarchy to use the same handlers
    sdk_logger = logging.getLogger("openmotion.sdk")
    sdk_logger.setLevel(logging.INFO)
    sdk_logger.addHandler(console_handler)
    sdk_logger.addHandler(file_handler)
    sdk_logger.propagate = False  # Don't propagate to root, use our handlers

    # Log host system info now that handlers are in place
    from motion_singleton import motion_interface
    motion_interface.log_system_info()

    qInstallMessageHandler(qt_message_handler)

    app = QApplication(sys.argv)

    # Windows-specific: Set application user model ID for proper taskbar grouping
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "OpenWaterHealth.BloodflowApp"
            )
        except Exception:
            pass  # Ignore if not available

    icon_path = str(resource_path("assets", "images", "favicon.ico"))
    app.setWindowIcon(QIcon(icon_path))

    # Set application properties for Windows taskbar
    app.setApplicationName("OpenWater Bloodflow")
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("OpenWater Health")

    engine = QQmlApplicationEngine()

    connector = MOTIONConnector(app_config=app_config, output_path=output_base)
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MOTIONInterface", connector)
    engine.rootContext().setContextProperty("appVersion", APP_VERSION)

    # Load the QML file
    engine.load(str(resource_path("main.qml")))

    if not engine.rootObjects():
        logger.error("Error: Failed to load QML file")
        sys.exit(-1)

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    async def main_async():
        logger.info("Starting MOTION monitoring...")
        await connector._interface.start_monitoring()

    async def shutdown():
        logger.info("Shutting down MOTION monitoring...")
        connector._interface.stop_monitoring()

        pending_tasks = [t for t in asyncio.all_tasks() if not t.done()]
        if pending_tasks:
            logger.info(f"Cancelling {len(pending_tasks)} pending tasks...")
            for task in pending_tasks:
                task.cancel()
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        logger.info("LIFU monitoring stopped. Application shutting down.")

    def handle_exit():
        logger.info("Application closing...")
        # Cease scan, stop console trigger, turn off camera modules before monitoring stops
        try:
            connector.shutdown()
        except Exception as e:
            logger.warning("Error during connector shutdown: %s", e)
        asyncio.ensure_future(shutdown()).add_done_callback(lambda _: loop.stop())
        engine.deleteLater()  # Ensure QML engine is destroyed
        cleanup_single_instance()  # Clean up single-instance lock

    app.aboutToQuit.connect(handle_exit)

    try:
        with loop:
            loop.run_until_complete(main_async())
            loop.run_forever()
    except RuntimeError as e:
        if "Event loop stopped before Future completed" in str(e):
            logger.warning(
                "App closed while a Future was still running (safe to ignore)"
            )
        else:
            logger.error(f"Runtime error: {e}")
    except KeyboardInterrupt:
        logger.info("Application interrupted by user.")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
