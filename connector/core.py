"""MotionConnector: the single QObject QML sees as ``MotionInterface``.

Each concern lives in its own mixin module; this class only composes them,
builds the shared state and wires the cross-thread signals."""
from PyQt6.QtCore import QObject, pyqtSlot

from omotion import MotionInterface

from connector._common import logger
from connector.alt_settings import AltSettingsMixin
from connector.calibration import CalibrationMixin
from connector.console import ConsoleMixin
from connector.contact_quality import ContactQualityMixin
from connector.devices import DevicesMixin
from connector.history import HistoryMixin
from connector.scan import ScanMixin
from connector.settings import SettingsMixin
from connector.support import SupportMixin
from connector.updates import UpdatesMixin
from services.audit_log import gather_host_info


class MotionConnector(
    SettingsMixin, SupportMixin, DevicesMixin, UpdatesMixin, ConsoleMixin,
    ScanMixin, ContactQualityMixin, CalibrationMixin, HistoryMixin,
    AltSettingsMixin, QObject,
):
    def __init__(self, interface: MotionInterface, app_config=None,
                 baseline_config=None, settings_store=None, data_dir=None,
                 parent=None, app_version="", log_path=""):
        super().__init__(parent)
        cfg = app_config or {}
        self._interface = interface
        self._scan_workflow = interface.scan_workflow

        self._setup_settings(cfg, baseline_config, settings_store, data_dir)
        self._setup_support(cfg, app_version, log_path)
        self._setup_devices(cfg)
        self._setup_updates()
        self._setup_console(cfg)
        self._setup_scan(cfg)
        self._setup_contact_quality()
        self._setup_calibration(cfg)
        self._setup_history()
        self.connect_signals()

        try:
            sdk_version = interface.get_sdk_version()
            sdk_version = sdk_version if isinstance(sdk_version, str) else ""
        except Exception:
            sdk_version = ""
        self._audit.log("system_startup", {
            "app_version": self._app_version,
            "sdk_version": sdk_version,
            "data_dir": self._directory,
        })
        self._audit.log("system_info", gather_host_info())

        # Every real connection arrives via _on_handle_state_changed: main.py
        # builds the connector (which subscribes in connect_signals) before
        # it calls MotionInterface.start().
        interface.console.telemetry.add_listener(self._on_telemetry_update)
        # Startup connection watchdog (E-104/E-106). main.py starts device
        # monitoring with wait=False (issue #223), so already-attached
        # devices enumerate concurrently with this timer; the default 12 s
        # leaves ~7 s of headroom over a normal ~5 s console handshake and
        # also gates the research-build sample-dataset offer.
        self._arm_connection_watchdog()


    @pyqtSlot()
    def shutdown(self):
        """Shutdown connector. Stops any capture in flight; main.py's
        handle_exit stops the MotionInterface (monitoring + device
        teardown) right after this returns."""
        logger.info("Shutting down MotionConnector...")
        self.stopCapture()
        try:
            self._audit.log("system_shutdown", {"clean": True})
            self._audit.close()
        except Exception:
            logger.warning("audit shutdown log failed", exc_info=True)
        logger.info("MotionConnector shutdown complete.")

    def connect_signals(self):
        """Subscribe to per-handle state changes on the SDK interface."""
        for handle in (
            self._interface.console,
            self._interface.left,
            self._interface.right,
        ):
            handle.signal_state_changed.connect(self._on_handle_state_changed)
        self.safetyTripDuringCaptureRequested.connect(
            self._on_safety_trip_during_capture
        )
        # Worker → Qt main thread for an async scan-worker abort (#213).
        # Default (auto) connection like safetyTripDuringCaptureRequested above:
        # resolves to a queued connection for the cross-thread emit, so the
        # slot's QML-touching teardown runs on the main thread.
        self.scanWorkerFailedDuringCapture.connect(
            self._on_scan_worker_failed
        )
        # Worker → Qt main thread: start the console error-LED blink for a
        # critical error (issue #257). The QTimer must be driven from the
        # GUI thread that owns it.
        self._errorLedBlinkRequested.connect(self._start_error_led_blink)
        # Worker → Qt main thread for the calibration completion callback.
        self._calibrationCompleteSignal.connect(self._on_calibration_complete)
        self._testScanCompleteSignal.connect(self._on_test_scan_complete)
        # Worker → Qt main thread for the CQ workflow result.
        self._cq_result_signal.connect(self._on_cq_result_ready)
        # Runner thread → Qt main thread for live contact-quality transitions.
        self._cqTransitionSignal.connect(self._on_cq_transition_main)
        # Worker → Qt main thread for async past-scan loads (issue #152).
        self._pastScanBuffersReady.connect(self._on_past_scan_buffers_ready)
        # Auto-queued: emitted on the pipeline worker thread, runs the toast
        # on the GUI thread (same marshalling pattern as _pastScanBuffersReady).
        self._scanOutcomeWarningSignal.connect(self._on_scan_outcome_warning)

    @property
    def interface(self):
        return self._interface
