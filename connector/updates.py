"""Firmware autoupdate (Research builds) and the app self-updater."""
import threading
from pathlib import Path

from PyQt6.QtCore import pyqtProperty, pyqtSignal, pyqtSlot
from connector._common import DEVICES, RUNNING, logger, spawn
from omotion.firmware_update import FirmwareKind, check_latest, is_update_available
from config.app_config import CLINICAL_MODE as _CLINICAL_BUILD

# The app self-updater is compiled out of clinical builds (#543, tracker
# M-02): openwater.spec excludes ``services.app_updater`` when CLINICAL_MODE
# is stamped True, and this import is skipped on the same constant, so the
# clinical exe neither ships nor loads that code. The runtime
# ``clinicalMode`` checks on the update slots stay as the backstop for a
# source run launched with ``--clinical`` (the constant is False there).
if _CLINICAL_BUILD:
    app_updater = None
else:
    try:
        from services import app_updater
    except ImportError:   # excluded from this bundle
        app_updater = None


def _device_property(type_, store: str, device: str, notify):
    """A read-only pyqtProperty over ``self.<store>[device]``."""
    return pyqtProperty(type_, lambda self: getattr(self, store)[device], notify=notify)


# Per-phase toast text for the SDK's flash progress. Never the SDK's own
# per-step text: it changes on every erase step and jitters in the UI.
# update() blocks through the last two phases until the device is back on
# USB; "power_cycle" waits for the user to do it (#739).
_FLASH_PHASE_LABELS = {
    "erase": "Erasing",
    "download": "Writing",
    "verify": "Verifying",
    "wait": "Waiting for the device to restart…",
    "power_cycle": "Firmware written. Power-cycle the device "
                   "now; the update finishes when it reconnects.",
}


