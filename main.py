import sys
import os
import warnings
import logging
import datetime
import json


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
    QQmlApplicationEngine,
    qmlRegisterSingletonInstance,
    qmlRegisterSingletonType,
)
from PyQt6.QtCore import qInstallMessageHandler, QtMsgType, QUrl

from motion_connector import MotionConnector
from motion_config import DEFAULT_TRIGGER_OVERRIDES
from omotion import MotionInterface
from utils.single_instance import check_single_instance, cleanup_single_instance
from version import get_version
from utils.resource_path import resource_path
from utils import app_paths, config_store, settings_store, startup_report


APP_VERSION = get_version()

# The compiled config (config/app_config.py) plus any dev-only launch
# overrides, captured at load so the connector can diff runtime changes
# against it when persisting preferences and the startup report can mark
# what deviates from it. There is no configuration file any more (#546).
_APP_CONFIG_BASELINE: dict = {}

# Keys a source-run launch flag forced this launch (for the startup report).
_DEV_CONFIG_KEYS: set = set()


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


def _load_app_config(
    *, clinical: bool | None = None, portable: bool | None = None,
    overrides: dict | None = None,
) -> dict:
    """The effective app config before saved preferences are applied.

    Since #546 the shipped values are compiled in (``config/app_config.py``)
    and there is no configuration file to read. ``clinical`` / ``portable``
    / ``overrides`` are the dev-only launch flags parsed from the command
    line by ``_parse_dev_args`` (``None`` = keep the compiled value); a
    frozen build never passes them, and the process environment is never
    consulted. Saved operator preferences are overlaid later in ``main``,
    once the scan database (which holds the settings table) is available.
    """
    cfg = config_store.compiled_config()
    dev_keys = set()

    # Dev-only launch overrides. main() only passes them for a source run:
    # a frozen build drops the flags, so a packaged artifact boots
    # identically on every machine. The build-time stamp of CLINICAL_MODE
    # in config/app_config.py is the source of truth for shipped artifacts.
    if clinical is not None:
        cfg["clinicalMode"] = bool(clinical)
        dev_keys.add("clinicalMode")
    # portableMode is derived, not configured: a frozen build asks
    # app_paths whether the installer registered this exe. --portable only
    # matters for the startup report on a source run (the writable root of
    # a source run is the cwd either way).
    if portable is not None:
        cfg["portableMode"] = bool(portable)
        dev_keys.add("portableMode")
    else:
        cfg["portableMode"] = app_paths.portable_mode()
    dev_keys |= config_store.apply_dev_overrides(cfg, overrides)

    # macOS is a research-only platform: it is never validated or shipped for
    # clinical use. This has to win over the compiled value AND the dev flag,
    # because clinicalMode drives require_encrypted_db (see the
    # MotionInterface construction below), and the SDK refuses the scan-db
    # keystore on macOS outright — so a "clinical" macOS session cannot start
    # at all, it can only fail later and less clearly. Forcing it here is what
    # makes the DMG a coherent research build rather than a broken clinical
    # one. build_macos.sh has no variant stamp of its own (unlike
    # scripts/build_common.ps1), so this is the only gate.
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
    # No config logging here: this runs before the log-file handler is
    # attached (the log's location depends on the config), so anything
    # logged here reaches only the console. The startup report logs the
    # effective config after the handler exists (#527).
    return cfg

