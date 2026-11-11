"""Firmware autoupdate (Research builds) and the app self-updater."""
import threading
from pathlib import Path

from PyQt6.QtCore import pyqtProperty, pyqtSignal, pyqtSlot
from connector._common import DEVICES, RUNNING, logger
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


class UpdatesMixin:
    """Firmware autoupdate (Research builds) and the app self-updater."""

    def _setup_updates(self) -> None:
        self._firmware_latest: dict[str, str] = {n: "" for n in DEVICES}
        self._firmware_update_available: dict[str, bool] = {n: False for n in DEVICES}
        self._firmware_latest_by_kind: dict[str, str] = {}   # "console"/"sensor" -> tag
        self._firmware_checking_kinds: set = set()           # in-flight network checks
        self._firmware_check_lock = threading.Lock()         # guards check-then-add on _firmware_checking_kinds
        self._firmware_check_generation: int = 0
        self._firmware_update_in_progress: str | None = None  # deviceKey being flashed

    # Firmware autoupdate (Research builds only; engineering mode adds only
    # the beta channel)
    firmwareUpdateInfoChanged = pyqtSignal()                # notify for the properties below
    firmwareUpdateAvailable = pyqtSignal(str, str, str)     # deviceKey, current, latest
    firmwareUpdateProgress = pyqtSignal(str, str, int, str) # deviceKey, stage, percent(-1=indeterminate), msg
    firmwareUpdateFinished = pyqtSignal(str, bool, str)     # deviceKey, ok, msg
    # App update signals
    updateAvailable = pyqtSignal(str, str)   # (latest_version, download_url)
    updateNotAvailable = pyqtSignal()
    updateCheckFailed = pyqtSignal(str)      # error message
    updateProgress = pyqtSignal(str)         # human-readable progress status

    @pyqtProperty(bool, notify=firmwareUpdateInfoChanged)
    def anyFirmwareUpdateAvailable(self) -> bool:
        return any(self._firmware_update_available.values())

    @pyqtProperty(bool, notify=firmwareUpdateInfoChanged)
    def consoleFirmwareUpdateAvailable(self) -> bool:
        return self._firmware_update_available["console"]

    @pyqtProperty(bool, notify=firmwareUpdateInfoChanged)
    def leftSensorFirmwareUpdateAvailable(self) -> bool:
        return self._firmware_update_available["left"]

    @pyqtProperty(bool, notify=firmwareUpdateInfoChanged)
    def rightSensorFirmwareUpdateAvailable(self) -> bool:
        return self._firmware_update_available["right"]

    @pyqtProperty(str, notify=firmwareUpdateInfoChanged)
    def consoleFirmwareLatest(self) -> str:
        return self._firmware_latest["console"]

    @pyqtProperty(str, notify=firmwareUpdateInfoChanged)
    def leftSensorFirmwareLatest(self) -> str:
        return self._firmware_latest["left"]

    @pyqtProperty(str, notify=firmwareUpdateInfoChanged)
    def rightSensorFirmwareLatest(self) -> str:
        return self._firmware_latest["right"]

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
        return (not self._app_config.get("clinicalMode", False)
                and self._app_config.get("engineeringMode", False)
                and self._app_config.get("downloadBetaUpdates", False))

    def _maybe_check_firmware_update(self, name: str) -> None:
        """In a Research build, ensure this device's firmware-update
        availability is computed — reusing a cached 'latest' for the kind, or
        kicking one background GitHub check per kind per session. Clinical
        builds never check, even with engineering mode unlocked (#386)."""
        if self._app_config.get("clinicalMode", False):
            return   # firmware updates are disabled in clinical builds (#386)
        if name not in self._firmware_versions or not self._firmware_versions[name]:
            return
        kind = self._kind_for_device(name)
        if self._firmware_latest_by_kind.get(kind.value):
            self._recompute_firmware_update(name)   # latest already known; no network
            return
        with self._firmware_check_lock:
            if kind in self._firmware_checking_kinds:
                return                              # a check is already in flight
            self._firmware_checking_kinds.add(kind)
        threading.Thread(
            target=self._firmware_check_worker,
            args=(kind, self._firmware_check_generation),
            daemon=True,
        ).start()

    def _firmware_check_worker(self, kind: FirmwareKind, generation: int | None = None) -> None:
        try:
            beta = self._beta_enabled()
            info = check_latest(kind, include_prerelease=beta)
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
        if not self._firmware_versions.get(name):
            return  # device not connected / version unknown — nothing to recompute
        kind = self._kind_for_device(name)
        installed = self._firmware_versions.get(name, "")
        latest = self._firmware_latest_by_kind.get(kind.value, "")
        beta = self._beta_enabled()
        avail = (bool(installed) and bool(latest)
                 and is_update_available(installed, latest, prerelease=beta))
        self._firmware_latest[name] = latest
        self._firmware_update_available[name] = avail
        self.firmwareUpdateInfoChanged.emit()
        if avail:
            logger.info("Firmware update available for %s: %s -> %s",
                        name, installed, latest)
            self.firmwareUpdateAvailable.emit(name, installed, latest)

    def _refresh_firmware_update_check(self) -> None:
        """Invalidate the firmware-latest caches and re-run detection for every
        connected device — called when downloadBetaUpdates toggles so the
        banner/Settings card reflect the new release pool immediately."""
        self._firmware_latest_by_kind.clear()
        self._firmware_checking_kinds.clear()
        self._firmware_check_generation += 1
        for name in ("console", "left", "right"):
            self._firmware_latest[name] = ""
            self._firmware_update_available[name] = False
        self.firmwareUpdateInfoChanged.emit()
        for name in ("console", "left", "right"):
            if self._firmware_versions.get(name):
                self._maybe_check_firmware_update(name)

    def _refresh_update_checks(self) -> None:
        """Re-evaluate BOTH updaters after a change to the beta channel or
        engineering mode. Firmware re-detects for connected devices; the app
        self-updater re-checks — but only in a Research build, so a Clinical
        build makes no outbound GitHub call (#96, #386). Also withdraws a stale
        beta offer: the re-check runs on the now-current (post-change) channel,
        and both banners drop the old offer if it no longer qualifies."""
        self._refresh_firmware_update_check()
        if not self._app_config.get("clinicalMode", False):
            # Withdraw any currently-shown app-update offer synchronously (it may
            # be a now-ineligible beta), mirroring the firmware side; the
            # re-check re-offers only if a valid update still exists, so a failed
            # re-check leaves the stale offer withdrawn rather than clickable.
            self.updateNotAvailable.emit()
            self.checkForUpdates()

    @pyqtSlot(str, result=bool)
    def startFirmwareUpdate(self, device_key: str) -> bool:
        """Download the latest firmware for device_key and flash it over DFU.
        Research-only (blocked in clinical builds); refused during a scan or
        while another update runs (#386)."""
        if device_key not in ("console", "left", "right"):
            return False
        if self._app_config.get("clinicalMode", False):
            return False   # firmware updates are disabled in clinical builds (#386)
        if self._state == RUNNING or self._running:
            self.firmwareUpdateFinished.emit(
                device_key, False, "Cannot update firmware during a scan.")
            return False
        if self._firmware_update_in_progress is not None:
            self.firmwareUpdateFinished.emit(
                device_key, False, "Another firmware update is in progress.")
            return False
        if not self._firmware_update_available.get(device_key, False):
            self.firmwareUpdateFinished.emit(
                device_key, False, "No update available for this device.")
            return False
        self._firmware_update_in_progress = device_key
        threading.Thread(
            target=self._firmware_update_worker, args=(device_key,), daemon=True
        ).start()
        return True

    def _firmware_update_worker(self, device_key: str) -> None:
        import tempfile
        from omotion.firmware_update import FirmwareUpdater, download_firmware

        ok, msg = False, ""
        try:
            kind = self._kind_for_device(device_key)
            self.firmwareUpdateProgress.emit(
                device_key, "check", -1, "Checking latest release…")
            beta = self._beta_enabled()
            info = check_latest(kind, include_prerelease=beta)
            if info is None:
                raise RuntimeError("Could not reach GitHub to fetch firmware.")
            self.firmwareUpdateProgress.emit(
                device_key, "download", -1, f"Downloading {info.tag}…")
            dest = Path(tempfile.gettempdir()) / "om-firmware"
            bin_path = download_firmware(info, dest)

            handle = getattr(self._interface, device_key, None)
            if handle is None:
                raise RuntimeError("Device handle unavailable.")

            def _prog(p) -> None:
                pct = p.percent if p.percent is not None else -1
                # Emit a clean per-phase label, never the SDK's per-step text:
                # it changes on every erase step and jitters in the UI.
                # update() blocks through the last two phases until the device
                # is back on USB; "power_cycle" waits for the user to do it (#739).
                label = {
                    "erase": "Erasing",
                    "download": "Writing",
                    "verify": "Verifying",
                    "wait": "Waiting for the device to restart…",
                    "power_cycle": "Firmware written. Power-cycle the device "
                                   "now; the update finishes when it reconnects.",
                }.get(p.phase, str(p.phase).capitalize())
                self.firmwareUpdateProgress.emit(device_key, p.phase, pct, label)

            self.firmwareUpdateProgress.emit(
                device_key, "flash", -1, "Entering DFU and flashing…")
            result = FirmwareUpdater().update(handle, bin_path, progress_cb=_prog)
            ok = bool(result.success)
            # needs_power_cycle: the image is written but the device did not
            # come back within the SDK's wait, so it still has to be
            # power-cycled before it runs the new firmware.
            if not ok:
                msg = "The firmware update reported a failure."
            elif result.needs_power_cycle:
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
            self._firmware_latest_by_kind.pop(
                self._kind_for_device(device_key).value, None)
            self.firmwareUpdateFinished.emit(device_key, ok, msg)

    # ── App update checking ─────────────────────────────────────────────
    # The implementation lives in app_updater.py, which clinical builds do
    # not ship (#543). These slots and the update signals keep the QML
    # contract identical in every variant; without the module they are
    # no-ops, and the runtime clinicalMode guard still applies first.

    @pyqtSlot()
    def checkForUpdates(self):
        """Check GitHub releases for a newer version (runs in a background thread)."""
        t = threading.Thread(target=self._check_for_updates_worker, daemon=True)
        t.start()

    def _check_for_updates_worker(self):
        if self._app_config.get("clinicalMode", False):
            return   # clinical builds never check for app updates (#96, #386)
        if app_updater is None:
            logger.info("App updater is not part of this build; update check skipped")
            return
        app_updater.check_for_updates(self)

    @pyqtSlot(str)
    def applyUpdate(self, download_url: str):
        """Download the update bundle, verify it, and launch the upgrade.

        Guarded against re-entry so repeated clicks can't spawn racing
        download threads against the same file.
        """
        if self._app_config.get("clinicalMode", False):
            return   # clinical builds never install app updates (#96, #386)
        if getattr(self, "_update_in_progress", False):
            logger.info("Update already in progress; ignoring repeat request")
            return
        self._update_in_progress = True
        t = threading.Thread(
            target=self._apply_update_worker, args=(download_url,), daemon=True
        )
        t.start()

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
