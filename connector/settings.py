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
        # The full config dict, exposed to QML as the appConfig property.
        self._app_config = dict(cfg)
        # Compiled baseline (config/app_config.py + dev launch flags); the
        # PREFERENCE/STATE tiers are persisted as a diff against this into
        # the scans.db settings table (see _save_app_config, #546). No file.
        self._baseline_config = dict(baseline_config or {})
        self._settings_store = settings_store
        # Raw CSV (#43, #234) and telemetry CSV (#43, #471) are opt-in;
        # default False so a config missing the key fails closed.
        self._write_raw_csv = bool(cfg.get("writeRawCsv", False))
        raw_csv = cfg.get("rawCsvDurationSec")
        self._raw_csv_duration_sec = float(raw_csv) if raw_csv is not None else None
        self._write_telemetry_csv = bool(cfg.get("writeTelemetryCsv", False))
        # Root directory: caller-supplied (from main.py) wins, else
        # dataDirectory from app config, else the resolved default (see
        # app_paths.writable_root). Scan files, calibrations, debug-bundles
        # and scans.db all live under self._data_root.
        self._directory = (
            data_dir
            or cfg.get("dataDirectory")
            or str(app_paths.writable_root(bool(cfg.get("portableMode", False))))
        )
        os.makedirs(self._directory, exist_ok=True)

    directoryChanged = pyqtSignal()  # Signal to notify QML of directory changes
    appConfigChanged = pyqtSignal()

    @pyqtSlot(bool)
    def setConsoleDebugLogging(self, on: bool) -> None:
        """Toggle the console USB-printf debug log (DEBUG_FLAG_USB_PRINTF).

        Persists ``consoleDebugLogging`` and, when the console is connected,
        pushes the bit live via ``console.enable_usb_printf`` (no restart) —
        the console analogue of the sensor ``setSensorDebugFlag`` toggles. The
        firmware flag is RAM-only, so it is also re-applied on every console
        connect (see the connect handler). When no console is connected the
        value is still persisted and applies on the next connect. Guarded like
        ``setConsoleFan`` so a mid-flight disconnect can't raise out of the
        slot.
        """
        on = bool(on)
        old = self._app_config.get("consoleDebugLogging")
        self._console_debug_logging = on
        self._app_config["consoleDebugLogging"] = on
        self._save_app_config()
        self.appConfigChanged.emit()
        if old != on:
            self._audit.log("settings_changed",
                            {"changes": {"consoleDebugLogging":
                                         {"old": old, "new": on}}})
        if not self._consoleConnected:
            return
        try:
            logger.info("Setting console debug logging %s",
                        "ON" if on else "OFF")
            if not self._interface.console.enable_usb_printf(on):
                logger.warning("Failed to set console debug logging")
        except Exception as e:  # noqa: BLE001 — slot must not raise
            logger.error("Error setting console debug logging: %s", e)

    @pyqtSlot(bool)
    def setForceLaserFail(self, enabled: bool) -> None:
        """Toggle the forceLaserFail flag (Settings → Engineering, issue #464).

        Persists the flag and updates the runtime cache that
        ``set_laser_power_from_config`` reads. Deliberately NOT pushed live:
        the laser param set is written once per console connect, so the
        change takes effect at the next console power-cycle/replug — which
        the interlock test needs anyway, since a tripped interlock stays
        latched until a power-cycle, and that same power-cycle loads the new
        param set. ON selects the SDK's ``laser_params_fault.json``, a set
        engineered to trip the hardware laser-safety interlock at the next
        laser start.
        """
        enabled = bool(enabled)
        old = self._app_config.get("forceLaserFail")
        self._force_laser_fail = enabled
        self._app_config["forceLaserFail"] = enabled
        self._save_app_config()
        self.appConfigChanged.emit()
        if old != enabled:
            self._audit.log("settings_changed",
                            {"changes": {"forceLaserFail":
                                         {"old": old, "new": enabled}}})
        logger.info(
            "forceLaserFail set to %s — applies when laser params load at "
            "the next console connect", enabled,
        )

    # ── Engineering-mode unlock (#706) ───────────────────────────────────
    def _engineering_unlock_available(self) -> bool:
        """True when this build carries the unlock: a Research build or the
        clinical service build. A ``--clinical`` source run counts as
        clinical, so it behaves like the shipped clinical exe."""
        if engineering_unlock is None:
            return False
        return _SERVICE_BUILD or not self._app_config.get("clinicalMode", False)

    @pyqtProperty(bool, constant=True)
    def engineeringUnlockAvailable(self) -> bool:
        """main.qml loads EngineeringUnlockModal.qml only when True; a
        clinical bundle doesn't carry that file."""
        return self._engineering_unlock_available()

    @pyqtProperty(bool, constant=True)
    def serviceBuild(self) -> bool:
        """The clinical service tool (SERVICE_BUILD stamp): a clinical build
        that keeps the engineering unlock. The header labels it."""
        return bool(_SERVICE_BUILD and self._app_config.get("clinicalMode", False))

    @pyqtSlot(str, result=bool)
    def unlockEngineeringMode(self, password: str) -> bool:
        """Turn engineering mode on for this session once the engineering
        password checks out (engineering_unlock). This is the only way to
        turn it on (setConfig / saveConfigs refuse True), and a build
        without the unlock refuses every call. Every attempt is audited."""
        if not self._engineering_unlock_available():
            logger.warning("[Connector] Engineering unlock refused: "
                           "this build has no engineering unlock")
            self._audit.log("engineering_unlock_refused",
                            {"reason": "not available in this build"})
            return False
        return engineering_unlock.unlock(self, password)

    def _refuse_engineering_mode_write(self, source: str) -> None:
        logger.warning("[Connector] %s refused engineeringMode=true: only "
                       "unlockEngineeringMode turns engineering mode on", source)
        self._audit.log("config_write_refused", {
            "source": source, "keys": ["engineeringMode"],
            "reason": "engineering unlock required",
        })

    def _require_engineering_mode(self, action: str) -> bool:
        """Calibrate and Test are engineering actions. The UI hides them,
        and this refuses a direct slot call without engineering mode."""
        if self._app_config.get("engineeringMode") is True:
            return True
        logger.warning("[Connector] %s refused: engineering mode is off", action)
        self._audit.log("engineering_action_refused", {"action": action})
        return False

    @pyqtProperty(str, notify=directoryChanged)
    def directory(self):
        return self._directory

    @directory.setter
    def directory(self, path):
        # Normalize incoming QML "file:///" path. The data directory is a
        # compiled constant since #546 (the settings table lives under it,
        # so it cannot be a saved preference); this setter only re-points
        # the running instance and persists nothing.
        if path.startswith("file:///"):
            path = path[8:] if path[9] != ":" else path[8:]
        self._directory = path
        logger.debug(f"[Connector] Directory set to: {self._directory}")
        self.directoryChanged.emit()

    @property
    def _data_root(self) -> str:
        """Where scan files, calibrations, debug-bundles, and
        the scan DB live: self._directory/data. A sibling of the app-wide
        logs/ folder (written by main.py only; the connector reads it for
        the engineering log viewer — see appLogPath)."""
        return os.path.join(self._directory, app_paths.DATA_DIRNAME)

    # ── App config — generic read/write API ──────────────────────────────────

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
        """The running config as pretty JSON, for the debug bundle (there is
        no config file to copy any more — this is what the app actually
        runs with, tier per key included)."""
        payload = {
            k: {"value": v, "tier": config_store.tier_of(k)}
            for k, v in sorted(self._app_config.items())
        }
        return json.dumps(payload, indent=2, sort_keys=True, default=str)

    def _save_app_config(self):
        """Persist the PREFERENCE / STATE tiers as a diff vs the compiled
        baseline into the scans.db settings table (#546).

        SESSION keys (engineering mode, debug flags, alt settings) and
        CONSTANT keys are never written anywhere; a key back at its
        compiled value has its row deleted. No-op without a store (unit
        tests that build the connector with __new__).
        """
        store = getattr(self, "_settings_store", None)
        if store is None:
            return
        to_save, to_delete = config_store.persistable_diff(
            self._app_config, self._baseline_config
        )
        if to_save:
            store.save(to_save)
        if to_delete:
            store.delete(to_delete)

    def _refuse_config_write(self, keys, source: str) -> None:
        """A runtime write to a compiled CONSTANT (or a key that is not in
        the compiled config at all) is refused and recorded — that is the
        tier-1 contract (#546). The tier table is the authority, not this
        instance's dict, so a connector built with a partial config in a
        test can still set any session/preference key."""
        keys = sorted(keys)
        logger.warning(
            "[Connector] %s refused for compiled/unknown config key(s) %s",
            source, keys,
        )
        self._audit.log("config_write_refused",
                        {"source": source, "keys": keys})

    @pyqtSlot(str, 'QVariant')
    def setConfig(self, key: str, value):
        """Update a single config key, persist to disk, and notify QML."""
        # QML arrays/objects reach 'QVariant' slots as QJSValue, which is
        # not JSON-serializable — the first array-valued key (#446's
        # altCameraGains) crashed the app inside the JSON persist. Unwrap to
        # plain Python at the bridge, once, for every caller.
        if isinstance(value, QJSValue):
            value = value.toVariant()
        if config_store.tier_of(key) is None or config_store.refused_keys([key]):
            self._refuse_config_write([key], "setConfig")
            return
        # Engineering mode can be switched off here ("Disable engineering
        # mode") but only unlockEngineeringMode switches it on (#706).
        if key == "engineeringMode" and value is not False:
            self._refuse_engineering_mode_write("setConfig")
            return
        old = self._app_config.get(key)
        self._app_config[key] = value
        self._save_app_config()
        self.appConfigChanged.emit()
        logger.debug(f"[Connector] Config set: {key} = {value!r}")
        if old != value:
            self._audit.log("settings_changed",
                            {"changes": {key: {"old": old, "new": value}}})
        if old != value and key in ("downloadBetaUpdates", "engineeringMode"):
            self._refresh_update_checks()
        if old != value and key == "bviLowPassEnabled":
            self._apply_bvi_lpf_setting()

    @pyqtSlot('QVariantMap')
    def saveConfigs(self, configs: dict):
        """Update multiple config keys at once, persist to disk, and notify QML."""
        # Same QJSValue unwrap as setConfig — a QVariantMap converts its
        # top level, but array/object VALUES still arrive as QJSValue.
        configs = {
            k: (v.toVariant() if isinstance(v, QJSValue) else v)
            for k, v in configs.items()
        }
        refused = [
            k for k in configs
            if config_store.tier_of(k) is None or config_store.refused_keys([k])
        ]
        if refused:
            self._refuse_config_write(refused, "saveConfigs")
            configs = {k: v for k, v in configs.items() if k not in refused}
        if "engineeringMode" in configs and configs["engineeringMode"] is not False:
            self._refuse_engineering_mode_write("saveConfigs")
            del configs["engineeringMode"]
        changes = {}
        for k, v in configs.items():
            old = self._app_config.get(k)
            if old != v:
                changes[k] = {"old": old, "new": v}
        self._app_config.update(configs)
        self._save_app_config()
        self.appConfigChanged.emit()
        logger.debug(f"[Connector] Config saved: {sorted(configs.keys())}")
        if changes:
            self._audit.log("settings_changed", {"changes": changes})
        if any(k in changes for k in ("downloadBetaUpdates", "engineeringMode")):
            self._refresh_update_checks()
        if "bviLowPassEnabled" in changes:
            self._apply_bvi_lpf_setting()

    # ── BVI display low-pass (#228, #552) ─────────────────────────────
    def _bvi_lpf_cutoff_hz(self) -> float:
        """Effective display low-pass cutoff: the compiled
        bviLowPassCutoffHz constant, gated by the research-only Settings
        switch. A clinical build has no switch, so a persisted
        bviLowPassEnabled=false can never turn the filter off there — the
        same forcing BloodFlow.qml applies to autoScale."""
        enabled = self._app_config.get("bviLowPassEnabled")
        if self._app_config.get("clinicalMode") is True:
            enabled = True
        return effective_bvi_lpf_cutoff(
            enabled, self._app_config.get("bviLowPassCutoffHz"))

    @staticmethod
    def _describe_bvi_lpf(cutoff_hz: float) -> str:
        return (f"{cutoff_hz:g} Hz cutoff" if cutoff_hz > 0.0
                else "off (bviLowPassEnabled false or cutoff <= 0)")

    def _apply_bvi_lpf_setting(self) -> None:
        """Push the Settings switch to the running live source so the
        toggle takes effect mid-scan (#552). No-op without a live scan;
        the next startCapture reads the config afresh anyway."""
        src = self._live_scan_source
        if src is None:
            return
        cutoff = self._bvi_lpf_cutoff_hz()
        src.set_bvi_lpf_cutoff_hz(cutoff)
        logger.info("BVI display low-pass (live): %s",
                    self._describe_bvi_lpf(cutoff))

    # Sensor debug-flag config keys surfaced as live Settings → Engineering
    # toggles, mapped to the runtime cache attribute that
    # _compute_sensor_debug_flags() reads.
    _SENSOR_DEBUG_FLAG_ATTRS = {
        "histoCmp": "_histo_cmp",
        "sensorDebugLogging": "_sensor_debug_logging",
        "debugHistoStallTest": "_histo_stall_test",
    }

    @pyqtSlot(str, bool)
    def setSensorDebugFlag(self, key: str, enabled: bool) -> None:
        """Toggle a sensor debug-flag config key, persist it, and re-push the
        recomputed debug-flag bitmask to every connected sensor immediately.

        Mirrors the live ``setConsoleFan`` engineering toggle — no app restart
        or reconnect needed. Unknown keys are ignored. When no sensor is
        connected the value is still persisted and applies on the next
        connect via ``_init_sensor``.
        """
        attr = self._SENSOR_DEBUG_FLAG_ATTRS.get(key)
        if attr is None:
            logger.warning("setSensorDebugFlag: unknown key %r", key)
            return
        enabled = bool(enabled)
        old = self._app_config.get(key)
        setattr(self, attr, enabled)
        self._app_config[key] = enabled
        self._save_app_config()
        self.appConfigChanged.emit()
        if old != enabled:
            self._audit.log("settings_changed",
                            {"changes": {key: {"old": old, "new": enabled}}})
        self._apply_sensor_debug_flags()

    @pyqtSlot(bool)
    def setWriteRawCsv(self, enabled: bool) -> None:
        """Update writeRawCsv in both the runtime cache and persisted config."""
        self._write_raw_csv = bool(enabled)
        self._app_config["writeRawCsv"] = self._write_raw_csv
        self._save_app_config()
        self.appConfigChanged.emit()
        logger.debug(f"[Connector] writeRawCsv set to {self._write_raw_csv}")

    @pyqtSlot(bool)
    def setWriteTelemetryCsv(self, enabled: bool) -> None:
        """Update writeTelemetryCsv in both the runtime cache and persisted config."""
        self._write_telemetry_csv = bool(enabled)
        self._app_config["writeTelemetryCsv"] = self._write_telemetry_csv
        self._save_app_config()
        self.appConfigChanged.emit()
        logger.debug(f"[Connector] writeTelemetryCsv set to {self._write_telemetry_csv}")

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
