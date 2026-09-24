"""Shipped application configuration, compiled into the executable (#546).

This module replaced ``config/app_config.json``. The keys, values and order
are the ones the JSON shipped with; the difference is that they are Python
literals now, so they compile into the binary with the rest of the code and
no editable configuration file ships next to the executable.

Every key belongs to exactly one tier, declared in the sets at the bottom:

* CONSTANT   compiled in, never overridable at runtime. A ``setConfig`` on
             one of these is refused and recorded in the audit log. The only
             way to run with a different value is a source run with the
             ``--config-override`` dev flag (dropped by a frozen build).
* SESSION    changeable in-process while the engineering interface allows
             it, reset to the compiled value at every launch, never written
             anywhere. The engineering unlock is per session, so nothing it
             enables may outlive the session that authorized it.
* PREFERENCE operator preferences that survive a relaunch. Persisted in the
             ``settings`` table of ``scans.db`` (SQLCipher-encrypted and
             HMAC-protected in clinical builds), never in a plaintext file.
* STATE      internal persisted bookkeeping with no UI (the alt-settings
             restore flags). Same storage as PREFERENCE.

``CLINICAL_MODE`` is stamped by the build (scripts/build_common.ps1,
``Set-BuildVariant``) before PyInstaller runs, one build per variant; the
repo value is the Research default. ``portableMode`` is not a stamp: a
frozen build derives it at launch from the installer's HKLM ``InstallDir``
marker (utils/app_paths.py), so the exe inside the portable zip and the
one inside the installer are byte-identical (#501's repack step depends on
that).
"""

# ── Build variant ─────────────────────────────────────────────────────────
# Stamped per artifact by scripts/build_common.ps1 (regex on this exact
# line). Source runs may override it with --clinical / --research.
CLINICAL_MODE = False

# ── Tier names ─────────────────────────────────────────────────────────────
CONSTANT = "constant"
SESSION = "session"
PREFERENCE = "preference"
STATE = "state"

