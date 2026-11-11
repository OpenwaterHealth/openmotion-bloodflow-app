"""Pre-scan contact-quality check and the live mid-scan CQ adapter."""
import threading
import time

from PyQt6.QtCore import pyqtSignal, pyqtSlot
from connector._common import logger

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
    # Mid-scan CQ state change (active=True on activation, False on clear).
    # BloodFlow.qml's ``onContactQualityIssueStateChanged`` consumes the
    # ``False`` edge to clear an entry from the live modal.
    contactQualityIssueStateChanged = pyqtSignal(str, str, str, float, bool)
    contactQualityScanInProgress = pyqtSignal(bool)
    # Runner thread → main thread marshal for live CQ transitions from the
    # SDK's ContactQualityMonitor. Same pattern as _cq_result_signal; QML
    # must never be touched from the pipeline worker thread.
    _cqTransitionSignal = pyqtSignal(str, int, str, float, bool)
    # Private signal used to marshal ContactQualityResult from the CQ worker
    # thread back to the main Qt thread (emitted by the _worker closure in
    # runContactQualityCheck, consumed by _on_cq_result_ready).
    _cq_result_signal = pyqtSignal(object)

    # ------------------------------------------------------------------
    # Contact-quality quick-check
    # ------------------------------------------------------------------
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

    # ──────────────────────────────────────────────────────────────────
    @pyqtSlot(int, int)
    def runContactQualityCheck(self, left_camera_mask: int, right_camera_mask: int):
        """Run the contact-quality check via the SDK's ContactQualityWorkflow.

        Delegates to interface.contact_quality_workflow.check(), which runs
        a short scan internally and returns a ContactQualityResult with
        per-camera BFI statistics. The SDK call is synchronous/blocking, so
        we run it in a background thread and marshal results back via a
        private signal.

        left_camera_mask / right_camera_mask: the *configured scan* mask
        (bloodFlow.leftMask/rightMask — cameras the user actually selected,
        with any camera set to "None" cleared). Only these cameras are
        evaluated; a camera excluded from the scan must not be required to
        pass contact quality. Still AND-gated on sensor-connected state
        below, matching the pre-existing safety check.
        """
        err = self._ensure_idle()
        if err is not None:
            self.contactQualityCheckFinished.emit(False, err, [])
            return

        cfg = self._app_config or {}
        duration_s = float(cfg.get("cq_check_duration_sec", 1.0))
        dark_thresholds = list(
            cfg.get("cq_dark_threshold_per_camera") or [CQ_DEFAULT_DARK_THRESHOLD_DN] * 8
        )
        light_thresholds = list(
            cfg.get("cq_light_threshold_per_camera") or [CQ_DEFAULT_LIGHT_THRESHOLD_DN] * 8
        )
        rolling_window = int(cfg.get("cq_rolling_avg_window", CQ_DEFAULT_ROLLING_WINDOW))

        if not (self._leftSensorConnected or self._rightSensorConnected):
            self.contactQualityCheckFinished.emit(False, "No sensors connected", [])
            return

        self._cq_quick_running = True
        self.contactQualityScanInProgress.emit(True)
        self.contactQualityCheckStarted.emit(int(round(duration_s + 3)))

        left_mask = int(left_camera_mask) if self._leftSensorConnected else 0x00
        right_mask = int(right_camera_mask) if self._rightSensorConnected else 0x00

        # #510: the preflight must run under the same registers the scan it
        # gates will use — apply (or restore) the Engineering-mode
        # alternative camera + laser settings before the check scan, exactly
        # as startCapture does. Synchronous on this thread, before the SDK
        # worker spawns, mirroring the scan-start call.
        self._sync_alt_scan_settings(left_mask, right_mask)

        self._cq_t0 = time.monotonic()
        logger.info(
            "=== Contact-quality check started: duration=%.1fs "
            "left=0x%02X right=0x%02X ===",
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

        t = threading.Thread(target=_worker, daemon=True, name="CQWorkflow-check")
        t.start()

    @pyqtSlot(object)
    def _on_cq_result_ready(self, result):
        """Main-thread slot: convert ContactQualityResult → UI warning list and
        emit contactQualityCheckFinished.  Connected to _cq_result_signal in
        __init__ (via connect_signals).
        """
        cfg = self._app_config or {}
        dark_thresholds = list(
            cfg.get("cq_dark_threshold_per_camera") or [CQ_DEFAULT_DARK_THRESHOLD_DN] * 8
        )
        light_thresholds = list(
            cfg.get("cq_light_threshold_per_camera") or [CQ_DEFAULT_LIGHT_THRESHOLD_DN] * 8
        )

        self._cq_quick_running = False
        self.contactQualityScanInProgress.emit(False)

        elapsed_s = (
            time.monotonic() - self._cq_t0 if self._cq_t0 is not None else 0.0
        )
        if result is None:
            logger.info(
                "=== Contact-quality check ended: failed after %.1fs ===",
                elapsed_s,
            )
            self.contactQualityCheckFinished.emit(False, "CQ check failed", [])
            return
        logger.info(
            "=== Contact-quality check ended: completed after %.1fs ===",
            elapsed_s,
        )

        # Convert CamCQResult.reason → (typeKey, warning dict) that the QML
        # ContactQualityModal expects.
        warnings_by_key: dict[tuple[str, str], dict] = {}
        table_rows: list[dict] = []

        # Iterate over active cameras in mask order for consistent logging.
        for side_idx, (side, mask) in enumerate((("left", 0xFF), ("right", 0xFF))):
            for cam_id in range(8):
                cam_res = result.per_camera.get((side, cam_id))
                if cam_res is None:
                    continue  # camera not evaluated (outside mask)
                camera = self._camera_label(side, cam_id)
                dark_threshold = (
                    dark_thresholds[cam_id] if cam_id < len(dark_thresholds)
                    else CQ_DEFAULT_DARK_THRESHOLD_DN
                )
                light_threshold = (
                    light_thresholds[cam_id] if cam_id < len(light_thresholds)
                    else CQ_DEFAULT_LIGHT_THRESHOLD_DN
                )
                light_avg_dn = cam_res.light_avg_dn
                dark_max_dn = cam_res.dark_max_dn
                reason = cam_res.reason
                warn_tags = []

                if reason == "ambient_light":
                    warnings_by_key[(camera, "ambient_light")] = {
                        "camera": camera,
                        "typeKey": "ambient_light",
                        "typeText": self._warning_text("ambient_light"),
                        "value": float(dark_max_dn) if dark_max_dn == dark_max_dn else 0.0,
                    }
                    warn_tags.append("ambient_light")
                elif reason in ("poor_contact", "no_signal"):
                    warnings_by_key[(camera, "poor_contact")] = {
                        "camera": camera,
                        "typeKey": "poor_contact",
                        "typeText": self._warning_text("poor_contact"),
                        "value": float(light_avg_dn) if light_avg_dn == light_avg_dn else 0.0,
                    }
                    warn_tags.append("poor_contact")

                table_rows.append({
                    "camera": camera,
                    "light_avg_dn": f"{light_avg_dn:.2f}" if light_avg_dn == light_avg_dn else "n/a",
                    "dark_max_dn":  f"{dark_max_dn:.2f}"  if dark_max_dn  == dark_max_dn  else "n/a",
                    "dark_threshold": dark_threshold,
                    "light_threshold": light_threshold,
                    "reason": reason,
                    "warnings": ",".join(warn_tags) if warn_tags else "-",
                })

        logger.info("CQ Final Compare (DN, pedestal-subtracted; rolling-avg light, max dark):")
        logger.info(
            "| Camera | LightAvg | DarkMax | DarkThr | LightThr | Reason         | Warnings |"
        )
        logger.info(
            "|--------|----------|---------|---------|----------|----------------|----------|"
        )
        for row in table_rows:
            logger.info(
                "| %-6s | %8s | %7s | %7.2f | %8.2f | %-14s | %-8s |",
                row["camera"],
                row["light_avg_dn"],
                row["dark_max_dn"],
                row["dark_threshold"],
                row["light_threshold"],
                row["reason"],
                row["warnings"],
            )

        warning_list = list(warnings_by_key.values())
        ok = result.passed
        err_msg = "" if ok else "Contact quality check failed"
        self.contactQualityCheckFinished.emit(ok, err_msg, warning_list)

    def _on_cq_transition(self, side, cam_id, reason, value, active):
        """Runner-thread callback from the SDK's ContactQualityMonitor.

        Does nothing but hop to the main thread — see the cross-thread
        gotcha in CLAUDE.md. Never raises: an exception escaping a sink's
        consume() aborts that batch mid-flight and the loss is invisible,
        which is exactly the regression this feature exists to fix (#364).
        """
        try:
            self._cqTransitionSignal.emit(
                str(side), int(cam_id), str(reason), float(value), bool(active)
            )
        except Exception as exc:
            logger.warning("contact-quality transition marshal failed: %s", exc)

    @pyqtSlot(str, int, str, float, bool)
    def _on_cq_transition_main(self, side, cam_id, reason, value, active):
        """Main thread: adapt an SDK CQ transition to the modal's signals.

        Mirrors the deleted _on_cq_warning closure. contactQualityWarning
        fires on activation only; contactQualityIssueStateChanged fires on
        every edge so QML can clear the entry.
        """
        label = self._camera_label(side, cam_id)
        # The modal knows two type keys. Defensive, not a live path today:
        # the SDK's ContactQualityMonitor skips non-finite readings before
        # on_transition ever fires, so it does not currently emit
        # "no_signal". Collapsing it to poor_contact here costs nothing and
        # keeps live and preflight wording identical — matching
        # _on_cq_result_ready's preflight mapping — if that contract ever
        # changes.
        type_key = "poor_contact" if reason == "no_signal" else reason
        type_text = self._warning_text(type_key)
        try:
            if active:
                self.contactQualityWarning.emit(label, type_key, type_text, value)
            self.contactQualityIssueStateChanged.emit(
                label, type_key, type_text, value, active
            )
        except Exception as exc:
            logger.warning("contact-quality signal emit failed: %s", exc)
