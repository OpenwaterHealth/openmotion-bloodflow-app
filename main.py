import argparse
import datetime
import json
import logging
import os
import sys
import warnings

# A windowed build has no console, so sys.stdout / sys.stderr are None and the
# first write (any logging.StreamHandler, the SDK's shutdown logging) raises
# AttributeError and kills the process. Point them at a sink before anything
# can attach a logger.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8", buffering=1)
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8", buffering=1)


# Vendored libusb DLL directories must be on the search path before the SDK
# (pyusb / libusb1) is imported. PyInstaller did this in a runtime hook; a
# Nuitka build has none, so it happens here for every frozen build (#548).
from utils.frozen import bundle_dir, is_frozen  # noqa: E402
from utils.libusb_paths import register_vendored_libusb  # noqa: E402

if is_frozen():
    register_vendored_libusb(bundle_dir())

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMessageBox
from PyQt6.QtQml import (
    QQmlApplicationEngine, qmlRegisterSingletonInstance, qmlRegisterSingletonType,
)
from PyQt6.QtCore import qInstallMessageHandler, QtMsgType, QTimer, QUrl

from config.app_config import SERVICE_BUILD
from connector import MotionConnector
from hardware.motion_config import DEFAULT_TRIGGER_OVERRIDES
from omotion import MotionInterface, db_open
from utils.single_instance import check_single_instance, cleanup_single_instance
from version import get_version
from utils.resource_path import resource_path
from utils import app_paths, config_store, settings_store, startup_report
from utils.onefile_cleanup import start_background_sweep


APP_VERSION = get_version()

# The compiled config (config/app_config.py) plus any dev-only launch
# overrides, captured at load so the connector can diff runtime changes
# against it when persisting preferences and the startup report can mark
# what deviates from it. There is no configuration file any more (#546).
_APP_CONFIG_BASELINE: dict = {}

# Keys a source-run launch flag forced this launch (for the startup report).
_DEV_CONFIG_KEYS: set = set()

logger = logging.getLogger("openmotion.bloodflow-app")
logger.setLevel(logging.INFO)

# Suppress PyQt6 DeprecationWarnings related to SIP
warnings.simplefilter("ignore", DeprecationWarning)

_QT_LOG_LEVELS = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}


def qt_message_handler(msg_type, context, message):
    """Forward QML messages to the app log at the matching severity.

    `console.log()` in QML is `QtDebugMsg` and is filtered out by default.
    Use `console.warn()` / `console.error()` from QML for things that
    should always reach the run log.
    """
    logging.getLogger("openmotion.bloodflow-app.qml-console").log(
        _QT_LOG_LEVELS.get(msg_type, logging.INFO), "QML: %s", message)


def _load_app_config(
    *, clinical: bool | None = None, portable: bool | None = None,
    overrides: dict | None = None,
) -> dict:
    """The effective app config before saved preferences are applied.

    The shipped values are compiled in (``config/app_config.py``, #546).
    ``clinical`` / ``portable`` / ``overrides`` are the dev-only launch flags
    from ``_parse_dev_args`` (``None`` = keep the compiled value); a frozen
    build never passes them, and the process environment is never consulted.
    Saved operator preferences are overlaid later in ``main``, once the scan
    database (which holds the settings table) is open.

    Logs nothing about the config: this runs before the log-file handler is
    attached, so the startup report does that afterwards (#527).
    """
    cfg = config_store.compiled_config()
    dev_keys = set()
    if clinical is not None:
        cfg["clinicalMode"] = bool(clinical)
        dev_keys.add("clinicalMode")
    # portableMode is derived, not configured: a frozen build asks app_paths
    # whether the installer registered this exe. --portable only matters for
    # the startup report (a source run writes under the cwd either way).
    if portable is not None:
        cfg["portableMode"] = bool(portable)
        dev_keys.add("portableMode")
    else:
        cfg["portableMode"] = app_paths.portable_mode()
    dev_keys |= config_store.apply_dev_overrides(cfg, overrides)

    # macOS is research-only. This has to win over the compiled value AND the
    # dev flag: clinicalMode drives require_encrypted_db and the SDK refuses
    # the scan-db keystore on macOS, so a "clinical" macOS session could only
    # fail later and less clearly. build_macos.sh has no variant stamp of its
    # own (unlike scripts/build_common.ps1), so this is the only gate.
    if sys.platform == "darwin" and cfg.get("clinicalMode"):
        logger.warning(
            "clinicalMode requested on macOS — forcing Research. macOS builds "
            "are research-only and are not validated for clinical use."
        )
        cfg["clinicalMode"] = False

    _APP_CONFIG_BASELINE.clear()
    _APP_CONFIG_BASELINE.update(config_store.compiled_config())
    _APP_CONFIG_BASELINE.update({k: cfg[k] for k in dev_keys})
    _APP_CONFIG_BASELINE["portableMode"] = cfg["portableMode"]
    _APP_CONFIG_BASELINE["clinicalMode"] = cfg["clinicalMode"]
    _DEV_CONFIG_KEYS.clear()
    _DEV_CONFIG_KEYS.update(dev_keys)
    return cfg


