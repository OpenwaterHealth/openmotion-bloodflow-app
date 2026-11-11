"""Pre-scan contact-quality check and the live mid-scan CQ adapter."""
import threading
import time

from PyQt6.QtCore import pyqtSignal, pyqtSlot
from connector._common import SIDES, logger

# Contact-quality defaults (overridable via app_config keys
# cq_dark_threshold_per_camera / cq_light_threshold_per_camera /
# cq_rolling_avg_window / cq_live_activate_frames / cq_live_clear_frames).
CQ_DEFAULT_DARK_THRESHOLD_DN = 3.0
CQ_DEFAULT_LIGHT_THRESHOLD_DN = 15.0
CQ_DEFAULT_ROLLING_WINDOW = 10
# Asymmetric live debounce (#364): RAISE fast, CLEAR slow.
CQ_DEFAULT_LIVE_ACTIVATE_FRAMES = 10
CQ_DEFAULT_LIVE_CLEAR_FRAMES = 80


class ContactQualityMixin:
    """Pre-scan contact-quality check and the live mid-scan CQ adapter."""

    def _setup_contact_quality(self) -> None:
        self._cq_quick_running = False
        self._cq_t0 = None  # check start (monotonic) for the log banner

    # Contact-quality quick-check signals.
    contactQualityCheckStarted = pyqtSignal(int)  # expected duration in seconds
    contactQualityCheckFinished = pyqtSignal(bool, str, 'QVariantList')  # ok, error, warnings
    contactQualityWarning = pyqtSignal(str, str, str, float)  # camera, typeKey, typeText, value
    # Mid-scan CQ state change (active=True on activation, False on clear);
    # BloodFlow.qml consumes the ``False`` edge to clear a live-modal entry.
    contactQualityIssueStateChanged = pyqtSignal(str, str, str, float, bool)
    contactQualityScanInProgress = pyqtSignal(bool)
    # Private worker→main marshalling (QML must never be touched from a
    # worker thread): live transitions from the SDK's ContactQualityMonitor
    # (runner thread), and the ContactQualityResult of the pre-scan check.
    _cqTransitionSignal = pyqtSignal(str, int, str, float, bool)
    _cq_result_signal = pyqtSignal(object)

    @staticmethod
    def _camera_label(side: str, cam_id: int) -> str:
        prefix = "L" if side == "left" else "R"
        return f"{prefix}{int(cam_id) + 1}"

    @staticmethod
    def _warning_text(type_key: str) -> str:
        return {
            "ambient_light": "Ambient light detected",
            "poor_contact": "Poor sensor contact",
        }.get(type_key, type_key)

    def _cq_thresholds(self) -> tuple[list, list]:
        """Configured per-camera (dark, light) thresholds in DN."""
        cfg = self._app_config or {}
        return (
            list(cfg.get("cq_dark_threshold_per_camera") or [CQ_DEFAULT_DARK_THRESHOLD_DN] * 8),
            list(cfg.get("cq_light_threshold_per_camera") or [CQ_DEFAULT_LIGHT_THRESHOLD_DN] * 8),
        )

    @pyqtSlot(int, int)
    def runContactQualityCheck(self, left_camera_mask: int, right_camera_mask: int):
        """Run the pre-scan contact-quality check (SDK ContactQualityWorkflow).

        ``check()`` runs a short scan and blocks, so it runs on a worker
        thread and its ContactQualityResult (per-camera statistics) comes
        back through ``_cq_result_signal``.

        The masks are the *configured scan* masks (bloodFlow.leftMask /
        rightMask, cameras set to "None" cleared): a camera excluded from
        the scan must not be required to pass contact quality. They are
        still AND-gated on the sensor being connected.
        """
        err = self._ensure_idle()
        if err is not None:
            self.contactQualityCheckFinished.emit(False, err, [])
            return

        cfg = self._app_config or {}
        duration_s = float(cfg.get("cq_check_duration_sec", 1.0))
        dark_thresholds, light_thresholds = self._cq_thresholds()
        rolling_window = int(cfg.get("cq_rolling_avg_window", CQ_DEFAULT_ROLLING_WINDOW))

        if not (self._leftSensorConnected or self._rightSensorConnected):
            self.contactQualityCheckFinished.emit(False, "No sensors connected", [])
            return

        self._cq_quick_running = True
        self.contactQualityScanInProgress.emit(True)
        self.contactQualityCheckStarted.emit(int(round(duration_s + 3)))

        left_mask = int(left_camera_mask) if self._leftSensorConnected else 0x00
        right_mask = int(right_camera_mask) if self._rightSensorConnected else 0x00

        # #510: the preflight must run under the same registers as the scan
        # it gates: apply (or restore) the Engineering-mode alternative
        # camera + laser settings first, synchronously on this thread before
        # the SDK worker spawns, exactly as startCapture does.
        self._sync_alt_scan_settings(left_mask, right_mask)

        self._cq_t0 = time.monotonic()
        logger.info(
            "=== Contact-quality check started: duration=%.1fs left=0x%02X right=0x%02X ===",
            duration_s, left_mask, right_mask,
        )

        def _worker():
            try:
                result = self._interface.contact_quality_workflow.check(
                    duration_sec=duration_s,
                    rolling_window=rolling_window,
                    dark_threshold_per_camera=dark_thresholds,
                    light_threshold_per_camera=light_thresholds,
                    left_camera_mask=left_mask,
                    right_camera_mask=right_mask,
                )
                self._cq_result_signal.emit(result)
            except Exception as exc:
                logger.exception("CQ workflow check raised: %s", exc)
                self._cq_result_signal.emit(None)

        threading.Thread(target=_worker, daemon=True, name="CQWorkflow-check").start()

    @pyqtSlot(object)
    def _on_cq_result_ready(self, result):
        """Main thread (queued via _cq_result_signal): turn a
        ContactQualityResult into the warning list ContactQualityModal
        expects and emit contactQualityCheckFinished."""
        self._cq_quick_running = False
        self.contactQualityScanInProgress.emit(False)

        elapsed_s = time.monotonic() - self._cq_t0 if self._cq_t0 is not None else 0.0
        if result is None:
            logger.info("=== Contact-quality check ended: failed after %.1fs ===", elapsed_s)
            self.contactQualityCheckFinished.emit(False, "CQ check failed", [])
            return
        logger.info("=== Contact-quality check ended: completed after %.1fs ===", elapsed_s)

        def dn(value) -> str:
            return f"{value:.2f}" if value == value else "n/a"  # NaN != NaN

        def threshold(values, cam_id, default):
            return values[cam_id] if cam_id < len(values) else default

        dark_thresholds, light_thresholds = self._cq_thresholds()
        warnings: list[dict] = []
        logger.info("CQ Final Compare (DN, pedestal-subtracted; rolling-avg light, max dark):")
        logger.info(
            "| Camera | LightAvg | DarkMax | DarkThr | LightThr | Reason         | Warnings |")
        logger.info(
            "|--------|----------|---------|---------|----------|----------------|----------|")
        for side in SIDES:
            for cam_id in range(8):
                cam_res = result.per_camera.get((side, cam_id))
                if cam_res is None:
                    continue  # camera not evaluated (outside mask)
                camera = self._camera_label(side, cam_id)
                # The modal knows two type keys; "no_signal" reads as poor contact.
                type_key, value = {
                    "ambient_light": ("ambient_light", cam_res.dark_max_dn),
                    "poor_contact": ("poor_contact", cam_res.light_avg_dn),
                    "no_signal": ("poor_contact", cam_res.light_avg_dn),
                }.get(cam_res.reason, ("", 0.0))
                if type_key:
                    warnings.append({
                        "camera": camera,
                        "typeKey": type_key,
                        "typeText": self._warning_text(type_key),
                        "value": float(value) if value == value else 0.0,
                    })
                logger.info(
                    "| %-6s | %8s | %7s | %7.2f | %8.2f | %-14s | %-8s |",
                    camera, dn(cam_res.light_avg_dn), dn(cam_res.dark_max_dn),
                    threshold(dark_thresholds, cam_id, CQ_DEFAULT_DARK_THRESHOLD_DN),
                    threshold(light_thresholds, cam_id, CQ_DEFAULT_LIGHT_THRESHOLD_DN),
                    cam_res.reason, type_key or "-",
                )

        ok = result.passed
        self.contactQualityCheckFinished.emit(
            ok, "" if ok else "Contact quality check failed", warnings)

    def _on_cq_transition(self, side, cam_id, reason, value, active):
        """Runner-thread callback from the SDK's ContactQualityMonitor: only
        hops to the main thread (cross-thread gotcha in CLAUDE.md).

        Never raises: an exception escaping a sink's consume() aborts that
        batch mid-flight and the loss is invisible, which is exactly the
        regression this feature exists to fix (#364).
        """
        try:
            self._cqTransitionSignal.emit(
                str(side), int(cam_id), str(reason), float(value), bool(active))
        except Exception as exc:
            logger.warning("contact-quality transition marshal failed: %s", exc)

    @pyqtSlot(str, int, str, float, bool)
    def _on_cq_transition_main(self, side, cam_id, reason, value, active):
        """Main thread: adapt an SDK CQ transition to the modal's signals.

        contactQualityWarning fires on activation only;
        contactQualityIssueStateChanged fires on every edge so QML can clear
        the entry.
        """
        label = self._camera_label(side, cam_id)
        # Defensive, not a live path today: the SDK's ContactQualityMonitor
        # skips non-finite readings before on_transition fires, so it never
        # emits "no_signal". Collapsing it to poor_contact keeps live and
        # preflight wording identical (see _on_cq_result_ready) if that changes.
        type_key = "poor_contact" if reason == "no_signal" else reason
        type_text = self._warning_text(type_key)
        try:
            if active:
                self.contactQualityWarning.emit(label, type_key, type_text, value)
            self.contactQualityIssueStateChanged.emit(label, type_key, type_text, value, active)
        except Exception as exc:
            logger.warning("contact-quality signal emit failed: %s", exc)
