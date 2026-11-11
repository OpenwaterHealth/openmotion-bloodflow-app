"""Toasts, the critical-error modal, audit-log slots, debug bundle, bug report, app log."""
import datetime
import itertools
import os
import time

from PyQt6.QtCore import pyqtSignal, pyqtSlot
from connector._common import logger, spawn
from services import bug_report, error_codes
from services.audit_log import EV_DEBUG_BUNDLE_CREATED, AuditLog, parse_date_bound
from utils import app_paths, log_tail
from connector.console import _RGB_LED_GREEN


class SupportMixin:
    """Toasts, the critical-error modal, audit-log slots, debug bundle, bug report, app log."""

    def _setup_support(self, cfg: dict, app_version: str, log_path: str) -> None:
        # Bug-report context (see sendBugReport): app_version + log_path come
        # from main.py, support_email / bug_report_smtp from app config.
        self._app_version = app_version
        self._log_path = log_path
        self._support_email = cfg.get("support_email", "support@openwater.health")
        self._bug_report_smtp = cfg.get("bug_report_smtp")
        # Incremental reader behind readAppLog(), created on the first poll.
        self._app_log_tail: log_tail.LogTail | None = None
        # No-op if there's no scan_db_path.
        self._audit = AuditLog(getattr(self._interface, "scan_db_path", None))
        if self._settings_store is not None and not self._settings_store.enabled:
            self._audit.log("settings_store_unavailable", {"path": self._settings_store.path})
        # notify() is reachable from several worker threads, and
        # itertools.count is one atomic step under the GIL where a
        # read-modify-write `+= 1` could hand out duplicate ids.
        self._notification_ids = itertools.count(1)
        # Critical-error modal coordination (#387): while a critical modal is
        # on screen the end-of-scan notes modal must not pop under it (it
        # opens seconds after an abort and would steal keyboard focus).
        # _raise_critical sets the first flag, _emit_or_defer_scan_notes sets
        # the second instead of opening, criticalErrorsDismissed flushes it.
        self._critical_modal_active = False
        self._scan_notes_pending = False

    errorOccurred = pyqtSignal(str)
    # A showstopper, shown as a blocking, dismissible modal with a stable
    # error code (see error_codes.py):
    # (code, title, message, suggestedAction, detail).
    criticalErrorRaised = pyqtSignal(str, str, str, str, str)
    notificationRequested = pyqtSignal('QVariant')  # toast notification payload dict
    notificationDismissByIdRequested = pyqtSignal(int)   # dismiss the toast with this id
    notificationDismissByTagRequested = pyqtSignal(str)  # dismiss the toast with this tag
    notificationDismissAllRequested = pyqtSignal()       # dismiss every active toast

    # ── Audit log (QML-facing) ───────────────────────────────────────────
    def _audit_rows(self, slot: str, read) -> list:
        """One audit-log read for a QML slot: ``read()``, or [] (logged) if it raises."""
        try:
            return read()
        except Exception:
            logger.warning("%s failed", slot, exc_info=True)
            return []

    @pyqtSlot(result="QVariantList")
    @pyqtSlot(int, result="QVariantList")
    def auditLogEntries(self, limit: int = 500):
        """Audit-log rows (newest first) for the Logs modal. Pure read: it
        does not itself log, so refreshing never double-logs."""
        return self._audit_rows("auditLogEntries", lambda: self._audit.query(int(limit)))

    @pyqtSlot("QVariantMap", result="QVariantList")
    def filteredAuditLogEntries(self, filters):
        """Audit-log rows (newest first) matching the Logs modal's filter
        bar (#226). ``filters`` keys, all optional: ``eventType`` (exact),
        ``dateFrom`` / ``dateTo`` (``YYYY-MM-DD``, local, inclusive whole
        days), ``text`` (case-insensitive substring across event type +
        details), ``limit`` (default 500). Empty/invalid values are ignored.
        Pure read, like auditLogEntries."""
        def read():
            f = dict(filters or {})
            return self._audit.query(
                limit=int(f.get("limit") or 500),
                event_type=str(f.get("eventType") or "") or None,
                since_epoch=parse_date_bound(f.get("dateFrom")),
                until_epoch=parse_date_bound(f.get("dateTo"), end=True),
                contains=str(f.get("text") or "") or None,
            )
        return self._audit_rows("filteredAuditLogEntries", read)

    @pyqtSlot(result="QVariantList")
    def auditEventTypes(self):
        """Distinct event types in the audit log (sorted), for the Logs
        modal's filter dropdown (#226)."""
        return self._audit_rows("auditEventTypes", self._audit.distinct_event_types)

    @pyqtSlot()
    def recordAuditLogViewed(self):
        """Record that the audit log was opened (once, from LogsModal.open())."""
        try:
            n = self._audit.count()
        except Exception:
            n = 0
        self._audit.log("audit_log_viewed", {"entry_count": n})

    @pyqtSlot(str, result=str)
    def exportAuditLogCsv(self, dest_path: str) -> str:
        """Export the full audit log to CSV (plain path or file:// URL) and
        record ``audit_log_exported``. Returns the written path, or '' on
        failure."""
        if not dest_path:
            return ""
        path = dest_path.replace("file:///", "").replace("file://", "")
        try:
            n = self._audit.export_csv(path)
            self._audit.log("audit_log_exported", {"dest": path, "row_count": n})
            logger.info("exportAuditLogCsv: wrote %d rows -> %s", n, path)
            return path
        except Exception:
            logger.exception("exportAuditLogCsv failed for %r", path)
            self.errorOccurred.emit("Audit log export failed.")
            return ""

    # ── Debug bundle ─────────────────────────────────────────────────────
    def _sdk_version(self) -> str:
        """The SDK version string, or '' when it can't be read."""
        try:
            version = self._interface.get_sdk_version()
        except Exception:
            return ""
        return version if isinstance(version, str) else ""

    @pyqtSlot()
    def prepareDebugLogBundle(self) -> None:
        """Zip the last 48h of app logs (+ config + system info) into
        data/debug-bundles/, reveal it in the file explorer, and toast the
        support address. Runs on a worker thread (zipping is multi-second
        and used to freeze the GUI); notify/errorOccurred emit queued
        signals, so that is safe. The immediate toast is replaced by the
        success toast (same tag)."""
        self.notify("Preparing debug logs…", type_="info", tag="debug-bundle")
        spawn(self._prepare_debug_bundle_sync, name="debug-bundle")

    def _prepare_debug_bundle_sync(self) -> str:
        """Build + reveal + audit + toast the debug bundle. Returns the zip
        path, or '' on failure. Unit tests call it synchronously."""
        try:
            from services.debug_bundle import build_debug_bundle, WINDOW_HOURS
            meta = build_debug_bundle(
                self._directory, os.path.join(self._data_root, "debug-bundles"), time.time(),
                config_json=self._effective_config_json(),
                extra_info={"app_version": self._app_version,
                            "sdk_version": self._sdk_version()},
            )
        except Exception:
            logger.exception("prepareDebugLogBundle: failed to build bundle")
            self.errorOccurred.emit("Could not create the debug log bundle.")
            self.dismissNotification("debug-bundle")
            return ""

        path = meta["path"]
        self._reveal_in_explorer(path)
        self._audit.log(EV_DEBUG_BUNDLE_CREATED, {
            "dest": path, "file_count": meta["file_count"], "log_count": meta["log_count"],
            "bytes": meta["bytes"], "window_hours": WINDOW_HOURS,
        })
        self.notify(
            "Debug logs saved to " + path + ". Please email this file to support@openwater.cc.",
            type_="success", duration_ms=0, dismissible=True, tag="debug-bundle",
        )
        return path

    def _reveal_in_explorer(self, path: str) -> None:
        """Open the OS file browser with the file selected. Never raises."""
        bug_report.reveal_in_file_manager(path)

    # ── Engineering log viewer (LogViewerWindow.qml) ─────────────────────
    @pyqtSlot(result=str)
    def appLogPath(self) -> str:
        """Absolute path of this launch's app log file, or ''. Prefers the
        path main.py handed in; falls back to the newest
        logs/open-motion-*.log under the data directory (tests build the
        connector without one)."""
        if self._log_path and os.path.isfile(self._log_path):
            return self._log_path
        return log_tail.latest_log_path(os.path.join(self._directory, app_paths.LOGS_DIRNAME))

    @pyqtSlot(result=str)
    def readAppLog(self) -> str:
        """New app-log text since the last call (see utils/log_tail.py); the
        first call returns the tail of the file, '' means nothing new.

        Bounded, shared-read, opens the file per call, so it is safe for the
        Logs window's GUI-thread QTimer poll. Re-resolves the path each call
        so the viewer recovers if the log only appears later.
        """
        path = self.appLogPath()
        if not path:
            return ""
        if self._app_log_tail is None or self._app_log_tail.path != path:
            self._app_log_tail = log_tail.LogTail(path)
        return self._app_log_tail.read_new()

    # ── Toasts ───────────────────────────────────────────────────────────
    @pyqtSlot(str, result=int)
    @pyqtSlot(str, str, result=int)
    @pyqtSlot(str, str, int, result=int)
    @pyqtSlot(str, str, int, bool, result=int)
    @pyqtSlot(str, str, int, bool, str, result=int)
    def notify(self, text: str, type_: str = "info", duration_ms: int = 4000,
               dismissible: bool = True, tag: str = "") -> int:
        """Fire a toast; callable from QML and from any Python thread.

        ``type_`` is "info", "success", "warning" or "error". ``duration_ms``
        0 makes it sticky until dismissed. A non-empty ``tag`` replaces the
        existing toast with the same tag (no stacking) and can be passed to
        dismissNotification, as can the returned id.
        """
        if type_ not in ("info", "success", "warning", "error"):
            logger.warning(f"notify: unknown type '{type_}', falling back to 'info'")
            type_ = "info"
        nid = next(self._notification_ids)
        # Every toast leaves a log line: field debug bundles must let us
        # reconstruct the notification stack (#489).
        logger.info("Toast #%d [%s] tag=%r duration_ms=%d: %s",
                    nid, type_, tag, int(duration_ms), text)
        self.notificationRequested.emit({
            "id": nid, "tag": str(tag), "text": text, "type": type_,
            "durationMs": int(duration_ms), "dismissible": bool(dismissible),
        })
        return nid

    @pyqtSlot(int)
    @pyqtSlot(str)
    def dismissNotification(self, value):
        """Dismiss one notification by id (int) or tag (str). Animated; a
        no-op for an id/tag that no longer exists."""
        if isinstance(value, bool):   # bool is an int subclass; not a valid id
            logger.warning("dismissNotification: bool is not a valid id/tag")
        elif isinstance(value, int):
            logger.info("Toast dismiss requested: #%d", value)
            self.notificationDismissByIdRequested.emit(value)
        else:
            logger.info("Toast dismiss requested: tag=%r", str(value))
            self.notificationDismissByTagRequested.emit(str(value))

    @pyqtSlot()
    def dismissAllNotifications(self):
        """Dismiss every active toast. Animated."""
        logger.info("Toast dismiss-all requested")
        self.notificationDismissAllRequested.emit()

    @pyqtSlot(result=str)
    def get_sdk_version(self):
        """The SDK version string (QML-facing)."""
        return self._interface.get_sdk_version()

    @pyqtSlot(str)
    def copyToClipboard(self, text: str) -> None:
        """Push a string to the system clipboard (the Test Results window's
        Copy button), so QML needs no dependency on PyQt6.QtGui."""
        from PyQt6.QtGui import QGuiApplication
        cb = QGuiApplication.clipboard()
        if cb is not None:
            cb.setText(text)

    # ── Critical errors ──────────────────────────────────────────────────
    def raise_startup_error(self, code: str, detail: str = "") -> None:
        """Raise the critical modal for a condition main.py found before
        the connector existed, e.g. a plaintext scans.db on a clinical
        build (E-108, #683). Called once the QML window has loaded."""
        self._raise_critical(code, detail=detail)

    def _raise_critical(self, code: str, detail: str = "") -> None:
        """Surface a showstopper through the critical-error modal: look
        ``code`` up in :mod:`error_codes`, log it, emit
        ``criticalErrorRaised``, and start the console error-LED blink
        (#257). Safe from worker threads (QML connects queued). Never raises.
        """
        try:
            err = error_codes.lookup(code)
            logger.error("CRITICAL %s — %s%s", code, err.title, f" ({detail})" if detail else "")
            self.criticalErrorRaised.emit(
                code, err.title, err.message, err.suggested_action, detail)
            self._critical_modal_active = True   # holds back the notes modal (#387)
            self._errorLedBlinkRequested.emit()
        except Exception:
            logger.exception("Failed to raise critical error %s", code)

    @pyqtSlot()
    def criticalErrorsDismissed(self):
        """Called from CriticalErrorModal when the last queued error is
        dismissed: release the notes modal deferred behind it (#387), then
        stop the error blink and restore the idle green LED (#257)."""
        # The notes flush must come before the LED early-return, which fires
        # when no blink was ever started (e.g. the console itself was the
        # disconnected device).
        self._critical_modal_active = False
        if self._scan_notes_pending:
            self._scan_notes_pending = False
            self.scanNotesReady.emit()
        if not self._error_led_timer.isActive() and not self._error_led_on:
            return
        self._error_led_timer.stop()
        self._error_led_on = False
        self._set_console_led(_RGB_LED_GREEN)

    # ── Bug report ───────────────────────────────────────────────────────
    @pyqtSlot(str)
    def sendBugReport(self, code: str) -> None:
        """Send the session log + error context to Openwater support: by
        SMTP on a background thread when a complete ``bug_report_smtp`` block
        is configured, else through the user's mail client with the log file
        revealed for manual attaching."""
        err = error_codes.lookup(code)
        subject = f"Open-Motion Bug Report — {code} {err.title}"
        body = bug_report.build_report_text(
            code=code, title=err.title, message=err.message,
            timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            app_version=self._app_version or "unknown",
            device_info=self._device_info_str(),
            log_path=self._log_path or "(no log file)",
        )
        if bug_report.smtp_config_complete(self._bug_report_smtp):
            self._send_bug_report_smtp(subject, body)
        else:
            self._send_bug_report_fallback(subject, body)

    def _send_bug_report_smtp(self, subject: str, body: str) -> None:
        """Send the report via SMTP on a daemon thread; toast the result."""
        def _worker():
            try:
                bug_report.send_via_smtp(
                    self._bug_report_smtp, to_addr=self._support_email,
                    subject=subject, body=body, log_path=self._log_path or None,
                )
                self.notify("Bug report sent to Openwater.", "success")
            except Exception as exc:
                logger.exception("SMTP bug report failed")
                self.notify(f"Could not send bug report: {exc}", "error")
                # Fall back so the user can still get the report out.
                self._send_bug_report_fallback(subject, body)
        spawn(_worker)

    def _send_bug_report_fallback(self, subject: str, body: str) -> None:
        """Open the mail client, copy the report, and reveal the log file."""
        import webbrowser
        self.copyToClipboard(body)
        if self._log_path:
            bug_report.reveal_in_file_manager(self._log_path)
        try:
            webbrowser.open(
                bug_report.build_mailto_url(self._support_email, subject=subject, body=body))
        except Exception:
            logger.exception("Could not open mail client for bug report")
        self.notify(
            "Bug report copied to clipboard. Attach the highlighted log "
            "file and send the email to Openwater.", "info", 8000, True)
