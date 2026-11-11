"""Scan history: session list, replay sources, CSV export, delete, sample scan."""
import datetime
import json
import os
import re
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtProperty, pyqtSignal, pyqtSlot
from connector._common import DEVICES, logger, scan_db, spawn
from plot.data_sources import (
    LiveScanSource, PastScanSource, ScanDataSource, buffers_are_empty, load_csv_scan_buffers,
)
from utils.resource_path import resource_path

# Camera-mask -> human config name, mirroring ScanSettingsModal's pattern
# table. Unmapped masks render as hex; -1 (unknown, e.g. a clinical-mode
# scan whose meta lacks sdk_flags) renders as an em dash.
_CONFIG_NAMES = {
    0x00: "None", 0x5A: "Near", 0x66: "Middle", 0xC3: "Far",
    0x99: "Outer", 0x0F: "Left", 0xF0: "Right", 0x42: "Third Row",
    0xFF: "All",
}


def _config_name(mask) -> str:
    if mask is None or mask < 0:
        return "—"
    m = int(mask) & 0xFF
    return _CONFIG_NAMES.get(m, f"0x{m:02X}")


def _sdk_meta_dict(session) -> dict:
    """Decode a ScanDatabase session dict's ``session_meta`` into a dict.
    ScanDatabase already json-decodes it, but tolerate a raw JSON string too.
    Returns an empty dict when absent or unparseable so callers can merge
    into it safely."""
    if not isinstance(session, dict):
        return {}
    meta = session.get("session_meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            return {}
    return meta if isinstance(meta, dict) else {}


def _sdk_flags_from_session(session) -> Optional[dict]:
    """The ``sdk_flags`` block of a session's ``session_meta`` (the camera
    config / clinical-mode the scan was RUN with, written by the SDK's
    ScanDBSink), or None when absent or unparseable. A replayed scan lays its
    plot grid out from this rather than only the cameras that logged data
    (#175)."""
    flags = _sdk_meta_dict(session).get("sdk_flags")
    return flags if isinstance(flags, dict) else None


class HistoryMixin:
    """Scan history: session list, replay sources, CSV export, delete, sample scan."""

    def _setup_history(self) -> None:
        # Source bound to the PlotViewer; assigned at scan start by startCapture.
        self._current_scan_source: ScanDataSource | None = None
        # The most-recent LiveScanSource is kept alive even after the user
        # navigates to a past scan, so "Back to live" can reassign without
        # reconstructing. None when no scan has run this session.
        self._live_scan_source: LiveScanSource | None = None
        # Monotonic token for async past-scan loads: a result whose seq
        # doesn't match the latest request is stale and dropped.
        self._past_scan_load_seq = 0

    # Real-time plot viewer source — see data_sources.py.
    currentScanSourceChanged = pyqtSignal()
    # No device at the startup watchdog → offer the bundled sample scan.
    # Research builds only; see _maybe_offer_sample_scan.
    sampleScanOfferRequested = pyqtSignal()
    liveSourceAvailableChanged = pyqtSignal()
    # History → "View in plot": fires on the GUI thread when an async
    # loadPastScan completes. (session_label, ok). HistoryModal uses it
    # to clear its busy overlay and close only on success (issue #152).
    pastScanLoadFinished = pyqtSignal(str, bool)
    # History → "Export CSV" / "Export to folder": fire on the GUI thread
    # when the async export workers finish. (ok, path) for the single-scan
    # export; (exported, skipped, folder) for the batch.
    scanCsvExportFinished = pyqtSignal(bool, str)
    scansExportFinished = pyqtSignal(int, int, str)
    # Private worker→main marshalling for loadPastScan results:
    # (seq, session_label, session_id, buffers-or-None, source_tag,
    #  recorded_flags-or-None, display_meta-or-None). recorded_flags is the
    # scan's stored session_meta.sdk_flags so the PastScanSource lays its grid
    # out from the config the scan was RUN with, not just the cameras that
    # logged data (issue #175 reopen). display_meta is {userLabel, dateTime}
    # for the viewer's "Viewing" badge (issue #245), derived from the same
    # session row the History list shows.
    _pastScanBuffersReady = pyqtSignal(int, str, int, object, str, object, object)

    # --- sample scan ---

    def _maybe_offer_sample_scan(self) -> None:
        """Offer the bundled sample scan when the watchdog found nothing.

        Three gates, all required:
          * research build — a clinical user must never be shown fabricated
            traces, not even behind a prompt;
          * no device at all (`_any_device_connected`, not merely
            `console_missing`) — a console-less rig with a live sensor is a
            real session, not a demo;
          * nothing already bound to the viewer.

        Emits only: nothing loads until the user accepts the dialog, which
        calls `loadSampleScan`. One-shot, because the watchdog is.

        Every exit logs its reason, so "correctly suppressed" can be told
        from "broken" after the fact. The sample file's availability is
        probed and logged HERE as well as in `_load_sample_scan`, because a
        user who declines the dialog never reaches the loader — otherwise a
        build shipping without the CSV would log like a healthy one.
        """
        if self._clinical:
            logger.info("[Plot] sample scan offer suppressed — clinical build")
            return
        if self._any_device_connected():
            logger.info(
                "[Plot] sample scan offer suppressed — a device is connected "
                "(console=%s left=%s right=%s)", *(self._sdk_connected(n) for n in DEVICES))
            return
        if self._current_scan_source is not None:
            logger.info("[Plot] sample scan offer suppressed — viewer already bound to %s",
                        type(self._current_scan_source).__name__)
            return
        self._log_sample_scan_availability()
        logger.info("[Plot] no device at watchdog — offering sample scan")
        self.sampleScanOfferRequested.emit()

    @staticmethod
    def _sample_scan_path() -> Path:
        """Resolved location of the bundled sample scan. `resource_path`
        returns its under-base candidate even when nothing exists there, so
        this is a *guess* until stat'd."""
        return Path(resource_path("resources", "sample_scan.csv"))

    def _log_sample_scan_availability(self) -> None:
        """Record whether the bundled sample scan is actually on disk.

        WARNING when it isn't: a missing sample in a research build is a
        packaging fault (openwater.spec drops the `datas` entry when the CSV
        is absent at build time), and the resolved absolute path is the only
        clue to where the frozen app searched."""
        path = self._sample_scan_path()
        try:
            size = path.stat().st_size
        except OSError as exc:
            logger.warning("[Plot] sample scan unavailable — cannot stat %s (%s)", path, exc)
            return
        logger.info("[Plot] sample scan available: %s (%d bytes)", path, size)

    @pyqtSlot()
    def loadSampleScan(self) -> None:
        """Load the bundled sample scan into the replay viewer (#314).

        Called from SampleScanOfferModal when the user accepts the offer the
        startup watchdog raised — never automatically, and never in a
        clinical build (the offer is not emitted there). The sample is never
        written anywhere, so real scan data is never polluted; once the user
        runs a scan, startCapture's fresh LiveScanSource retires it. Guarded
        so it can't clobber a source bound between the offer and the click.
        """
        if self._current_scan_source is not None:
            logger.info("[Plot] sample scan request ignored — viewer already bound to %s",
                        type(self._current_scan_source).__name__)
            return
        self._load_sample_scan(str(self._sample_scan_path()))

    def _load_sample_scan(self, csv_path: str) -> None:
        """Parse a scan-export CSV and bind it to the viewer as a DB-free
        PastScanSource. Fail-soft: never raises (a bad sample file must not
        break boot) and leaves the viewer untouched on failure. Split from
        the slot so unit tests can drive it with an arbitrary path.

        Every failure exit names the path and distinguishes WHY: file absent
        is a packaging fault, unreadable is a permissions/IO fault,
        parsed-but-empty is a bad dataset, and the user-visible symptom (an
        empty viewer) is the same for all three."""
        # Stat first: the CSV loader swallows OSError, which would otherwise
        # make a missing file read as a bad dataset.
        try:
            size = os.stat(csv_path).st_size
        except OSError as exc:
            logger.warning("[Plot] sample scan unavailable — cannot read %s (%s)", csv_path, exc)
            return
        if size == 0:
            logger.warning(
                "[Plot] sample scan unavailable — file is empty (0 bytes): %s", csv_path)
            return
        try:
            buffers = load_csv_scan_buffers(csv_path)
        except Exception:
            logger.warning("[Plot] sample scan load failed: %s", csv_path, exc_info=True)
            return
        if buffers_are_empty(buffers):
            logger.warning(
                "[Plot] sample scan unavailable — %s (%d bytes) parsed but "
                "held no usable samples; expected a per-cam scan-export CSV "
                "with bfi_l1..contrast_r8 columns", csv_path, size)
            return
        try:
            sample = PastScanSource(
                scan_db=None, session_id=-1, parent=self, preloaded_buffers=buffers,
                user_label="Sample scan", date_time="")
        except Exception:
            logger.warning("[Plot] sample scan source construction failed", exc_info=True)
            return
        self._set_current_scan_source(sample)
        logger.info("[Plot] loaded sample scan: buffers=%d samples=%d from %s",
                    len(sample.buffers), sum(b.n for b in sample.buffers.values()), csv_path)

    # --- session list / details / delete ---

    @staticmethod
    def _friendly_ts(ts: str, tz=None) -> str:
        """Convert a UTC scan timestamp to local time for display."""
        if not ts or len(ts) != 15:
            return ts or "-"
        try:
            utc_dt = datetime.datetime.strptime(ts, "%Y%m%d_%H%M%S")
        except ValueError:
            return ts
        local_dt = utc_dt.replace(tzinfo=datetime.timezone.utc).astimezone(tz)
        return local_dt.strftime("%Y-%m-%d %H:%M:%S")

    def _session_to_row(self, s: dict) -> dict:
        """Flatten one ScanDatabase session dict into the QVariantMap the
        History table consumes. Masks/operator come from session_meta
        (written by ScanDBSink); duration from session_start/end."""
        label = (s.get("session_label") or "").strip()
        meta = _sdk_meta_dict(s)
        flags = meta.get("sdk_flags") or {}
        left_mask = flags.get("left_camera_mask")
        right_mask = flags.get("right_camera_mask")
        left_mask = -1 if left_mask is None else left_mask
        right_mask = -1 if right_mask is None else right_mask

        user_label = meta.get("subject_id") or ""
        timestamp = ""
        m = re.match(r"^(\d{8}_\d{6})_?(.*)$", label)
        if m:
            timestamp = m.group(1)
            user_label = user_label or m.group(2)

        start = s.get("session_start")
        end = s.get("session_end")
        # Prefer the actual trigger-ON duration stamped by the completion
        # handler (#335). session_end - session_start brackets the whole
        # pipeline lifetime (pre-trigger setup + post-trigger USB drain), which
        # runs ~3 s longer than the laser-on window. Old rows lack the key
        # and fall back to wall-clock.
        try:
            duration = float(meta["actual_duration_sec"])
        except (KeyError, TypeError, ValueError):
            duration = float(end) - float(start) if None not in (start, end) else -1.0

        return {
            "sessionId": int(s.get("id")),
            "label": label,
            "userLabel": user_label,
            "operator": meta.get("operator") or "",
            "timestamp": timestamp or label,
            "dateTime": self._friendly_ts(timestamp),
            "durationSec": duration,
            "leftMask": int(left_mask),
            "rightMask": int(right_mask),
            "configL": _config_name(left_mask),
            "configR": _config_name(right_mask),
            "clinicalMode": bool(flags.get("reduced_mode", False)),
            "notes": s.get("session_notes") or "",
            "interrupted": end is None,
        }

    @pyqtSlot(result="QVariantList")
    def get_scan_sessions(self):
        """Return one row per scan-DB session, newest first, for the
        History table. Best-effort: a missing/unreadable DB yields []."""
        rows = []
        try:
            with scan_db(self._interface) as db:
                if db is None:
                    return []
                for s in db.iter_sessions():
                    # One malformed session must not blank the whole History
                    # view: skip it and keep the rest.
                    try:
                        rows.append(self._session_to_row(s))
                    except Exception:
                        logger.warning("get_scan_sessions: skipping malformed session %s",
                                       s.get("id"), exc_info=True)
        except Exception:
            logger.warning("get_scan_sessions: could not read scan DB", exc_info=True)
            return []
        rows.sort(key=lambda r: r["timestamp"], reverse=True)
        return rows

    @pyqtSlot(int, result="QVariantMap")
    def get_session_stats(self, session_id: int):
        """Lazily fetch heavier per-scan stats (row count) when a History
        row is focused, so the list query itself stays cheap."""
        try:
            with scan_db(self._interface) as db:
                if db is None:
                    return {"sampleCount": 0}
                # ScanDatabase exposes no public row-count API.
                row = db._connection().execute(
                    "SELECT COUNT(*) FROM session_data WHERE session_id = ?",
                    (int(session_id),)).fetchone()
                return {"sampleCount": int(row[0]) if row else 0}
        except Exception:
            logger.warning("get_session_stats failed for %s", session_id, exc_info=True)
            return {"sampleCount": 0}

    @pyqtSlot("QVariantList", result=int)
    def deleteScans(self, session_ids):
        """Delete the given scan-DB sessions (CASCADE removes their
        session_data). Returns the count actually deleted. No password in
        any build (#703): HistoryModal asks "are you sure" before calling
        this, and the delete is audited (scan_deleted)."""
        deleted = 0
        try:
            with scan_db(self._interface) as db:
                if db is None:
                    return 0
                for sid in session_ids:
                    try:
                        if db.delete_session(int(sid)):
                            deleted += 1
                            logger.info("deleteScans: removed session %s", sid)
                    except Exception:
                        logger.warning("deleteScans: failed to delete %s", sid, exc_info=True)
        except Exception:
            logger.warning("deleteScans: could not open scan DB", exc_info=True)
        # Tolerate non-int ids the same way the delete loop does: this runs
        # outside AuditLog.log's fail-soft wrapper.
        ids = []
        for s in session_ids:
            try:
                ids.append(int(s))
            except (TypeError, ValueError):
                pass
        self._audit.log("scan_deleted", {"session_ids": ids, "count": deleted})
        return deleted

    # --- viewer source bookkeeping ---

    @pyqtProperty(QObject, notify=currentScanSourceChanged)
    def currentScanSource(self) -> ScanDataSource | None:
        """Current ScanDataSource bound to the PlotViewer. Usually the
        live source during a scan; can be a PastScanSource while the
        user is reviewing history via loadPastScan."""
        return self._current_scan_source

    @pyqtProperty(bool, notify=liveSourceAvailableChanged)
    def liveSourceAvailable(self) -> bool:
        """True when a LiveScanSource is held (at least one scan has run this
        session); PlotToolbar's "Back to live" button depends on it."""
        return self._live_scan_source is not None

    def _retire_scan_source(self, source: ScanDataSource | None) -> None:
        """Tear down a source QML can no longer reach: stop its flush
        timer, close any scan-DB handle, and queue C++ deletion. Sources
        are parented to the connector, so without this every superseded
        source (one per scan / History view) stays alive for the app's
        lifetime — up to ~46 MB of buffers plus a running 10 Hz timer
        each. deleteLater (not a direct delete) so any in-flight QML
        paint against the old source finishes before the object dies."""
        if source is None:
            return
        try:
            source.release()
        except Exception:
            logger.warning("scan source release failed", exc_info=True)
        source.deleteLater()

    def _set_current_scan_source(self, source: ScanDataSource | None) -> None:
        """Replace the active source. Dedupes identical-instance assignments
        so the notify signal only fires on real transitions. The previous
        source is retired unless it is the held live source, which must
        survive History navigation for "Back to live"."""
        if source is self._current_scan_source:
            return
        prev = self._current_scan_source
        self._current_scan_source = source
        self.currentScanSourceChanged.emit()
        if prev is not None and prev is not self._live_scan_source:
            self._retire_scan_source(prev)

    @pyqtSlot()
    def showLiveSource(self) -> None:
        """Switch the viewer back to the held live source, if any.
        No-op when no LiveScanSource has been created this session."""
        if self._live_scan_source is None:
            return
        self._set_current_scan_source(self._live_scan_source)
        logger.info("[Plot] viewer switched back to live source")

    # --- past-scan replay ---

    @pyqtSlot(int, str)
    def loadPastScan(self, session_id: int, session_label: str) -> None:
        """Open the saved scan with the given unique DB session id and
        display it in the PlotViewer. The held live source is left intact so
        a subsequent showLiveSource() can return to it.

        session_label (YYYYMMDD_HHMMSS_userLabel) still names the scan's CSVs
        on disk and the log/signal payloads, but the DB session is resolved
        by id: a by-label lookup returns the newest session with that label,
        so duplicate labels loaded a different scan than the one selected
        (#254).

        Asynchronous (#152): the bulk SQLite walk / CSV parse is multi-second
        for long scans (a ~70-min scan measured ~8.3M samples ≈ 20 s), so it
        runs on a daemon worker; the finished buffers are marshalled back to
        the GUI thread via _pastScanBuffersReady, where the PastScanSource
        QObject is constructed and bound. pastScanLoadFinished(label, ok)
        fires on completion either way so the History modal can clear its
        busy state."""
        if session_id < 0 or not session_label:
            logger.warning(
                "loadPastScan: invalid session_id %r / label %r", session_id, session_label)
            self.pastScanLoadFinished.emit(session_label or "", False)
            return
        db_path = getattr(self._interface, "scan_db_path", None)
        if not db_path:
            logger.warning("loadPastScan: scan_db_path unavailable on interface")
            self.pastScanLoadFinished.emit(session_label, False)
            return
        self._audit.log("scan_viewed", {"label": session_label, "session_id": int(session_id)})
        self._past_scan_load_seq += 1
        seq = self._past_scan_load_seq
        spawn(self._load_past_scan_worker, seq, int(session_id), session_label, str(db_path),
              name=f"past-scan-load-{seq}")

    def _load_past_scan_worker(self, seq: int, session_id: int, session_label: str,
                               db_path: str) -> None:
        """Worker thread: bulk-load a past scan's samples (the heavy,
        Qt-free part of loadPastScan). Opens its own ScanDatabase handle;
        results cross back to the GUI thread via _pastScanBuffersReady
        (cross-thread signal → queued delivery)."""
        failure = (seq, session_label, -1, None, "", None, None)
        try:
            from omotion.ScanDatabase import ScanDatabase
            from plot.data_sources import load_past_scan_buffers
            with ScanDatabase(db_path) as db:
                session = db.get_session(session_id)
                if not session:
                    logger.warning("loadPastScan: no session found for id %d (label %r)",
                                   session_id, session_label)
                    self._pastScanBuffersReady.emit(*failure)
                    return
                recorded_flags = _sdk_flags_from_session(session)
                # The viewer's "Viewing" badge (#245) is derived from the SAME
                # row the History list renders, so it can't disagree with the
                # row the user clicked. Best-effort: a parse failure just hides
                # the badge and must never block the load.
                display_meta = None
                try:
                    row = self._session_to_row(session)
                    display_meta = {"userLabel": row["userLabel"], "dateTime": row["dateTime"]}
                except Exception:
                    logger.warning("loadPastScan: could not derive badge meta for %r",
                                   session_label, exc_info=True)
                buffers, _ = load_past_scan_buffers(db, session_id)
            # "db" when the session holds per-camera BFI, else only the
            # clinical side average (or nothing).
            per_cam = any(k[1] >= 0 and k[2] == "bfi" for k in buffers)
            source_tag = "db" if per_cam else "db-only-sentinel"
            self._pastScanBuffersReady.emit(
                seq, session_label, session_id, buffers, source_tag, recorded_flags, display_meta)
        except Exception:
            logger.exception("loadPastScan failed for label %r", session_label)
            self._pastScanBuffersReady.emit(*failure)

    def _on_past_scan_buffers_ready(self, seq: int, session_label: str, session_id: int, buffers,
                                    source_tag: str, recorded_flags=None,
                                    display_meta=None) -> None:
        """GUI thread: bind a worker-loaded past scan to the viewer.
        Results from a superseded request (user clicked another scan
        while this one was loading) are dropped."""
        if seq != self._past_scan_load_seq:
            logger.info("loadPastScan: dropping stale result for %r (seq %d, latest %d)",
                        session_label, seq, self._past_scan_load_seq)
            return
        error = None
        if buffers is None:
            error = (f"Could not load scan '{session_label}' for plotting.\n"
                     "See the app log for details.")
        elif buffers_are_empty(buffers):
            logger.info("loadPastScan: %r contains no samples — not displaying", session_label)
            error = (f"Scan '{session_label}' contains no data.\n"
                     "It was interrupted before any data could be recorded.")
        if error:
            self.errorOccurred.emit(error)
            self.pastScanLoadFinished.emit(session_label, False)
            return
        try:
            dm = display_meta or {}
            past = PastScanSource(
                scan_db=None, session_id=session_id, parent=self, preloaded_buffers=buffers,
                recorded_flags=recorded_flags, user_label=dm.get("userLabel", ""),
                date_time=dm.get("dateTime", ""))
            self._set_current_scan_source(past)

            # Grid masks are what the viewer lays out from; logged next to the
            # data shape so a config/data mismatch (#175) is diagnosable from
            # one line. flags=no is an older scan with no stored sdk_flags;
            # -1 prints as "unknown" (viewer falls back to the live selection).
            def _mstr(v):
                return f"0x{v & 0xFF:02X}" if v >= 0 else "unknown"
            logger.info(
                "[Plot] loaded past scan %r (session_id=%d) source=%s: "
                "buffers=%d samples=%d liveEdge=%.3f gridMasks=L%s/R%s "
                "clinical=%d flags=%s sample_keys=%s",
                session_label, session_id, source_tag, len(past.buffers),
                sum(b.n for b in past.buffers.values()), past.liveEdge,
                _mstr(past.leftMask), _mstr(past.rightMask), past.clinicalMode,
                "yes" if recorded_flags else "no", sorted(past.buffers.keys())[:8])
            self.pastScanLoadFinished.emit(session_label, True)
        except Exception:
            logger.exception("loadPastScan failed for label %r", session_label)
            self.pastScanLoadFinished.emit(session_label, False)

    # --- CSV export ---

    def _materialize_csv(self, session_id: int, output_path: str) -> None:
        """Write one session's corrected-format CSV (multi-second for a long scan)."""
        from omotion.SessionPlayback import materialize_corrected_csv
        materialize_corrected_csv(str(self._interface.scan_db_path), int(session_id),
                                  output_path, include_quality=True)

    @pyqtSlot(int, str)
    def exportScanCsv(self, session_id: int, output_path: str) -> None:
        """Export a scan's session_data to a corrected-format CSV on a worker
        thread (History "Export CSV", after the user picks a save path). The
        result arrives via scanCsvExportFinished(ok, path). Resolves the scan
        by its unique DB id: labels can collide (#254)."""
        if session_id < 0 or not output_path:
            self.scanCsvExportFinished.emit(False, output_path or "")
            return
        spawn(self._export_scan_csv_worker, int(session_id), output_path, name="scan-csv-export")

    def _export_scan_csv_worker(self, session_id: int, output_path: str) -> None:
        self.scanCsvExportFinished.emit(
            self._export_scan_csv_sync(session_id, output_path), output_path)

    def _export_scan_csv_sync(self, session_id: int, output_path: str) -> bool:
        """Worker body of exportScanCsv; unit tests call it directly."""
        try:
            with scan_db(self._interface) as db:
                if db is None:
                    self.errorOccurred.emit("No scan database available.")
                    return False
                session = db.get_session(int(session_id))
            if not session:
                self.errorOccurred.emit(f"No database session found for id {session_id}.")
                return False
            self._materialize_csv(session_id, output_path)
            logger.info("exportScanCsv: exported %r (sid=%d) → %s",
                        session.get("session_label"), session_id, output_path)
            return True
        except Exception as exc:
            logger.exception("exportScanCsv failed for sid %s", session_id)
            self.errorOccurred.emit(f"Export failed:\n{exc}")
            return False

    @pyqtSlot('QVariantList', str)
    def exportScansToFolder(self, session_ids, folder) -> None:
        """Export several scans, one CSV each, into ``folder`` on a worker
        thread. Completion arrives via scansExportFinished(exported, skipped,
        folder). (Callers pre-filter interrupted scans, which can't be
        materialized.)"""
        session_ids = [int(x) for x in (session_ids or [])]
        if not session_ids or not folder:
            self.scansExportFinished.emit(0, 0, str(folder or ""))
            return
        self.notify(f"Exporting {len(session_ids)} scan(s)…", type_="info", tag="scan-export")
        spawn(self._export_scans_worker, session_ids, str(folder), name="scan-batch-export")

    def _export_scans_worker(self, session_ids, folder) -> None:
        result = self._export_scans_sync(session_ids, folder)
        self.scansExportFinished.emit(result["exported"], result["skipped"], folder)

    def _export_scans_sync(self, session_ids, folder) -> dict:
        """Worker body of exportScansToFolder; unit tests call it directly.
        Writes ``<folder>/<label>_export.csv`` per session id, taking the
        label from the id-resolved session (labels can collide, #254).
        Missing or failing scans count as skipped and never raise."""
        result = {"exported": 0, "skipped": 0}
        if not session_ids or not folder:
            return result
        try:
            with scan_db(self._interface) as db:
                if db is None:
                    self.errorOccurred.emit("No scan database available.")
                    return result
                for sid in session_ids:
                    try:
                        sid = int(sid)
                        session = db.get_session(sid)
                        if not session:
                            result["skipped"] += 1
                            continue
                        label = session.get("session_label") or f"scan_{sid}"
                        out_path = os.path.join(folder, f"{label}_export.csv")
                        self._materialize_csv(sid, out_path)
                        result["exported"] += 1
                        logger.info(
                            "exportScansToFolder: exported %r (sid=%d) -> %s", label, sid, out_path)
                    except Exception:
                        logger.exception("exportScansToFolder: failed for %r", sid)
                        result["skipped"] += 1
        except Exception as exc:
            logger.exception("exportScansToFolder failed")
            self.errorOccurred.emit(f"Export failed:\n{exc}")
        return result