# Qt runtime knobs the host environment could otherwise inject. Each changes
# how the window comes up (platform plugin, style/theme, DPI scaling, render
# backend, logging) with no change to the build, which a validated artifact
# must not allow. They are removed before the first QApplication is
# constructed; the three the app relies on are then pinned.
#
# Deliberately NOT listed: QT_PLUGIN_PATH, QML2_IMPORT_PATH and PATH.
# PyInstaller's PyQt6 runtime hook sets those to the bundle's own Qt tree at
# process start, and the frozen build needs them to find its plugins.
_QT_ENV_SCRUB = (
    "QT_QPA_PLATFORM", "QT_QPA_PLATFORMTHEME", "QT_QPA_PLATFORM_PLUGIN_PATH",
    "QT_QPA_FONTDIR", "QML_IMPORT_PATH", "QT_STYLE_OVERRIDE",
    "QT_QUICK_CONTROLS_CONF", "QT_QUICK_CONTROLS_FALLBACK_STYLE",
    "QT_QUICK_CONTROLS_HOVER_ENABLED", "QT_QUICK_CONTROLS_MATERIAL_VARIANT",
    "QT_QUICK_CONTROLS_MATERIAL_ACCENT", "QT_QUICK_CONTROLS_MATERIAL_PRIMARY",
    "QT_QUICK_CONTROLS_MATERIAL_FOREGROUND", "QT_QUICK_CONTROLS_MATERIAL_BACKGROUND",
    "QT_SCALE_FACTOR", "QT_SCREEN_SCALE_FACTORS", "QT_AUTO_SCREEN_SCALE_FACTOR",
    "QT_ENABLE_HIGHDPI_SCALING", "QT_SCALE_FACTOR_ROUNDING_POLICY",
    "QT_FONT_DPI", "QT_USE_PHYSICAL_DPI",
    "QT_QUICK_BACKEND", "QSG_RHI_BACKEND", "QSG_RENDER_LOOP", "QSG_INFO",
    "QT_OPENGL", "QT_ANGLE_PLATFORM", "QT_D3D_ADAPTER_INDEX",
    "QT_MESSAGE_PATTERN", "QT_DEBUG_PLUGINS", "QT_FATAL_WARNINGS",
    "QML_DISABLE_DISK_CACHE", "QML_FORCE_DISK_CACHE", "QML_DISK_CACHE_PATH",
)

_QT_ENV_PINNED = {
    "QT_QUICK_CONTROLS_STYLE": "Material",
    "QT_QUICK_CONTROLS_MATERIAL_THEME": "Dark",
    "QT_LOGGING_RULES": "qt.qpa.fonts=false",
}


def _pin_qt_environment(environ=None) -> list[str]:
    """Make Qt's startup independent of the inherited environment.

    Drops every key in _QT_ENV_SCRUB and sets the _QT_ENV_PINNED values, so
    the platform plugin, Controls style/theme, DPI scaling and scene-graph
    backend are decided by this file alone. Must run before QApplication is
    created. Returns the names that were actually removed, for the log.
    """
    env = os.environ if environ is None else environ
    removed = [k for k in _QT_ENV_SCRUB if env.pop(k, None) is not None]
    env.update(_QT_ENV_PINNED)
    return removed


