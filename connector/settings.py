"""appConfig read/write, the data directory, and the engineering unlock."""
import json
import os

from PyQt6.QtQml import QJSValue
from PyQt6.QtCore import pyqtProperty, pyqtSignal, pyqtSlot
from connector._common import logger
from config.app_config import CLINICAL_MODE as _CLINICAL_BUILD
from config.app_config import SERVICE_BUILD as _SERVICE_BUILD
from plot.data_sources import effective_bvi_lpf_cutoff
from utils import app_paths, config_store

# The engineering-mode unlock is compiled out of clinical builds (#706):
# openwater.spec / build_nuitka.ps1 leave ``services.engineering_unlock``
# (and EngineeringUnlockModal.qml) out of a clinical bundle unless it is
# stamped SERVICE_BUILD, and this import is skipped on the same constants.
# Without the module nothing can turn engineeringMode on. A source run
# with ``--clinical`` still has it and relies on the runtime check in
# _engineering_unlock_available().
if _CLINICAL_BUILD and not _SERVICE_BUILD:
    engineering_unlock = None
else:
    try:
        from services import engineering_unlock
    except ImportError:   # excluded from this bundle
        engineering_unlock = None


class SettingsMixin:
    """appConfig read/write, the data directory, and the engineering unlock."""

    def _setup_settings(self, cfg: dict, baseline_config, settings_store, data_dir) -> None:
        self._app_config = dict(cfg)   # exposed to QML as appConfig
        # Compiled baseline (config/app_config.py + dev launch flags). The
        # PREFERENCE/STATE tiers are persisted as a diff against it into the
        # scans.db settings table (see _save_app_config, #546). No file.
        self._baseline_config = dict(baseline_config or {})
        self._settings_store = settings_store
        # Raw CSV (#43, #234) and telemetry CSV (#43, #471) are opt-in;
        # default False so a config missing the key fails closed.
        self._write_raw_csv = bool(cfg.get("writeRawCsv", False))
        raw_csv = cfg.get("rawCsvDurationSec")
        self._raw_csv_duration_sec = float(raw_csv) if raw_csv is not None else None
        self._write_telemetry_csv = bool(cfg.get("writeTelemetryCsv", False))
        # Root directory: main.py's data_dir wins, else dataDirectory from
        # app config, else app_paths.writable_root. Scan files, calibrations,
        # debug-bundles and scans.db all live under self._data_root.
        self._directory = (data_dir or cfg.get("dataDirectory")
                           or str(app_paths.writable_root(bool(cfg.get("portableMode", False)))))
        os.makedirs(self._directory, exist_ok=True)

    # Never emitted: the data directory is a compiled constant since #546 (the
    # settings table lives under it). Kept as the notify signal QML binds to.
    directoryChanged = pyqtSignal()
    appConfigChanged = pyqtSignal()

    # ── Config store ─────────────────────────────────────────────────────
    def _flag(self, key: str) -> bool:
        """Truthiness of one appConfig key (missing = False)."""
        return bool(self._app_config.get(key, False))

    @property
    def _clinical(self) -> bool:
        return self._flag("clinicalMode")

    @property
    def _engineering(self) -> bool:
        return self._flag("engineeringMode")

    @pyqtProperty('QVariantMap', notify=appConfigChanged)
    def appConfig(self):
        return self._app_config

    def _effective_config_json(self) -> str:
        """The running config as pretty JSON (value + tier per key), for the
        debug bundle: there is no config file to copy."""
        payload = {k: {"value": v, "tier": config_store.tier_of(k)}
                   for k, v in sorted(self._app_config.items())}
        return json.dumps(payload, indent=2, sort_keys=True, default=str)

    def _save_app_config(self):
        """Persist the PREFERENCE / STATE tiers as a diff vs the compiled
        baseline into the scans.db settings table (#546). SESSION and
        CONSTANT keys are never written; a key back at its compiled value has
        its row deleted. No-op without a store (connectors built with
        __new__ in unit tests)."""
        store = getattr(self, "_settings_store", None)
        if store is None:
            return
        to_save, to_delete = config_store.persistable_diff(self._app_config, self._baseline_config)
        if to_save:
            store.save(to_save)
        if to_delete:
            store.delete(to_delete)

    def _apply_config(self, values: dict, audit: bool = True) -> dict:
        """Store ``values``, persist, notify QML. Returns the keys whose value
        changed as ``{key: {"old", "new"}}`` and, with ``audit``, records them."""
        changes = {k: {"old": self._app_config.get(k), "new": v}
                   for k, v in values.items() if self._app_config.get(k) != v}
        self._app_config.update(values)
        self._save_app_config()
        self.appConfigChanged.emit()
        if audit and changes:
            self._audit.log("settings_changed", {"changes": changes})
        return changes

    def _writable_configs(self, configs: dict, source: str) -> dict:
        """``configs`` minus the keys a runtime write may not touch; each
        refusal is logged and audited under ``source``.

        QML arrays/objects reach 'QVariant' slots (and QVariantMap values) as
        QJSValue, which is not JSON-serializable (#446's altCameraGains
        crashed the persist), so values are unwrapped to plain Python here.
        """
        configs = {k: (v.toVariant() if isinstance(v, QJSValue) else v)
                   for k, v in configs.items()}
        # Tier-1 contract (#546): compiled CONSTANTs and unknown keys are
        # refused. The tier table is the authority, not this instance's dict,
        # so a test connector with a partial config can still set any
        # session/preference key.
        refused = sorted(k for k in configs if config_store.tier_of(k) is None
                         or config_store.refused_keys([k]))
        if refused:
            logger.warning("[Connector] %s refused for compiled/unknown config key(s) %s",
                           source, refused)
            self._audit.log("config_write_refused", {"source": source, "keys": refused})
            configs = {k: v for k, v in configs.items() if k not in refused}
        # Engineering mode can be switched off here ("Disable engineering
        # mode") but only unlockEngineeringMode switches it on (#706).
        if configs.get("engineeringMode", False) is not False:
            logger.warning("[Connector] %s refused engineeringMode=true: only "
                           "unlockEngineeringMode turns engineering mode on", source)
            self._audit.log("config_write_refused", {
                "source": source, "keys": ["engineeringMode"],
                "reason": "engineering unlock required"})
            del configs["engineeringMode"]
        return configs

    def _commit_configs(self, configs: dict) -> None:
        changes = self._apply_config(configs)
        logger.debug("[Connector] Config saved: %r", configs)
        if "downloadBetaUpdates" in changes or "engineeringMode" in changes:
            self._refresh_update_checks()
        if "bviLowPassEnabled" in changes:
            self._apply_bvi_lpf_setting()

    @pyqtSlot(str, 'QVariant')
    def setConfig(self, key: str, value):
        """Update a single config key, persist it, and notify QML."""
        configs = self._writable_configs({key: value}, "setConfig")
        if configs:   # a refused key changes nothing and notifies nobody
            self._commit_configs(configs)

    @pyqtSlot('QVariantMap')
    def saveConfigs(self, configs: dict):
        """Update multiple config keys at once, persist them, and notify QML."""
        self._commit_configs(self._writable_configs(configs, "saveConfigs"))

    # ── Engineering toggles ──────────────────────────────────────────────
    @pyqtSlot(bool)
    def setConsoleDebugLogging(self, on: bool) -> None:
        """Toggle the console USB-printf debug log (DEBUG_FLAG_USB_PRINTF).

        Persists ``consoleDebugLogging`` and, when the console is connected,
        pushes the bit live (no restart). The firmware flag is RAM-only, so
        the connect handler re-applies it on every console connect.
        """
        on = bool(on)
        self._console_debug_logging = on
        self._apply_config({"consoleDebugLogging": on})
        if not self._consoleConnected:
            return
        try:
            logger.info("Setting console debug logging %s", "ON" if on else "OFF")
            if not self._interface.console.enable_usb_printf(on):
                logger.warning("Failed to set console debug logging")
        except Exception as e:  # noqa: BLE001 — a mid-flight disconnect must not raise
            logger.error("Error setting console debug logging: %s", e)

    @pyqtSlot(bool)
    def setForceLaserFail(self, enabled: bool) -> None:
        """Toggle the forceLaserFail flag (Settings → Engineering, #464).

        ON selects the SDK's ``laser_params_fault.json``, a set engineered to
        trip the hardware laser-safety interlock at the next laser start.
        Deliberately NOT pushed live: the laser param set is written once per
        console connect, and the interlock test needs a power-cycle anyway (a
        tripped interlock stays latched until then).
        """
        enabled = bool(enabled)
        self._force_laser_fail = enabled
        self._apply_config({"forceLaserFail": enabled})
        logger.info("forceLaserFail set to %s — applies when laser params load at "
                    "the next console connect", enabled)

    # Sensor debug-flag config keys (live Settings → Engineering toggles) and
    # the runtime cache attribute _compute_sensor_debug_flags() reads.
    _SENSOR_DEBUG_FLAG_ATTRS = {
        "histoCmp": "_histo_cmp",
        "sensorDebugLogging": "_sensor_debug_logging",
        "debugHistoStallTest": "_histo_stall_test",
    }

    @pyqtSlot(str, bool)
    def setSensorDebugFlag(self, key: str, enabled: bool) -> None:
        """Toggle a sensor debug-flag config key, persist it, and re-push the
        debug-flag bitmask to every connected sensor at once. Unknown keys
        are ignored. With no sensor connected the value applies on the next
        connect via ``_init_sensor``."""
        attr = self._SENSOR_DEBUG_FLAG_ATTRS.get(key)
        if attr is None:
            logger.warning("setSensorDebugFlag: unknown key %r", key)
            return
        setattr(self, attr, bool(enabled))
        self._apply_config({key: bool(enabled)})
        self._apply_sensor_debug_flags()

    @pyqtSlot(bool)
    def setWriteRawCsv(self, enabled: bool) -> None:
        """Update writeRawCsv in both the runtime cache and persisted config."""
        self._write_raw_csv = bool(enabled)
        self._apply_config({"writeRawCsv": self._write_raw_csv}, audit=False)

    @pyqtSlot(bool)
    def setWriteTelemetryCsv(self, enabled: bool) -> None:
        """Update writeTelemetryCsv in both the runtime cache and persisted config."""
        self._write_telemetry_csv = bool(enabled)
        self._apply_config({"writeTelemetryCsv": self._write_telemetry_csv}, audit=False)

    @pyqtSlot('QVariant')
    def setRawCsvDurationSec(self, value) -> None:
        """Update rawCsvDurationSec in the runtime cache and persisted config.
        ``None`` / ``null`` / empty / unparseable disables the limit (full scan)."""
        duration = None
        if value is not None and str(value).strip() not in ("", "null", "undefined"):
            try:
                duration = float(value)
            except (TypeError, ValueError):
                logger.debug("[Connector] rawCsvDurationSec %r is not a number", value)
        self._raw_csv_duration_sec = duration
        self._apply_config({"rawCsvDurationSec": duration}, audit=False)

    # ── Engineering-mode unlock (#706) ───────────────────────────────────
    def _engineering_unlock_available(self) -> bool:
        """True when this build carries the unlock: a Research build or the
        clinical service build. A ``--clinical`` source run counts as
        clinical, so it behaves like the shipped clinical exe."""
        return engineering_unlock is not None and (_SERVICE_BUILD or not self._clinical)

    @pyqtProperty(bool, constant=True)
    def engineeringUnlockAvailable(self) -> bool:
        """main.qml loads EngineeringUnlockModal.qml only when True; a
        clinical bundle doesn't carry that file."""
        return self._engineering_unlock_available()

    @pyqtProperty(bool, constant=True)
    def serviceBuild(self) -> bool:
        """The clinical service tool (SERVICE_BUILD stamp): a clinical build
        that keeps the engineering unlock. The header labels it."""
        return bool(_SERVICE_BUILD and self._clinical)

    @pyqtSlot(str, result=bool)
    def unlockEngineeringMode(self, password: str) -> bool:
        """Turn engineering mode on for this session once the engineering
        password checks out. The only way to turn it on (setConfig /
        saveConfigs refuse True); a build without the unlock refuses every
        call. Every attempt is audited."""
        if not self._engineering_unlock_available():
            logger.warning("[Connector] Engineering unlock refused: "
                           "this build has no engineering unlock")
            self._audit.log("engineering_unlock_refused", {"reason": "not available in this build"})
            return False
        return engineering_unlock.unlock(self, password)

    def _require_engineering_mode(self, action: str) -> bool:
        """Calibrate and Test are engineering actions. The UI hides them,
        and this refuses a direct slot call without engineering mode."""
        if self._app_config.get("engineeringMode") is True:
            return True
        logger.warning("[Connector] %s refused: engineering mode is off", action)
        self._audit.log("engineering_action_refused", {"action": action})
        return False

    # ── Data directory ───────────────────────────────────────────────────
    @pyqtProperty(str, notify=directoryChanged)
    def directory(self):
        """The data directory. Read-only: a compiled constant since #546."""
        return self._directory

    @property
    def _data_root(self) -> str:
        """self._directory/data: scan files, calibrations, debug-bundles and
        the scan DB. A sibling of the app-wide logs/ folder, which main.py
        writes and the connector only reads (see appLogPath)."""
        return os.path.join(self._directory, app_paths.DATA_DIRNAME)

    # ── BVI display low-pass (#228, #552) ────────────────────────────────
    def _bvi_lpf_cutoff_hz(self) -> float:
        """Effective display low-pass cutoff: the compiled bviLowPassCutoffHz
        constant, gated by the research-only Settings switch. A clinical
        build has no switch, so a persisted bviLowPassEnabled=false can never
        turn the filter off there (BloodFlow.qml forces autoScale likewise)."""
        enabled = self._app_config.get("bviLowPassEnabled")
        if self._app_config.get("clinicalMode") is True:
            enabled = True
        return effective_bvi_lpf_cutoff(enabled, self._app_config.get("bviLowPassCutoffHz"))

    @staticmethod
    def _describe_bvi_lpf(cutoff_hz: float) -> str:
        return (f"{cutoff_hz:g} Hz cutoff" if cutoff_hz > 0.0
                else "off (bviLowPassEnabled false or cutoff <= 0)")

    def _apply_bvi_lpf_setting(self) -> None:
        """Push the Settings switch to the running live source so the toggle
        takes effect mid-scan (#552). No-op without a live scan; the next
        startCapture reads the config afresh anyway."""
        src = self._live_scan_source
        if src is None:
            return
        cutoff = self._bvi_lpf_cutoff_hz()
        src.set_bvi_lpf_cutoff_hz(cutoff)
        logger.info("BVI display low-pass (live): %s", self._describe_bvi_lpf(cutoff))