# ── Values (same keys, same order as the former app_config.json) ──────────
APP_CONFIG = {
    # Derived at launch for frozen builds (see module docstring); a source
    # run may force it with --portable. Kept as a key so the startup report
    # and the updater can read it like any other value.
    "portableMode": None,
    "dataDirectory": None,
    "writeRawCsv": False,
    "rawCsvDurationSec": None,
    # Research-only (#598): export the History → Export CSV file into data/
    # automatically when each scan ends. Never exports on a clinical build.
    "autoExportCsv": False,
    "writeTelemetryCsv": False,
    "leftMask": 102,
    "rightMask": 102,
    "clinicalMode": CLINICAL_MODE,
    "clinicalModeLeftMask": 195,
    "clinicalModeRightMask": 195,
    "showBfiBvi": True,
    "bfiMin": 0.0,
    "bfiMax": 10.0,
    "bviMin": 0.0,
    "bviMax": 10.0,
    "meanMin": 0.0,
    "meanMax": 200.0,
    "contrastMin": 0.0,
    "contrastMax": 0.7,
    "bfiClampLow": 0.0,
    "bfiClampHigh": 10.0,
    "bviClampLow": 0.0,
    "bviClampHigh": 10.0,
    "bfiColor": "#ffffff",
    "bviColor": "#3437db",
    # BVI display low-pass (#228, #552): the cutoff is a compiled constant;
    # the research-only Settings switch gates it via bviLowPassEnabled.
    "bviLowPassEnabled": True,
    "bviLowPassCutoffHz": 20.0,
    "plotWindowSec": 5,
    "autoScale": False,
    "autoScalePerPlot": False,
    "showAxisLabels": True,
    "darkMode": True,
    "max_calibration_time_sec": 600,
    "calibration_scan_duration_sec": 15,
    "test_scan_duration_sec": 5,
    "calibration_scan_delay_sec": 1,
    # Calibration pass thresholds — must agree with the SDK's factory
    # acceptance values (omotion.factory_calibration_thresholds); a zero
    # here can never fail and silently disables the pre-write gate (#473).
    "ft_min_mean_per_camera": [40, 80, 80, 80, 80, 80, 80, 40],
    "ft_min_contrast_per_camera": [0.25, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25],
    "ft_min_bfi_per_camera": [-0.5, -0.5, -0.5, -0.5, -0.5, -0.5, -0.5, -0.5],
    "ft_max_bfi_per_camera": [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5],
    "ft_min_bvi_per_camera": [4.5, 4.5, 4.5, 4.5, 4.5, 4.5, 4.5, 4.5],
    "ft_max_bvi_per_camera": [5.5, 5.5, 5.5, 5.5, 5.5, 5.5, 5.5, 5.5],
    "ft_max_dark_per_camera": [3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0],
    "cq_check_duration_sec": 1.0,
    "cq_rolling_avg_window": 5,
    # Live contact-quality debounce (#364), asymmetric: raise fast, clear
    # slow. Consecutive light-frame evaluations at ~40 Hz.
    "cq_live_activate_frames": 10,
    "cq_live_clear_frames": 80,
    "cq_dark_threshold_per_camera": [3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0],
    "cq_light_threshold_per_camera": [15.0, 15.0, 15.0, 15.0, 15.0, 15.0, 15.0, 15.0],
    "engineeringMode": False,
    # Alternative camera settings (#446): exposure in whole 9 us rows
    # (99-2196 us) and 8 per-position analog gains (1/2/4/8/16), written to
    # every scanned camera before each scan while enabled. Defaults mirror
    # the sensor firmware (72 rows = 648 us; 16/4/2/1/1/2/4/16).
    "altCameraSettingsEnabled": False,
    "altCameraExposureUs": 648,
    "altCameraGains": [16, 4, 2, 1, 1, 2, 4, 16],
    # Alternative laser pulse width (#449), experiments only; whole us,
    # 20-2200. Writes the TA driver's pulse_width register at scan start.
    "altLaserPulseWidthEnabled": False,
    "altLaserPulseWidthUsec": 500,
    # Beta/prerelease channel for both updaters; effective only in a
    # Research build with engineering mode unlocked.
    "downloadBetaUpdates": False,
    "forceLaserFail": False,
    "cameraTempAlertThresholdC": 110,
    # Whole-scan data-stall watchdog (#248): abort with E-303 when no camera
    # delivers a frame for this long while the trigger is on; <= 0 disables.
    "scanDataStallTimeoutSec": 3,
    # Console over-temp trip (C) pushed to the console user config on
    # connect; validated 1-60 before any write (motion_config.ensure_tec_trip).
    "tecTripTempC": 40,
    "powerOffUnusedCameras": True,
    "sensorDebugLogging": False,
    "consoleDebugLogging": False,
    "histoThrottle": False,
    "histoCmp": True,
    "deferHistoSend": True,
    "commVerbose": False,
    "verboseCommandHandling": False,
    "support_email": "support@openwater.health",
    # Startup connection watchdog (E-104/E-106); also gates the research
    # sample-dataset offer, so deliberately short.
    "connectionTimeoutSec": 12,
    "requireConsole": True,
    "minSensors": 1,
    # ── Keys that lived only in main.py's defaults dict before #546 ──────
    # QA/bench lever: DEBUG_FLAG_HISTO_STALL on both sensors so a scan
    # deterministically loses all camera data ~45 s in (sensor-fw#75).
    "debugHistoStallTest": False,
    # In-app updater source (Research builds only; the clinical build has
    # no updater). None = the production GitHub repo / releases endpoint.
    # Compiled in: an overridable update source is an attack surface.
    "updateRepo": None,
    "updateApiUrl": None,
    # Corrected per-cam CSV ({scan_id}.csv); redundant now that per-cam
    # BFI/BVI lands in scans.db.
    "writeCorrectedCsv": False,
    # Seconds of live data held in memory per plot buffer before the oldest
    # half is ring-trimmed (#256): ~0.7 MB per buffer at 900 s.
    "liveCacheMaxSeconds": 900,
    # Profiling HUD on the PlotViewer, gated on engineeringMode as well.
    "showProfiling": False,
    # Critical-error bug report SMTP block (see bug_report.py); None
    # disables the SMTP path and the report is copied to the clipboard.
    "bug_report_smtp": None,
    # Liquid Glass theme: the default look of the Research variant (macOS
    # included, which is Research-only); Clinical keeps the solid palette.
    # main._load_app_config re-derives it from the effective clinicalMode
    # so --clinical / --research source runs get the matching default.
    "liquidGlass": not CLINICAL_MODE,
    # Internal restore bookkeeping for the alternative settings above:
    # True while camera / TA / safety-ceiling registers may still hold an
    # alternative value, so the first scan after the toggle goes off writes
    # the firmware / laser_params baseline back once.
    "altCameraSettingsDirty": False,
    "altLaserPulseWidthDirty": False,
    "altLaserSafetyCeilingDirty": False,
}