# Qt runtime knobs the host environment could otherwise inject. Every one of
# these changes how the window comes up (platform plugin, style/theme, DPI
# scaling, render backend, logging) with no change to the build — exactly
# what a validated artifact must not allow. They are removed before the first
# QApplication is constructed; the three the app relies on are then pinned.
#
# Deliberately NOT listed: QT_PLUGIN_PATH, QML2_IMPORT_PATH and PATH.
# PyInstaller's PyQt6 runtime hook sets those to the bundle's own Qt tree at
# process start, and the frozen build needs them to find its plugins.
_QT_ENV_SCRUB = (
    "QT_QPA_PLATFORM",
    "QT_QPA_PLATFORMTHEME",
    "QT_QPA_PLATFORM_PLUGIN_PATH",
    "QT_QPA_FONTDIR",
    "QML_IMPORT_PATH",
    "QT_STYLE_OVERRIDE",
    "QT_QUICK_CONTROLS_CONF",
    "QT_QUICK_CONTROLS_FALLBACK_STYLE",
    "QT_QUICK_CONTROLS_HOVER_ENABLED",
    "QT_QUICK_CONTROLS_MATERIAL_VARIANT",
    "QT_QUICK_CONTROLS_MATERIAL_ACCENT",
    "QT_QUICK_CONTROLS_MATERIAL_PRIMARY",
    "QT_QUICK_CONTROLS_MATERIAL_FOREGROUND",
    "QT_QUICK_CONTROLS_MATERIAL_BACKGROUND",
    "QT_SCALE_FACTOR",
    "QT_SCREEN_SCALE_FACTORS",
    "QT_AUTO_SCREEN_SCALE_FACTOR",
    "QT_ENABLE_HIGHDPI_SCALING",
    "QT_SCALE_FACTOR_ROUNDING_POLICY",
    "QT_FONT_DPI",
    "QT_USE_PHYSICAL_DPI",
    "QT_QUICK_BACKEND",
    "QSG_RHI_BACKEND",
    "QSG_RENDER_LOOP",
    "QSG_INFO",
    "QT_OPENGL",
    "QT_ANGLE_PLATFORM",
    "QT_D3D_ADAPTER_INDEX",
    "QT_MESSAGE_PATTERN",
    "QT_DEBUG_PLUGINS",
    "QT_FATAL_WARNINGS",
    "QML_DISABLE_DISK_CACHE",
    "QML_FORCE_DISK_CACHE",
    "QML_DISK_CACHE_PATH",
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
    --data-root <dir>``). They replaced the OPENMOTION_CLINICAL /
    OPENMOTION_PORTABLE / OPENWATER_DATA_ROOT env vars so nothing ambient
    can steer a launch. ``--config-override '{"engineeringMode": true}'``
    (#546) is what the HIL harness uses instead of editing a config file:
    it can force any compiled key, constants included, on a source run.
    A frozen build ignores all of them entirely: the artifact's variant is
    compiled in by scripts/build_common.ps1 (#233 / #546).
    """
    import argparse

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
    dev = {
        "clinical": ns.clinical,
        "portable": ns.portable,
        "data_root": ns.data_root,
        "config_override": overrides,
        "ignored": None,
    }
    if frozen is None:
        frozen = is_frozen()
    given = {k: v for k, v in dev.items() if k != "ignored" and v is not None}
    if frozen and given:
        dev = {
            "clinical": None, "portable": None, "data_root": None,
            "config_override": None, "ignored": given,
        }
    return dev, argv[:1] + rest


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
    # Set the Windows AppUserModelID before any QApplication (and thus any
    # HWND) exists: Windows binds the taskbar button to the process identity
    # when the first window appears, so this has to be settled first. It must
    # run before check_single_instance()'s message box too.
    #
    # DO NOT bump this string again. It was bumped once ("Openwater.OpenMotion"
    # -> ".1") on the theory that Explorer caches an icon per AUMID and that a
    # fresh ID would clear a poisoned entry. That theory did not hold up: the
    # generic-icon bug reproduced under a brand-new AUMID and a clean relaunch
    # under the *same* AUMID showed the correct icon, so the AUMID was never
    # what was broken. The real cause was the Win32 window-class icon (see the
    # #223 note further down, and utils/win_taskbar_icon.py). Changing the
    # AUMID only mints a new identity and strands every taskbar pin users have
    # made since 1.4.0 — a one-way cost with no benefit.
    # Keep in sync with the ShortcutProperty in installer/app.wxs.
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Openwater.OpenMotion.1"
            )
        except Exception:
            pass  # Ignore if not available

    # Startup must not depend on the inherited environment. Strip the Qt
    # knobs a host machine could inject and pin ours (before the first
    # QApplication, including the single-instance message box below), and
    # take the dev-only launch flags from argv — never from env vars.
    scrubbed_qt_env = _pin_qt_environment()
    dev, qt_argv = _parse_dev_args(sys.argv)
    if dev["data_root"]:
        app_paths.set_data_root_override(dev["data_root"])

    # Check if another instance is already running
    if not check_single_instance():
        # Create a minimal QApplication to show message box
        app = QApplication(qt_argv)
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

    # Qt style/theme/logging are pinned by _pin_qt_environment() at the top of
    # main(), before the first QApplication.

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
    app_config = _load_app_config(
        clinical=dev["clinical"], portable=dev["portable"],
        overrides=dev["config_override"],
    )
    # Single output root: dataDirectory, or app_paths.writable_root() (which
    # already applies the frozen/portable/dev-cwd precedence + the
    # ~/Documents fallback for an unwritable candidate — e.g. macOS Finder
    # launch where cwd is "/"). Two fixed children live under it: logs/
    # (this run's log file) and data/ (scans.db, scan CSVs, calibrations,
    # debug-bundles, downloaded updates).
    _data_dir = app_config.get("dataDirectory") or str(
        app_paths.writable_root(app_config.get("portableMode"))
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

    # Startup-environment report. Logged here rather than at the top of
    # main() so it lands in the file log, not just the (often hidden) console.
    if scrubbed_qt_env:
        logger.info(
            "Ignored Qt environment overrides from the host: %s", scrubbed_qt_env
        )
    if dev["ignored"]:
        logger.warning(
            "Ignoring dev-only launch flags %s: a packaged build's variant and "
            "data root are fixed at build time.", dev["ignored"],
        )
    elif any(dev[k] is not None for k in
             ("clinical", "portable", "data_root", "config_override")):
        logger.info(
            "Dev launch overrides: clinical=%s portable=%s data_root=%s "
            "config_override=%s",
            dev["clinical"], dev["portable"], dev["data_root"],
            dev["config_override"],
        )

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
    # Clinical builds encrypt scans.db at rest (SQLCipher, key in the Windows
    # Credential Manager). The flag is the SIGNED build config, so the encrypt
    # decision cannot be independently forgotten. Constructing MotionInterface
    # sets the SDK's process-wide policy exactly once.
    _clinical = bool(app_config.get("clinicalMode", False))
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

    # An existing PLAINTEXT scans.db must be encrypted before anything opens it:
    # under the policy the SDK refuses to open plaintext (it never silently
    # appends PHI in the clear), and AuditLog opens the same file inside
    # MotionConnector below. So this has to happen here — after the policy is
    # set, before the connector exists.
    if _clinical:
        from omotion import db_migrate

        try:
            if db_migrate.migrate_plaintext_to_encrypted(_scan_db_path):
                logger.warning(
                    "scans.db was plaintext and has been encrypted in place. A "
                    "backup of the original remains at %s.pre-encryption.bak — "
                    "remove it per SOP once this build is confirmed.",
                    _scan_db_path,
                )
        except Exception:
            # Fail loudly but let the app start: the SDK's own pre-flight will
            # refuse the scan before the laser fires, which is a far clearer
            # failure than a dead splash screen.
            logger.exception(
                "scans.db encryption migration FAILED — scanning will be "
                "refused until this is resolved. The original database is "
                "untouched."
            )

    # Operator preferences (#546): the PREFERENCE / STATE tiers of the
    # compiled config live in the settings table of scans.db — encrypted and
    # HMAC-protected on a clinical build — instead of a plaintext overrides
    # file. Opened here because it needs the encryption policy set (above)
    # and the plaintext migration done. A pre-#546 app_config.local.json,
    # if one is still on the machine, is ignored: nothing reads it.
    settings = settings_store.SettingsStore(_scan_db_path)
    saved_keys = config_store.apply_saved_preferences(app_config, settings.load())

    # Startup diagnostics (issue #527): build variant, install mode, where
    # preferences persist, and the effective config with every key that is
    # not at its compiled value marked. AFTER the file handler attaches —
    # everything logged inside _load_app_config reaches only the console.
    startup_report.log_startup_report(
        logger, app_config, _APP_CONFIG_BASELINE,
        dev_keys=set(_DEV_CONFIG_KEYS), saved_keys=saved_keys,
        settings_path=settings.path, settings_enabled=settings.enabled,
    )

    motion_interface.log_system_info()

    qInstallMessageHandler(qt_message_handler)

    app = QApplication(qt_argv)

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
        baseline_config=_APP_CONFIG_BASELINE, settings_store=settings,
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

    # Pin the Win32 window-class icon (issue #223). Qt answers Explorer's
    # WM_GETICON probe with the icon set above, but the shell asks with
    # SMTO_ABORTIFHUNG and falls back to the *class* icon when the GUI thread
    # is busy — and Qt leaves that at the generic IDI_APPLICATION, because its
    # LoadImage(hInst, L"IDI_ICON1", ...) lookup finds nothing in a PyInstaller
    # build. Frozen builds get the resource from openwater.spec's build hook;
    # this makes the fallback correct at runtime too, including from source.
    if sys.platform == "win32":
        from utils.win_taskbar_icon import apply_window_class_icon

        apply_window_class_icon(
            int(engine.rootObjects()[0].winId()),
            resource_path("assets", "images", "favicon.ico"),
        )

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