def _parse_dev_args(argv, *, frozen=None) -> tuple[dict, list[str]]:
    """Split our dev-only launch flags out of ``argv``.

    Returns ``(dev, qt_argv)``. ``dev`` has ``clinical`` / ``portable``
    (``True`` / ``False`` / ``None`` = not given), ``data_root`` (str or
    ``None``), ``config_override`` (a dict of config keys to force for this
    source run, parsed from a JSON object, or ``None``) and ``ignored`` (the
    flags a frozen build refused, or ``None``).
    ``qt_argv`` is ``argv`` with our flags removed, for QApplication — Qt's
    own ``-platform`` / ``-style`` options pass through untouched (no short
    flags and no abbreviation matching, so ``-platform`` can never be read
    as ``-p latform``).

    These flags let a source checkout launch as either build variant (the
    Zed tasks: ``--portable --clinical`` and ``--portable --research
    --data-root <dir>``); nothing ambient (an env var) can steer a launch.
    ``--config-override '{"engineeringMode": true}'`` (#546) is what the HIL
    harness uses: it can force any compiled key, constants included, on a
    source run. A frozen build ignores all of them: the artifact's variant
    is compiled in by scripts/build_common.ps1 (#233 / #546).
    """
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--clinical", dest="clinical", action="store_true", default=None)
    mode.add_argument("--research", dest="clinical", action="store_false", default=None)
    parser.add_argument("--portable", action="store_true", default=None)
    parser.add_argument("--data-root", dest="data_root", default=None)
    parser.add_argument("--config-override", dest="config_override", default=None)
    argv = list(argv)
    ns, rest = parser.parse_known_args(argv[1:])
    overrides = None
    if ns.config_override is not None:
        try:
            overrides = json.loads(ns.config_override)
        except ValueError as e:
            raise SystemExit(f"--config-override is not valid JSON: {e}")
        if not isinstance(overrides, dict):
            raise SystemExit("--config-override must be a JSON object")
    dev = {"clinical": ns.clinical, "portable": ns.portable, "data_root": ns.data_root,
           "config_override": overrides, "ignored": None}
    given = {k: v for k, v in dev.items() if v is not None}
    if frozen is None:
        frozen = is_frozen()
    if frozen and given:
        dev = dict.fromkeys(dev)
        dev["ignored"] = given
    return dev, argv[:1] + rest


def _app_icon() -> QIcon:
    """The multi-size .ico (the taskbar wants its 16-48px frames), or the
    1024px PNG when the ICO does not load (e.g. a packaged build missing Qt's
    ico image-format plugin), so the window never shows the generic icon."""
    icon = QIcon(str(resource_path("assets", "images", "favicon.ico")))
    if icon.isNull() or not icon.availableSizes():
        icon = QIcon(str(resource_path("assets", "images", "favicon.png")))
    return icon


def _scan_db_is_encrypted(scan_db_path: str) -> bool:
    """Whether a clinical build may open scans.db (#683).

    Clinical builds keep scans.db encrypted at rest. A missing (or still
    empty) file passes: MotionInterface turns the SDK's encryption policy
    on, and the SDK then creates the database encrypted. A plaintext SQLite
    file does not. A Research install or a version before 1.5.0 can leave
    one in the same data folder, and a clinical build never converts it.
    """
    return db_open.classify_file(scan_db_path) != "plaintext"


def _set_app_user_model_id() -> None:
    """Set the Windows AppUserModelID (the taskbar identity).

    DO NOT bump this string again: it mints a new identity and strands every
    taskbar pin users have made since 1.4.0. It was bumped once (to ".1") on
    the theory that Explorer caches an icon per AUMID; the generic-icon bug
    reproduced under the new one, and the real cause was the Win32
    window-class icon (#223, utils/win_taskbar_icon.py).
    Keep in sync with the ShortcutProperty in installer/app.wxs.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Openwater.OpenMotion.1")
    except Exception:
        pass  # cosmetic only


def _show_already_running(qt_argv) -> None:
    """Tell the user another instance is running (needs its own QApplication)."""
    app = QApplication(qt_argv)
    app.setWindowIcon(_app_icon())
    msg_box = QMessageBox()
    msg_box.setIcon(QMessageBox.Icon.Warning)
    msg_box.setWindowTitle("Open-Motion")
    msg_box.setText("Another instance of the application is already running.")
    msg_box.setInformativeText("Please close the existing instance before opening a new one.")
    msg_box.setStandardButtons(QMessageBox.StandardButton.Ok)
    msg_box.exec()


def _log_handler(handler, formatter) -> logging.Handler:
    """``handler`` at INFO with the app's line format."""
    handler.setLevel(logging.INFO)
    handler.setFormatter(formatter)
    return handler