# ── Tiers ──────────────────────────────────────────────────────────────────
CONSTANT_KEYS = frozenset({
    "portableMode", "dataDirectory", "clinicalMode",
    "clinicalModeLeftMask", "clinicalModeRightMask",
    "bfiClampLow", "bfiClampHigh", "bviClampLow", "bviClampHigh",
    "bviLowPassCutoffHz",
    "max_calibration_time_sec", "calibration_scan_duration_sec",
    "test_scan_duration_sec", "calibration_scan_delay_sec",
    "ft_min_mean_per_camera", "ft_min_contrast_per_camera",
    "ft_min_bfi_per_camera", "ft_max_bfi_per_camera",
    "ft_min_bvi_per_camera", "ft_max_bvi_per_camera",
    "ft_max_dark_per_camera",
    "cq_check_duration_sec", "cq_rolling_avg_window",
    "cq_live_activate_frames", "cq_live_clear_frames",
    "cq_dark_threshold_per_camera", "cq_light_threshold_per_camera",
    "cameraTempAlertThresholdC", "scanDataStallTimeoutSec", "tecTripTempC",
    "powerOffUnusedCameras", "histoThrottle", "deferHistoSend",
    "commVerbose", "verboseCommandHandling",
    "support_email", "connectionTimeoutSec", "requireConsole", "minSensors",
    "updateRepo", "updateApiUrl", "writeCorrectedCsv", "liveCacheMaxSeconds",
    "bug_report_smtp",
})

SESSION_KEYS = frozenset({
    "engineeringMode", "forceLaserFail", "debugHistoStallTest",
    "histoCmp", "sensorDebugLogging", "consoleDebugLogging",
    "altCameraSettingsEnabled", "altCameraExposureUs", "altCameraGains",
    "altLaserPulseWidthEnabled", "altLaserPulseWidthUsec",
    "writeTelemetryCsv", "downloadBetaUpdates", "showProfiling",
})

PREFERENCE_KEYS = frozenset({
    "leftMask", "rightMask", "showBfiBvi",
    "bfiMin", "bfiMax", "bviMin", "bviMax",
    "meanMin", "meanMax", "contrastMin", "contrastMax",
    "bfiColor", "bviColor", "plotWindowSec",
    "autoScale", "autoScalePerPlot", "showAxisLabels",
    "bviLowPassEnabled",
    "darkMode", "liquidGlass",
    # Research data output. Persisting these is harmless in a clinical
    # build: the scan-start gate is (!clinicalMode || engineeringMode), and
    # engineeringMode is session-only. autoExportCsv is gated on
    # !clinicalMode alone at scan end.
    "writeRawCsv", "rawCsvDurationSec", "autoExportCsv",
})

STATE_KEYS = frozenset({
    "altCameraSettingsDirty", "altLaserPulseWidthDirty",
    "altLaserSafetyCeilingDirty",
})

PERSISTED_KEYS = PREFERENCE_KEYS | STATE_KEYS

_TIER_OF = {}
for _tier, _keys in (
    (CONSTANT, CONSTANT_KEYS), (SESSION, SESSION_KEYS),
    (PREFERENCE, PREFERENCE_KEYS), (STATE, STATE_KEYS),
):
    for _key in _keys:
        if _key in _TIER_OF:
            raise RuntimeError(f"config key {_key!r} is in two tiers")
        _TIER_OF[_key] = _tier
if set(_TIER_OF) != set(APP_CONFIG):
    raise RuntimeError(
        "config tiers do not partition APP_CONFIG: "
        f"untiered={sorted(set(APP_CONFIG) - set(_TIER_OF))} "
        f"unknown={sorted(set(_TIER_OF) - set(APP_CONFIG))}"
    )
del _tier, _keys, _key


def tier_of(key: str) -> str | None:
    """The tier ``key`` belongs to, or None for a key that does not exist."""
    return _TIER_OF.get(key)


def compiled_config() -> dict:
    """A fresh deep copy of the compiled values (callers may mutate it)."""
    import copy

    return copy.deepcopy(APP_CONFIG)