class UpdatesMixin:
    """Firmware autoupdate (Research builds) and the app self-updater."""

    def _setup_updates(self) -> None:
        self._firmware_latest: dict[str, str] = {n: "" for n in DEVICES}
        self._firmware_update_available: dict[str, bool] = {n: False for n in DEVICES}
        self._firmware_latest_by_kind: dict[str, str] = {}   # "console"/"sensor" -> tag
        self._firmware_checking_kinds: set = set()           # in-flight network checks
        self._firmware_check_lock = threading.Lock()         # guards check-then-add on the set
        self._firmware_check_generation: int = 0
        self._firmware_update_in_progress: str | None = None  # deviceKey being flashed
        self._update_in_progress = False                      # app self-update running

    # Firmware autoupdate (Research builds only; engineering mode adds only
    # the beta channel)
    firmwareUpdateInfoChanged = pyqtSignal()                # notify for the properties below
    firmwareUpdateAvailable = pyqtSignal(str, str, str)     # deviceKey, current, latest
    firmwareUpdateProgress = pyqtSignal(str, str, int, str) # deviceKey, stage, percent or -1, msg
    firmwareUpdateFinished = pyqtSignal(str, bool, str)     # deviceKey, ok, msg
    # App update signals
    updateAvailable = pyqtSignal(str, str)   # (latest_version, download_url)
    updateNotAvailable = pyqtSignal()
    updateCheckFailed = pyqtSignal(str)      # error message
    updateProgress = pyqtSignal(str)         # human-readable progress status

    @pyqtProperty(bool, notify=firmwareUpdateInfoChanged)
    def anyFirmwareUpdateAvailable(self) -> bool:
        return any(self._firmware_update_available.values())

    consoleFirmwareUpdateAvailable = _device_property(
        bool, "_firmware_update_available", "console", firmwareUpdateInfoChanged)
    leftSensorFirmwareUpdateAvailable = _device_property(
        bool, "_firmware_update_available", "left", firmwareUpdateInfoChanged)
    rightSensorFirmwareUpdateAvailable = _device_property(
        bool, "_firmware_update_available", "right", firmwareUpdateInfoChanged)
    consoleFirmwareLatest = _device_property(
        str, "_firmware_latest", "console", firmwareUpdateInfoChanged)
    leftSensorFirmwareLatest = _device_property(
        str, "_firmware_latest", "left", firmwareUpdateInfoChanged)
    rightSensorFirmwareLatest = _device_property(
        str, "_firmware_latest", "right", firmwareUpdateInfoChanged)

    @staticmethod
    def _kind_for_device(name: str) -> FirmwareKind:
        return FirmwareKind.CONSOLE if name == "console" else FirmwareKind.SENSOR

    @staticmethod
    def _devices_for_kind(kind: FirmwareKind) -> list[str]:
        return ["console"] if kind == FirmwareKind.CONSOLE else ["left", "right"]

    def _beta_enabled(self) -> bool:
        """Prerelease (beta) channel is active only for a Research build with
        engineering mode unlocked and the beta toggle on. Clinical never opts
        in; disabling engineering mode drops back to stable. Shared by the
        firmware updater and the app self-updater (#386)."""
        return not self._clinical and self._engineering and self._flag("downloadBetaUpdates")

    # ── Firmware update detection ────────────────────────────────────────
    def _maybe_check_firmware_update(self, name: str) -> None:
        """In a Research build, ensure this device's firmware-update
        availability is computed: reuse a cached 'latest' for the kind, or
        kick one background GitHub check per kind per session. Clinical
        builds never check, even with engineering mode unlocked (#386)."""
        if self._clinical or not self._firmware_versions.get(name):
            return
        kind = self._kind_for_device(name)
        if self._firmware_latest_by_kind.get(kind.value):
            self._recompute_firmware_update(name)   # latest already known; no network
            return
        with self._firmware_check_lock:
            if kind in self._firmware_checking_kinds:
                return                              # a check is already in flight
            self._firmware_checking_kinds.add(kind)
        spawn(self._firmware_check_worker, kind, self._firmware_check_generation)

    def _firmware_check_worker(self, kind: FirmwareKind, generation: int | None = None) -> None:
        try:
            info = check_latest(kind, include_prerelease=self._beta_enabled())
        except Exception:                           # defensive; check_latest is fail-soft
            info = None
        finally:
            self._firmware_checking_kinds.discard(kind)
        if info is None:
            return                                  # leave kind unchecked so it retries
        # A beta toggle (or other refresh) bumps the generation; a worker that
        # started under an older generation captured a now-stale beta flag, so
        # discard its result rather than clobber the fresh one.
        if generation is not None and generation != self._firmware_check_generation:
            return
        self._firmware_latest_by_kind[kind.value] = info.tag
        for dev in self._devices_for_kind(kind):
            self._recompute_firmware_update(dev)

    def _recompute_firmware_update(self, name: str) -> None:
        installed = self._firmware_versions.get(name)
        if not installed:
            return  # device not connected / version unknown
        latest = self._firmware_latest_by_kind.get(self._kind_for_device(name).value, "")
        avail = bool(latest) and is_update_available(
            installed, latest, prerelease=self._beta_enabled())
        self._firmware_latest[name] = latest
        self._firmware_update_available[name] = avail
        self.firmwareUpdateInfoChanged.emit()
        if avail:
            logger.info("Firmware update available for %s: %s -> %s", name, installed, latest)
            self.firmwareUpdateAvailable.emit(name, installed, latest)

    def _refresh_firmware_update_check(self) -> None:
        """Invalidate the firmware-latest caches and re-run detection for every
        connected device. Called when downloadBetaUpdates toggles so the
        banner/Settings card reflect the new release pool immediately."""
        self._firmware_latest_by_kind.clear()
        self._firmware_checking_kinds.clear()
        self._firmware_check_generation += 1
        for name in DEVICES:
            self._firmware_latest[name] = ""
            self._firmware_update_available[name] = False
        self.firmwareUpdateInfoChanged.emit()
        for name in DEVICES:
            self._maybe_check_firmware_update(name)

    def _refresh_update_checks(self) -> None:
        """Re-evaluate BOTH updaters after a change to the beta channel or
        engineering mode. Firmware re-detects for connected devices; the app
        self-updater re-checks, but only in a Research build, so a Clinical
        build makes no outbound GitHub call (#96, #386)."""
        self._refresh_firmware_update_check()
        if not self._clinical:
            # Withdraw the currently-shown app-update offer synchronously (it
            # may be a now-ineligible beta), mirroring the firmware side. The
            # re-check re-offers only if a valid update still exists, so a
            # failed re-check leaves the stale offer withdrawn, not clickable.
            self.updateNotAvailable.emit()
            self.checkForUpdates()

    # ── Firmware flashing ────────────────────────────────────────────────
    @pyqtSlot(str, result=bool)
    def startFirmwareUpdate(self, device_key: str) -> bool:
        """Download the latest firmware for device_key and flash it over DFU.
        Research-only (blocked in clinical builds, #386); refused during a
        scan or while another update runs."""
        if device_key not in DEVICES or self._clinical:
            return False
        if self._state == RUNNING or self._running:
            refusal = "Cannot update firmware during a scan."
        elif self._firmware_update_in_progress is not None:
            refusal = "Another firmware update is in progress."
        elif not self._firmware_update_available.get(device_key, False):
            refusal = "No update available for this device."
        else:
            self._firmware_update_in_progress = device_key
            spawn(self._firmware_update_worker, device_key)
            return True
        self.firmwareUpdateFinished.emit(device_key, False, refusal)
        return False

    def _firmware_update_worker(self, device_key: str) -> None:
        import tempfile
        from omotion.firmware_update import FirmwareUpdater, download_firmware

        def progress(stage: str, percent: int, text: str) -> None:
            self.firmwareUpdateProgress.emit(device_key, stage, percent, text)

        def flash_progress(p) -> None:
            progress(p.phase, p.percent if p.percent is not None else -1,
                     _FLASH_PHASE_LABELS.get(p.phase, str(p.phase).capitalize()))

        ok, msg = False, ""
        try:
            progress("check", -1, "Checking latest release…")
            info = check_latest(self._kind_for_device(device_key),
                                include_prerelease=self._beta_enabled())
            if info is None:
                raise RuntimeError("Could not reach GitHub to fetch firmware.")
            progress("download", -1, f"Downloading {info.tag}…")
            bin_path = download_firmware(info, Path(tempfile.gettempdir()) / "om-firmware")
            handle = getattr(self._interface, device_key, None)
            if handle is None:
                raise RuntimeError("Device handle unavailable.")
            progress("flash", -1, "Entering DFU and flashing…")
            result = FirmwareUpdater().update(handle, bin_path, progress_cb=flash_progress)
            ok = bool(result.success)
            if not ok:
                msg = "The firmware update reported a failure."
            elif result.needs_power_cycle:
                # The image is written but the device did not come back within
                # the SDK's wait, so it runs the old firmware until power-cycled.
                msg = ("Firmware written. Power-cycle the device now — it will "
                       "reconnect with the new version shortly.")
            else:
                msg = "Firmware updated. The device restarted with the new version."
        except Exception as e:                       # noqa: BLE001 - reported to UI
            logger.exception("Firmware update failed for %s", device_key)
            ok, msg = False, str(e)
        finally:
            self._firmware_update_in_progress = None
            # Force re-detection so the banner/card clear once the new version
            # is read back after the device re-enumerates.
            self._firmware_latest_by_kind.pop(self._kind_for_device(device_key).value, None)
            self.firmwareUpdateFinished.emit(device_key, ok, msg)

    # ── App self-update ──────────────────────────────────────────────────
    # The implementation lives in services/app_updater.py, which clinical
    # builds do not ship (#543). These slots and the update signals keep the
    # QML contract identical in every variant; without the module they are
    # no-ops, and the runtime clinicalMode guard still applies first.

    @pyqtSlot()
    def checkForUpdates(self):
        """Check GitHub releases for a newer version (runs in a background thread)."""
        spawn(self._check_for_updates_worker)

    def _check_for_updates_worker(self):
        if self._clinical:
            return   # clinical builds never check for app updates (#96, #386)
        if app_updater is None:
            logger.info("App updater is not part of this build; update check skipped")
            return
        app_updater.check_for_updates(self)

    @pyqtSlot(str)
    def applyUpdate(self, download_url: str):
        """Download the update bundle, verify it, and launch the upgrade.
        Guarded against re-entry so repeated clicks can't spawn racing
        download threads against the same file."""
        if self._clinical:
            return   # clinical builds never install app updates (#96, #386)
        if self._update_in_progress:
            logger.info("Update already in progress; ignoring repeat request")
            return
        self._update_in_progress = True
        spawn(self._apply_update_worker, download_url)

    def _apply_update_worker(self, download_url: str):
        try:
            if app_updater is None:
                logger.info("App updater is not part of this build; applyUpdate ignored")
                return
            app_updater.apply_update(self, download_url)
        finally:
            # Cleared so a failed attempt can be retried (on success the app
            # is already quitting).
            self._update_in_progress = False