def _app_display_name(app_config) -> str:
    """The taskbar name by build variant: clinicalMode=false is the Research
    distribution, and the clinical service tool (#706) names itself so it
    can't pass for a patient build."""
    if not app_config.get("clinicalMode", False):
        return "Open-Motion Research"
    return "Open-Motion Service" if SERVICE_BUILD else "Open-Motion"


def main():
    # Before any QApplication (and thus any HWND) exists, including the
    # single-instance message box: Windows binds the taskbar button to the
    # process identity when the first window appears.
    _set_app_user_model_id()

    # Startup must not depend on the inherited environment. Strip the Qt
    # knobs a host machine could inject and pin ours (before the first
    # QApplication), and take the dev-only launch flags from argv — never
    # from env vars.
    scrubbed_qt_env = _pin_qt_environment()
    dev, qt_argv = _parse_dev_args(sys.argv)
    if dev["data_root"]:
        app_paths.set_data_root_override(dev["data_root"])

    if not check_single_instance():
        _show_already_running(qt_argv)
        sys.exit(1)

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    console_handler = _log_handler(logging.StreamHandler(), formatter)
    logger.addHandler(console_handler)

    app_config = _load_app_config(
        clinical=dev["clinical"], portable=dev["portable"], overrides=dev["config_override"])
    # Single output root: dataDirectory, or app_paths.writable_root() (which
    # applies the frozen/portable/dev-cwd precedence and the ~/Documents
    # fallback for an unwritable candidate). Two fixed children live under
    # it: logs/ (this run's log file) and data/ (scans.db, scan CSVs,
    # calibrations, debug-bundles, downloaded updates).
    _data_dir = app_config.get("dataDirectory") or str(
        app_paths.writable_root(app_config.get("portableMode")))
    os.makedirs(_data_dir, exist_ok=True)
    run_dir = os.path.join(_data_dir, app_paths.LOGS_DIRNAME)
    os.makedirs(run_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    logfile_path = os.path.join(run_dir, f"open-motion-{ts}.log")

    file_handler = _log_handler(
        logging.FileHandler(logfile_path, mode="w", encoding="utf-8"), formatter)
    logger.addHandler(file_handler)
    logger.info("=" * 64)
    logger.info("Open-Motion %s starting", APP_VERSION)
    logger.info("Log file:       %s", logfile_path)
    logger.info("Data directory: %s", _data_dir)
    logger.info("=" * 64)

    # Logged here rather than at the top of main() so it lands in the file
    # log, not just the (often hidden) console.
    if scrubbed_qt_env:
        logger.info("Ignored Qt environment overrides from the host: %s", scrubbed_qt_env)
    if dev["ignored"]:
        logger.warning(
            "Ignoring dev-only launch flags %s: a packaged build's variant and "
            "data root are fixed at build time.", dev["ignored"],
        )
    elif any(v is not None for v in dev.values()):
        logger.info(
            "Dev launch overrides: clinical=%s portable=%s data_root=%s config_override=%s",
            dev["clinical"], dev["portable"], dev["data_root"], dev["config_override"],
        )

    # The SDK logger hierarchy writes through the same handlers, not the root.
    sdk_logger = logging.getLogger("openmotion.sdk")
    sdk_logger.setLevel(logging.INFO)
    sdk_logger.addHandler(console_handler)
    sdk_logger.addHandler(file_handler)
    sdk_logger.propagate = False

    # <_data_dir>/data holds the SDK's default CsvSink / ScanDBSink output
    # and the connector's calibrations / debug-bundles (self._data_root).
    _scan_data_dir = os.path.join(_data_dir, app_paths.DATA_DIRNAME)
    os.makedirs(_scan_data_dir, exist_ok=True)
    _scan_db_path = os.path.join(_scan_data_dir, "scans.db")
    # Clinical builds encrypt scans.db at rest (SQLCipher, key in the Windows
    # Credential Manager). The flag is the SIGNED build config, so the encrypt
    # decision cannot be independently forgotten. Constructing MotionInterface
    # sets the SDK's process-wide policy exactly once. A clinical build checks
    # scans.db first: a plaintext one left in the data folder is reported as
    # E-108 once the window is up (#683), and the SDK refuses to open it.
    _clinical = bool(app_config.get("clinicalMode", False))
    _plaintext_scan_db = _clinical and not _scan_db_is_encrypted(_scan_db_path)
    motion_interface = MotionInterface(
        data_dir=_scan_data_dir,
        scan_db_path=_scan_db_path,
        operator_id="bloodflow-app",
        require_encrypted_db=_clinical,
        # Dark-frame skip displacement (#449): pinned at the interface level
        # so EVERY resolved trigger config carries it — including the SDK's
        # own re-send right before start_trigger, which reverts anything
        # patched in after resolution. See motion_config.py for the numbers.
        default_trigger_config=DEFAULT_TRIGGER_OVERRIDES,
    )

    # Operator preferences (#546): the PREFERENCE / STATE tiers of the
    # compiled config live in the settings table of scans.db (encrypted and
    # HMAC-protected on a clinical build). Opened here because it needs the
    # encryption policy set above.
    settings = settings_store.SettingsStore(_scan_db_path)
    saved_keys = config_store.apply_saved_preferences(app_config, settings.load())

    # Startup diagnostics (#527), after the file handler is attached.
    startup_report.log_startup_report(
        logger, app_config, _APP_CONFIG_BASELINE,
        dev_keys=set(_DEV_CONFIG_KEYS), saved_keys=saved_keys,
        settings_path=settings.path, settings_enabled=settings.enabled,
    )
    motion_interface.log_system_info()

    # Delete the %TEMP% extraction folders earlier launches orphaned when
    # their onefile launcher was killed (#727). Only after the
    # single-instance check: no other instance may be running from one.
    start_background_sweep()

    qInstallMessageHandler(qt_message_handler)

    app = QApplication(qt_argv)
    app.setWindowIcon(_app_icon())
    app.setApplicationName(_app_display_name(app_config))
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("Openwater")

    engine = QQmlApplicationEngine()

    connector = MotionConnector(
        motion_interface, app_config=app_config, data_dir=_data_dir,
        baseline_config=_APP_CONFIG_BASELINE, settings_store=settings,
        app_version=APP_VERSION, log_path=logfile_path,
    )
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", connector)
    # AppTheme as a true QML singleton: one QObject with ~40 color bindings
    # instead of one instance per file. Registered into the OpenMotion module
    # QML already imports; instantiated lazily on first use.
    qmlRegisterSingletonType(
        QUrl.fromLocalFile(str(resource_path("components", "AppTheme.qml"))),
        "OpenMotion", 1, 0, "AppTheme",
    )
    engine.rootContext().setContextProperty("appVersion", APP_VERSION)

    engine.load(str(resource_path("main.qml")))
    if not engine.rootObjects():
        logger.error("Error: Failed to load QML file")
        sys.exit(-1)

    # Pin the Win32 window-class icon (#223): the shell falls back to it when
    # the GUI thread is too busy to answer WM_GETICON, and Qt leaves it at the
    # generic IDI_APPLICATION. See utils/win_taskbar_icon.py.
    if sys.platform == "win32":
        from utils.win_taskbar_icon import apply_window_class_icon

        apply_window_class_icon(
            int(engine.rootObjects()[0].winId()),
            resource_path("assets", "images", "favicon.ico"),
        )

    # Low-storage check (#506): E-107 critical modal when the data drive is
    # nearly full. Deferred to the first event-loop turn so the QML modal is
    # listening when it fires.
    QTimer.singleShot(0, connector.check_startup_storage)
    if _plaintext_scan_db:
        QTimer.singleShot(0, lambda: connector.raise_startup_error(
            "E-108", detail=f"{_scan_db_path} is a plaintext database."))

    # wait=False: the QML window is already visible and the event loop has not
    # started, so a blocking wait here (a console handshake takes ~5 s) starves
    # Explorer's taskbar icon negotiation for the new window (#223).
    # Already-attached devices reach the UI through the same
    # _on_handle_state_changed signal path as any hotplug.
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
        settings.close()
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
