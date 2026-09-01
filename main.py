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
from motion_config import DEFAULT_TRIGGER_OVERRIDES
from omotion import MotionInterface, factory_calibration_thresholds
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


def _ft_factory_defaults() -> dict:
    """ft_* code defaults = the SDK's factory acceptance thresholds.

    These were zeros once: a zero min-mean/min-contrast can never fail
    (both quantities are non-negative), which silently disabled the SDK's
    calibration pre-write gate whenever a config lacked the ft_* keys — a
    below-spec calibration then wrote the console EEPROM and displayed
    PASSED (#473). Config files still override every key; only the
    fallback changed.
    """
    f = factory_calibration_thresholds()
    return {
        "ft_min_mean_per_camera": f.min_mean_per_camera,
        "ft_min_contrast_per_camera": f.min_contrast_per_camera,
        "ft_min_bfi_per_camera": f.min_bfi_per_camera,
        "ft_max_bfi_per_camera": f.max_bfi_per_camera,
        "ft_min_bvi_per_camera": f.min_bvi_per_camera,
        "ft_max_bvi_per_camera": f.max_bvi_per_camera,
        "ft_max_dark_per_camera": f.max_dark_per_camera,
    }


def _load_app_config(
    *, clinical: bool | None = None, portable: bool | None = None
) -> dict:
    """Load application config from config/app_config.json. Returns defaults if missing or invalid.

    ``clinical`` / ``portable`` are the dev-only launch overrides parsed from
    the command line by ``_parse_dev_args`` (``None`` = keep the shipped
    value). The process environment is never consulted here.
    """
    defaults = {
        "forceLaserFail": False,
        # QA/bench lever: sets DEBUG_FLAG_HISTO_STALL on both sensors so a
        # scan deterministically loses all camera data ~45 s in while USB
        # stays alive (sensor-fw#75) — the #248/#174 repro. Default off.
        # Live toggle in Settings → Engineering (#525).
        "debugHistoStallTest": False,
        # In-app updater source overrides (default None => production GitHub
        # repo). updateRepo swaps the owner/repo; updateApiUrl fully overrides
        # the releases-latest endpoint (used by the local update-test server).
        "updateRepo": None,
        "updateApiUrl": None,
        # Beta/prerelease update channel for BOTH updaters (app self-update
        # + device firmware). Effective only in a Research build with
        # engineering mode unlocked — see MotionConnector._beta_enabled().
        # Must be registered here or config_store drops it (silently
        # non-persistent). Renamed from downloadBetaFirmware (#386).
        "downloadBetaUpdates": False,
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
        **_ft_factory_defaults(),
        "max_calibration_time_sec": 600,
        "calibration_scan_duration_sec": 15,
        "test_scan_duration_sec": 5,
        "calibration_scan_delay_sec": 1,
        "leftMask": 0x66,   # 0b01100110 — cameras 2,3,6,7 (Middle pattern)
        "rightMask": 0x66,
        "uncorrectedOnly": False,
        "engineeringMode": False,
        # Alternative camera settings (#446, Settings → Engineering →
        # Camera settings). When enabled, exposure + per-camera analog gain
        # are written via the SDK to every scanned camera just before each
        # scan starts. Defaults mirror what the sensor firmware itself
        # programs (X02C1B_Sensor_Config.h: 72 rows × 9 µs = 648 µs;
        # X02C1B_configure_sensor per-position gain ladder). Valid exposures
        # are whole 9 µs rows in 99–2196 µs; valid gains 1/2/4/8/16.
        "altCameraSettingsEnabled": False,
        "altCameraExposureUs": 648,
        "altCameraGains": [16, 4, 2, 1, 1, 2, 4, 16],
        # Internal (no UI): true while camera registers may hold alternative
        # values, so the first scan after disabling restores fw defaults.
        "altCameraSettingsDirty": False,
        # Alternative laser pulse width (#449) — experiments only, separate
        # enable from the camera settings above. Overrides the trigger
        # config's LaserPulseWidthUsec (SDK default 500 µs) on every push
        # while enabled; valid 100–2200 whole µs (stock safety interlock
        # latches above ~1000 µs — the operator disables it first).
        # Ignored on a plain clinical build.
        "altLaserPulseWidthEnabled": False,
        "altLaserPulseWidthUsec": 500,
        # Internal (no UI): true while the TA driver's pulse_width register
        # may hold an alternative value, so the first scan after disabling
        # restores the laser_params.json baseline.
        "altLaserPulseWidthDirty": False,
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
        # Per-scan telemetry CSV — engineering-only (#43) AND opt-in via the
        # Settings → Engineering switch (#471). Fail closed by default.
        "writeTelemetryCsv": False,
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
        # Liquid Glass theme — translucent frosted surfaces over an
        # animated ambient backdrop (Settings → Appearance → Theme).
        # Orthogonal to darkMode; both light and dark have a glass
        # variant, and "Liquid Glass" in the Theme selector is the
        # dark-based one. Default ON for macOS (its native Tahoe look),
        # OFF elsewhere so Windows clinical builds keep the solid palette.
        "liquidGlass": sys.platform == "darwin",
        "cq_check_duration_sec": 1.0,
        "cq_rolling_avg_window": 5,
        # Live contact-quality monitor debounce (issue #364), asymmetric:
        # RAISE the warning fast (a late warning is a safety miss) but CLEAR
        # it slowly (a premature dismiss strands the operator on a still-bad
        # camera). Counts consecutive light-frame evaluations at ~40 Hz, so
        # 10 ~= 0.25 s to pop up, 80 ~= 2 s to dismiss. The dark/ambient path
        # is not debounced — darks are ~15 s apart, their own debounce.
        "cq_live_activate_frames": 10,
        "cq_live_clear_frames": 80,
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
        # Startup connection watchdog (E-104/E-106). Also gates the
        # research-build sample-dataset offer, so this is deliberately
        # short — the user should not stare at an empty scan page.
        "connectionTimeoutSec": 12,
        "requireConsole": True,
        "minSensors": 1,
    }
    baseline, merged = config_store.load_app_config(defaults)

    # Dev-only launch overrides from the command line (see _parse_dev_args —
    # e.g. the Zed tasks for Clinical/Research x always-portable). main()
    # only passes them for a source run: a frozen build drops the flags, and
    # the process environment is never read, so a packaged artifact boots
    # identically on every machine. The build-time flip in build_common.ps1
    # remains the source of truth for shipped artifacts.
    if portable is not None:
        baseline["portableMode"] = bool(portable)
        merged["portableMode"] = bool(portable)
    if clinical is not None:
        baseline["clinicalMode"] = bool(clinical)
        merged["clinicalMode"] = bool(clinical)

    # macOS is a research-only platform: it is never validated or shipped for
    # clinical use. This has to win over the bundled config AND the env
    # override, because clinicalMode drives require_encrypted_db (see the
    # MotionInterface construction below), and the SDK refuses the scan-db
    # keystore on macOS outright — so a "clinical" macOS session cannot start
    # at all, it can only fail later and less clearly. Forcing it here is what
    # makes the DMG a coherent research build rather than a broken clinical
    # one. build_macos.sh bundles config/ wholesale and has no variant flip of
    # its own (unlike scripts/build_common.ps1), so this is the only gate.
    if sys.platform == "darwin" and (baseline.get("clinicalMode") or merged.get("clinicalMode")):
        logger.warning(
            "clinicalMode requested on macOS — forcing Research. macOS builds "
            "are research-only and are not validated for clinical use."
        )
        baseline["clinicalMode"] = False
        merged["clinicalMode"] = False

    _APP_CONFIG_BASELINE.clear()
    _APP_CONFIG_BASELINE.update(baseline)
    logger.info(
        "Loaded app config (overrides from %s)",
        app_paths.local_config_path(bool(baseline.get("portableMode", False))),
    )
    return merged


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
    ``None``) and ``ignored`` (the flags a frozen build refused, or ``None``).
    ``qt_argv`` is ``argv`` with our flags removed, for QApplication — Qt's
    own ``-platform`` / ``-style`` options pass through untouched (no short
    flags and no abbreviation matching, so ``-platform`` can never be read
    as ``-p latform``).

    These flags let a source checkout launch as either build variant (the
    Zed tasks: ``--portable --clinical`` and ``--portable --research
    --data-root <dir>``). They replaced the OPENMOTION_CLINICAL /
    OPENMOTION_PORTABLE / OPENWATER_DATA_ROOT env vars so nothing ambient
    can steer a launch. A frozen build ignores them entirely: the artifact's
    variant and data root are baked in by scripts/build_common.ps1 (#233).
    """
    import argparse

    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--clinical", dest="clinical", action="store_true", default=None)
    mode.add_argument("--research", dest="clinical", action="store_false", default=None)
    parser.add_argument("--portable", action="store_true", default=None)
    parser.add_argument("--data-root", dest="data_root", default=None)
    argv = list(argv)
    ns, rest = parser.parse_known_args(argv[1:])
    dev = {
        "clinical": ns.clinical,
        "portable": ns.portable,
        "data_root": ns.data_root,
        "ignored": None,
    }
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    given = {k: v for k, v in dev.items() if k != "ignored" and v is not None}
    if frozen and given:
        dev = {"clinical": None, "portable": None, "data_root": None, "ignored": given}
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
    app_config = _load_app_config(clinical=dev["clinical"], portable=dev["portable"])
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
    elif any(dev[k] is not None for k in ("clinical", "portable", "data_root")):
        logger.info(
            "Dev launch overrides: clinical=%s portable=%s data_root=%s",
            dev["clinical"], dev["portable"], dev["data_root"],
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
