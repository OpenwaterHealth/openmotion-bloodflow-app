"""Scan history: session list, replay sources, CSV export, delete, sample scan."""
import datetime
import json
import os
import re
import threading
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QVariant, pyqtProperty, pyqtSignal, pyqtSlot
from connector._common import DEVICES, logger
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
    ScanDatabase already json-decodes it, but tolerate a raw JSON string too —
    the same defensive parse the History list uses. Returns an empty dict when
    absent or unparseable so callers can merge into it safely."""
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
    """Pull the ``sdk_flags`` block out of a ScanDatabase session dict's
    ``session_meta`` (the camera config / clinical-mode the scan was RUN with,
    written by the SDK's ScanDBSink). Returns None when absent or unparseable.

    Used to lay a replayed scan's plot grid out from its recorded config rather
    than only the cameras that logged data (#175)."""
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
    # export; (exported, skipped, folder) for the batch. HistoryModal shows
    # the summary toast from these instead of a synchronous return value —
    # a big batch used to freeze the GUI for its whole materialize loop.
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
    _pastScanBuffersReady = pyqtSignal(
        int, str, int, object, str, object, object)

    def _maybe_offer_sample_scan(self) -> None:
        """Offer the bundled sample scan when the watchdog found nothing.

        Three gates, all required:
          * research build — a clinical user must never be shown fabricated
            traces, not even behind a prompt;
          * no device at all (`_any_device_connected`, not merely
            `console_missing`) — a console-less rig with a live sensor is a
            real session, not a demo;
          * nothing already bound to the viewer.

        Emits only. Nothing loads until the user accepts the dialog, which
        calls `loadSampleScan`. One-shot, because the watchdog is: a user
        who declines gets no second offer this launch.

        Every exit logs its reason. The offer is a once-per-launch event
        with three independent gates, so a silent return leaves no way to
        tell "correctly suppressed" from "broken" after the fact. The
        sample file's availability is also probed and logged HERE rather
        than only in `_load_sample_scan`, because a user who declines the
        dialog never reaches the loader — without this, a build shipping
        without the CSV would look identical in the log to a healthy one.
        """
        if bool(self._app_config.get("clinicalMode", False)):
            logger.info("[Plot] sample scan offer suppressed — clinical build")
            return
        if self._any_device_connected():
            logger.info(
                "[Plot] sample scan offer suppressed — a device is connected "
                "(console=%s left=%s right=%s)",
                *(self._sdk_connected(n) for n in DEVICES))
            return
        if self._current_scan_source is not None:
            logger.info(
                "[Plot] sample scan offer suppressed — viewer already bound "
                "to %s", type(self._current_scan_source).__name__)
            return
        self._log_sample_scan_availability()
        logger.info("[Plot] no device at watchdog — offering sample scan")
        self.sampleScanOfferRequested.emit()

    @staticmethod
    def _sample_scan_path() -> Path:
        """Resolved location of the bundled sample scan.

        Note `resource_path` returns its under-base candidate even when
        nothing exists there, so this path is a *guess* until stat'd — see
        `_log_sample_scan_availability`."""
        return Path(resource_path("resources", "sample_scan.csv"))

    def _log_sample_scan_availability(self) -> None:
        """Record whether the bundled sample scan is actually on disk.

        Logged at WARNING when it isn't: a missing sample in a research
        build is a packaging fault (openwater.spec drops the `datas` entry
        when the CSV is absent at build time), and the resolved absolute
        path is the only clue to where the frozen app searched."""
        path = self._sample_scan_path()
        try:
            size = path.stat().st_size
        except OSError as exc:
            logger.warning(
                "[Plot] sample scan unavailable — cannot stat %s (%s)",
                path, exc)
            return
        logger.info("[Plot] sample scan available: %s (%d bytes)", path, size)

    # --- SCAN MANAGEMENT METHODS ---
    # Suffix patterns that distinguish the corrected (canonical) CSV
    # from per-scan auxiliary CSVs (raw histo, telemetry). Issue #44:
    # the canonical file dropped its ``_corrected`` suffix so the
    # previous ``*_corrected.csv`` glob no longer matches new scans.
    # Discovery now globs all CSVs and excludes anything matching one
    # of these aux patterns. Order matters: longer / more-specific
    # patterns first so an aux file isn't mistaken for canonical.
    _AUX_CSV_RE = re.compile(
        r"_("
        r"telemetry"                                  # _telemetry.csv
        r"|(?:left|right)_mask[0-9A-Fa-f]+(?:_raw)?"  # raw histo, new + legacy
        r")$"
    )

    @pyqtSlot(result=list)
    def get_scan_list(self):
        """Return sorted list of scan IDs from BOTH the corrected CSVs on disk
        and the scan database's sessions.

        The scan DB is the system of record: clinical-mode scans (and any scan
        with writeCorrectedCsv off) write no corrected CSV, so they exist only
        as DB sessions. A session_label has the same shape as the CSV-derived
        scan id (``YYYYMMDD_HHMMSS_userLabel``), so the two sources merge by id.

        CSV filename formats supported (legacy / corrected-CSV scans):
          New (post-#44): {YYYYMMDD_HHMMSS}_{sessionId}.csv
          Mid:            {YYYYMMDD_HHMMSS}_{sessionId}_corrected.csv
          Legacy:         scan_{sessionId}_{YYYYMMDD_HHMMSS}_corrected.csv
        """
        seen: set[str] = set()
        ids: list[str] = []

        base_path = Path(self._data_root)
        if base_path.exists():
            for f in base_path.glob("*.csv"):
                if not f.is_file():
                    continue
                stem = f.stem
                # Skip per-scan auxiliary files (raw histo, telemetry).
                if self._AUX_CSV_RE.search(stem):
                    continue
                # Mid format: strip the ``_corrected`` suffix to get the
                # canonical scan id.
                if stem.endswith("_corrected"):
                    stem = stem[:-10]
                # Legacy format: ``scan_{sessionId}_{ts}`` — strip the
                # ``scan_`` prefix.
                if stem.startswith("scan_"):
                    stem = stem[5:]
                if stem in seen:
                    continue
                seen.add(stem)
                ids.append(stem)

        # DB-backed scans (no corrected CSV on disk). Best-effort: a missing or
        # unreadable DB just leaves the CSV-derived list.
        db_path = getattr(self._interface, "scan_db_path", None)
        if db_path:
            try:
                from omotion.ScanDatabase import ScanDatabase
                db = ScanDatabase(db_path)
                try:
                    for session in db.iter_sessions():
                        label = (session.get("session_label") or "").strip()
                        if label and label not in seen:
                            seen.add(label)
                            ids.append(label)
                finally:
                    db.close()
            except Exception:
                logger.warning("get_scan_list: could not read scan DB sessions",
                               exc_info=True)

        def ts_key(s):
            # New / mid format starts with YYYYMMDD (8 digits)
            if re.match(r'^\d{8}_\d{6}', s):
                return s[:15]       # YYYYMMDD_HHMMSS
            # Legacy format: sessionId_YYYYMMDD_HHMMSS
            parts = s.split("_", 1)
            return parts[1] if len(parts) == 2 else s

        return sorted(ids, key=ts_key, reverse=True)

    @pyqtSlot(str, result=QVariant)
    def get_scan_details(self, scan_id: str, session_id: int = -1):
        """
        scan_id is either:
          New / mid format: 'YYYYMMDD_HHMMSS_userLabel'
          Legacy format:    'userLabel_YYYYMMDD_HHMMSS'

        session_id, when >= 0, is the unique DB session id: the DB-side
        fields (notes, row-count) are resolved by it instead of by
        label — a by-label lookup returns the newest session with that
        label, so duplicate labels silently read a different session's
        notes (issue #254). File resolution always keys off scan_id;
        the CSVs are named by label on disk.

        For each format we try to resolve the canonical CSV across
        all naming generations (#44):
          New:    {scan_id}.csv
          Mid:    {scan_id}_corrected.csv
          Legacy: scan_{scan_id}_corrected.csv
        And the raw histo CSVs across two generations:
          New:    {scan_id}_(left|right)_mask*_raw.csv
          Legacy: {scan_id}_(left|right)_mask*.csv
        """
        base = Path(self._data_root)

        # Detect format by checking if it starts with a date
        if re.match(r'^\d{8}_\d{6}_', scan_id):
            # New / mid: YYYYMMDD_HHMMSS_userLabel
            parts = scan_id.split("_", 2)
            ts = parts[0] + "_" + parts[1]
            subject = parts[2] if len(parts) > 2 else ""
            notes_path = base / f"{scan_id}_notes.txt"
            left      = (next(base.glob(f"{scan_id}_left_mask*_raw.csv"), None)
                         or next(base.glob(f"{scan_id}_left_mask*.csv"), None))
            right     = (next(base.glob(f"{scan_id}_right_mask*_raw.csv"), None)
                         or next(base.glob(f"{scan_id}_right_mask*.csv"), None))
            # Prefer new naming (no suffix); fall back to mid format
            # (_corrected). Filter out files that match the raw
            # mask pattern so they aren't picked up as the canonical
            # CSV.
            corrected = next(
                (p for p in base.glob(f"{scan_id}.csv") if p.is_file()),
                None,
            )
            if corrected is None:
                corrected = next(base.glob(f"{scan_id}_corrected.csv"), None)
        else:
            # Legacy: userLabel_YYYYMMDD_HHMMSS
            parts = scan_id.split("_", 1)
            subject = parts[0]
            ts = parts[1] if len(parts) > 1 else ""
            notes_path = base / f"scan_{scan_id}_notes.txt"
            left      = next(base.glob(f"scan_{scan_id}_left_mask*.csv"), None)
            right     = next(base.glob(f"scan_{scan_id}_right_mask*.csv"), None)
            corrected = next(base.glob(f"scan_{scan_id}_corrected.csv"), None)

        left_mask = ""
        right_mask = ""
        if left:
            m = re.search(r"_mask([0-9A-Fa-f]+)(?:_raw)?\.csv$", left.name)
            if m:
                left_mask = m.group(1)
        if right:
            m = re.search(r"_mask([0-9A-Fa-f]+)(?:_raw)?\.csv$", right.name)
            if m:
                right_mask = m.group(1)

        # Notes live in the scan DB (sessions.session_notes). Scans from
        # before the DB migration only have a *_notes.txt on disk — fall
        # back to that.
        notes = ""
        has_db_rows = False
        db_path = getattr(self._interface, "scan_db_path", None)
        if db_path:
            try:
                from omotion.ScanDatabase import ScanDatabase
                db = ScanDatabase(db_path)
                try:
                    if session_id >= 0:
                        session = db.get_session(int(session_id))
                    else:
                        session = db.get_session_by_label(scan_id)
                    if session and session.get("session_notes"):
                        notes = session["session_notes"]
                    if session:
                        row = next(
                            db._connection().execute(
                                "SELECT EXISTS(SELECT 1 FROM session_data "
                                "WHERE session_id = ? LIMIT 1)",
                                (int(session["id"]),),
                            ),
                            None,
                        )
                        has_db_rows = bool(row[0]) if row else False
                finally:
                    db.close()
            except Exception:
                logger.warning("get_scan_details: could not read notes/rows from DB",
                               exc_info=True)
        if not notes:
            try:
                notes = notes_path.read_text(encoding="utf-8")
            except OSError:
                # No legacy *_notes.txt fallback for this scan — expected
                # for anything recorded since notes moved into scans.db.
                logger.debug("no legacy notes file at %s", notes_path)

        return {
            "userLabel": subject,
            "sessionId": f"{ts}_{subject}",
            "timestamp": ts,
            "leftMask": left_mask,
            "rightMask": right_mask,
            "leftPath": str(left) if left else "",
            "rightPath": str(right) if right else "",
            "correctedPath": str(corrected) if corrected else "",
            "notesPath": str(notes_path),
            "notes": notes,
            "hasData": bool(has_db_rows or corrected or left or right),
        }

    @staticmethod
    def _friendly_ts(ts: str, tz=None) -> str:
        """Convert a UTC scan timestamp to local time for display."""
        if not ts or len(ts) != 15:
            return ts or "-"

        try:
            utc_dt = datetime.datetime.strptime(
                ts, "%Y%m%d_%H%M%S"
            ).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            return ts

        local_dt = utc_dt.astimezone(tz)
        return local_dt.strftime("%Y-%m-%d %H:%M:%S")

    def _session_to_row(self, s: dict) -> dict:
        """Flatten one ScanDatabase session dict into the QVariantMap the
        History table consumes. Masks/operator come from session_meta
        (written by ScanDBSink); duration from session_start/end."""
        label = (s.get("session_label") or "").strip()
        meta = s.get("session_meta")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        flags = meta.get("sdk_flags") or {}

        left_mask = flags.get("left_camera_mask", -1)
        right_mask = flags.get("right_camera_mask", -1)
        if left_mask is None:
            left_mask = -1
        if right_mask is None:
            right_mask = -1

        user_label = meta.get("subject_id") or ""
        timestamp = ""
        m = re.match(r"^(\d{8}_\d{6})_?(.*)$", label)
        if m:
            timestamp = m.group(1)
            if not user_label:
                user_label = m.group(2)

        start = s.get("session_start")
        end = s.get("session_end")
        # Prefer the actual trigger-ON duration stamped by the completion
        # handler (issue #335). session_end - session_start brackets the whole
        # pipeline lifetime — pre-trigger setup + post-trigger USB drain — which
        # runs ~3 s longer than the laser-on window, so the raw delta reads e.g.
        # "2:03" for a 2:00 scan. Old rows lack the key; fall back to wall-clock.
        actual = meta.get("actual_duration_sec")
        duration = None
        if actual is not None:
            try:
                duration = float(actual)
            except (TypeError, ValueError):
                duration = None
        if duration is None:
            if start is not None and end is not None:
                duration = float(end) - float(start)
            else:
                duration = -1.0

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
        History table. DB-only — does not list CSV-derived scans.
        Best-effort: a missing/unreadable DB yields []."""
        db_path = getattr(self._interface, "scan_db_path", None)
        if not db_path:
            return []
        rows = []
        try:
            from omotion.ScanDatabase import ScanDatabase
            db = ScanDatabase(db_path)
            try:
                for s in db.iter_sessions():
                    # Per-row guard: one malformed session must not blank the
                    # whole History view — skip it and keep the rest.
                    try:
                        rows.append(self._session_to_row(s))
                    except Exception:
                        logger.warning(
                            "get_scan_sessions: skipping malformed session %s",
                            s.get("id"), exc_info=True)
            finally:
                db.close()
        except Exception:
            logger.warning("get_scan_sessions: could not read scan DB",
                           exc_info=True)
            return []
        rows.sort(key=lambda r: r["timestamp"], reverse=True)
        return rows

    @pyqtSlot(int, result="QVariantMap")
    def get_session_stats(self, session_id: int):
        """Lazily fetch heavier per-scan stats (row count) when a History
        row is focused, so the list query itself stays cheap."""
        db_path = getattr(self._interface, "scan_db_path", None)
        if not db_path:
            return {"sampleCount": 0}
        try:
            from omotion.ScanDatabase import ScanDatabase
            db = ScanDatabase(db_path)
            try:
                # Raw COUNT via the connection — ScanDatabase exposes no
                # public row-count API; get_scan_details reaches for
                # _connection() the same way for its EXISTS probe.
                row = next(
                    db._connection().execute(
                        "SELECT COUNT(*) FROM session_data"
                        " WHERE session_id = ?",
                        (int(session_id),),
                    ),
                    None,
                )
                return {"sampleCount": int(row[0]) if row else 0}
            finally:
                db.close()
        except Exception:
            logger.warning(
                "get_session_stats failed for %s", session_id,
                exc_info=True)
            return {"sampleCount": 0}

    @pyqtSlot("QVariantList", result=int)
    def deleteScans(self, session_ids):
        """Delete the given scan-DB sessions (CASCADE removes their
        session_data). Returns the count actually deleted. No password in
        any build (#703): HistoryModal asks "are you sure" before calling
        this, and the delete is audited (scan_deleted)."""
        db_path = getattr(self._interface, "scan_db_path", None)
        if not db_path:
            return 0
        deleted = 0
        try:
            from omotion.ScanDatabase import ScanDatabase
            db = ScanDatabase(db_path)
            try:
                for sid in session_ids:
                    try:
                        if db.delete_session(int(sid)):
                            deleted += 1
                            logger.info(
                                "deleteScans: removed session %s", sid)
                    except Exception:
                        logger.warning(
                            "deleteScans: failed to delete %s", sid,
                            exc_info=True)
            finally:
                db.close()
        except Exception:
            logger.warning(
                "deleteScans: could not open scan DB", exc_info=True)
        # Tolerate non-int ids the same way the delete loop above does —
        # this comprehension runs outside AuditLog.log's fail-soft wrapper.
        _ids = []
        for s in session_ids:
            try:
                _ids.append(int(s))
            except (TypeError, ValueError):
                pass
        self._audit.log("scan_deleted", {
            "session_ids": _ids,
            "count": deleted,
        })
        return deleted

    @pyqtProperty(QObject, notify=currentScanSourceChanged)
    def currentScanSource(self) -> ScanDataSource | None:
        """Current ScanDataSource bound to the PlotViewer. Usually the
        live source during a scan; can be a PastScanSource while the
        user is reviewing history via loadPastScan."""
        return self._current_scan_source

    @pyqtProperty(bool, notify=liveSourceAvailableChanged)
    def liveSourceAvailable(self) -> bool:
        """True when a LiveScanSource is held (i.e. at least one scan
        has run this session). PlotToolbar uses this to decide whether
        the "Back to live" button should switch sources back to live."""
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

    @pyqtSlot()
    def loadSampleScan(self) -> None:
        """Load the bundled sample scan into the replay viewer (#314).

        Called from SampleScanOfferModal when the user accepts the offer
        the startup watchdog raised — never automatically, and never in a
        clinical build (the connector doesn't emit the offer there). The
        sample is never written anywhere, so real scan data is never
        polluted; once the user runs a scan, startCapture's fresh
        LiveScanSource retires it.

        Guarded so it can't clobber a source that got bound between the
        offer and the user's click.
        """
        if self._current_scan_source is not None:
            logger.info(
                "[Plot] sample scan request ignored — viewer already bound "
                "to %s", type(self._current_scan_source).__name__)
            return
        self._load_sample_scan(str(self._sample_scan_path()))

    def _load_sample_scan(self, csv_path: str) -> None:
        """Parse a scan-export CSV and bind it to the viewer as a DB-free
        PastScanSource. Fail-soft: a missing/unreadable/empty CSV logs a
        warning and leaves the viewer untouched (never raises — a bad
        sample file must not break boot). Split from the slot so unit tests
        can drive it with an arbitrary path.

        Every failure exit names the resolved path and distinguishes WHY.
        The three causes need different fixes — file absent is a packaging
        fault, unreadable is a permissions/IO fault, parsed-but-empty is a
        bad dataset — and the user-visible symptom is identical in all
        three (an empty viewer), so the log is the only place they can be
        told apart."""
        # Stat first: the loader below is fail-soft on a missing file
        # (`_load_corrected_csv_into` swallows OSError), which would
        # otherwise land a packaging fault in the generic empty-buffers
        # branch and read as a bad dataset.
        try:
            size = os.stat(csv_path).st_size
        except OSError as exc:
            logger.warning(
                "[Plot] sample scan unavailable — cannot read %s (%s)",
                csv_path, exc)
            return
        if size == 0:
            logger.warning(
                "[Plot] sample scan unavailable — file is empty (0 bytes): %s",
                csv_path)
            return
        try:
            buffers = load_csv_scan_buffers(csv_path)
        except Exception:
            logger.warning(
                "[Plot] sample scan load failed: %s", csv_path, exc_info=True)
            return
        if buffers_are_empty(buffers):
            logger.warning(
                "[Plot] sample scan unavailable — %s (%d bytes) parsed but "
                "held no usable samples; expected a per-cam scan-export CSV "
                "with bfi_l1..contrast_r8 columns", csv_path, size)
            return
        try:
            sample = PastScanSource(
                scan_db=None,
                session_id=-1,
                parent=self,
                preloaded_buffers=buffers,
                user_label="Sample scan",
                date_time="",
            )
        except Exception:
            logger.warning(
                "[Plot] sample scan source construction failed",
                exc_info=True)
            return
        self._set_current_scan_source(sample)
        n_samples = sum(b.n for b in sample.buffers.values())
        logger.info(
            "[Plot] loaded sample scan: buffers=%d samples=%d from %s",
            len(sample.buffers), n_samples, csv_path)

    @pyqtSlot(int, str)
    def loadPastScan(self, session_id: int, session_label: str) -> None:
        """Open the saved scan with the given unique DB session id and
        display it in the PlotViewer. The held live source is left
        intact so a subsequent showLiveSource() can return to it.

        session_label (the YYYYMMDD_HHMMSS_userLabel string used
        elsewhere in the UI) still names the scan's CSVs on disk and the
        log/signal payloads, but the DB session is resolved by id — a
        by-label lookup returns the newest session with that label, so
        duplicate labels silently loaded a different scan than the one
        selected in History (issue #254, same fix as the exports).

        Asynchronous load (issue #152): the bulk SQLite walk / CSV parse
        is multi-second for long scans (a ~70-min scan measured ~8.3M
        samples ≈ 20 s) and used to freeze the GUI thread. The heavy
        load now runs on a daemon worker thread; the finished buffers
        are marshalled back to the GUI thread via _pastScanBuffersReady,
        where the PastScanSource QObject is constructed and bound to the
        viewer. pastScanLoadFinished(label, ok) fires on completion
        either way so the History modal can clear its busy state."""
        if session_id < 0 or not session_label:
            logger.warning(
                "loadPastScan: invalid session_id %r / label %r",
                session_id, session_label,
            )
            self.pastScanLoadFinished.emit(session_label or "", False)
            return
        db_path = getattr(self._interface, "scan_db_path", None)
        if not db_path:
            logger.warning(
                "loadPastScan: scan_db_path unavailable on interface"
            )
            self.pastScanLoadFinished.emit(session_label, False)
            return
        self._audit.log("scan_viewed", {"label": session_label,
                                        "session_id": int(session_id)})
        # Per-cam corrected CSV ({scan_id}.csv, 82-col wide format)
        # is the only source of per-cam BFI/BVI/mean/contrast for
        # past replay — the DB's session_data only holds side-
        # aggregated corrected-frame placeholders. CsvSink writes
        # this CSV unconditionally for every scan, so it's reliably
        # available. Resolving the path is a cheap directory glob —
        # fine on the GUI thread (the modal already does it per
        # selection); the heavy parse happens on the worker.
        details = self.get_scan_details(
            session_label, session_id=int(session_id)) or {}
        corrected_csv = details.get("correctedPath") or None
        self._past_scan_load_seq += 1
        seq = self._past_scan_load_seq
        threading.Thread(
            target=self._load_past_scan_worker,
            args=(seq, int(session_id), session_label, str(db_path),
                  corrected_csv),
            name=f"past-scan-load-{seq}",
            daemon=True,
        ).start()

    def _load_past_scan_worker(
        self,
        seq: int,
        session_id: int,
        session_label: str,
        db_path: str,
        corrected_csv: Optional[str],
    ) -> None:
        """Worker thread: bulk-load a past scan's samples (the heavy,
        Qt-free part of loadPastScan). Opens its own ScanDatabase handle;
        results cross back to the GUI thread via _pastScanBuffersReady
        (cross-thread signal → queued delivery)."""
        try:
            from omotion.ScanDatabase import ScanDatabase
            from plot.data_sources import load_past_scan_buffers
            db = ScanDatabase(db_path)
            try:
                session = db.get_session(session_id)
                if not session:
                    logger.warning(
                        "loadPastScan: no session found for id %d "
                        "(label %r)",
                        session_id, session_label,
                    )
                    self._pastScanBuffersReady.emit(
                        seq, session_label, -1, None, "", None, None
                    )
                    return
                # The camera config the scan was RUN with lives in
                # session_meta.sdk_flags (written by ScanDBSink). Thread it to
                # the GUI thread so PastScanSource lays its grid out from the
                # recorded config, not just the cameras that logged data
                # (issue #175 reopen). Same parse the History list uses.
                recorded_flags = _sdk_flags_from_session(session)
                # The viewer's "Viewing" badge (issue #245) names the scan with
                # the user label + date/time. Derive them from the SAME row the
                # History list renders (_session_to_row prefers subject_id over
                # the label suffix) so the badge can't disagree with the row the
                # user clicked. Best-effort: a parse failure just hides the
                # badge — it must never block the load.
                display_meta = None
                try:
                    row = self._session_to_row(session)
                    display_meta = {
                        "userLabel": row["userLabel"],
                        "dateTime": row["dateTime"],
                    }
                except Exception:
                    logger.warning(
                        "loadPastScan: could not derive badge meta for %r",
                        session_label, exc_info=True,
                    )
                buffers, _ = load_past_scan_buffers(
                    db, session_id, corrected_csv
                )
            finally:
                db.close()
            # Detect which source actually populated per-cam BFI: if
            # any (side, cam_id != -1, bfi) buffer exists, the DB was
            # sufficient and the CSV fallback was skipped.
            db_had_per_cam = any(k[1] >= 0 and k[2] == "bfi" for k in buffers)
            source_tag = (
                "db" if db_had_per_cam
                else ("csv" if corrected_csv else "db-only-sentinel")
            )
            self._pastScanBuffersReady.emit(
                seq, session_label, session_id, buffers, source_tag,
                recorded_flags, display_meta,
            )
        except Exception:
            logger.exception("loadPastScan failed for label %r", session_label)
            self._pastScanBuffersReady.emit(
                seq, session_label, -1, None, "", None, None
            )

    def _on_past_scan_buffers_ready(
        self,
        seq: int,
        session_label: str,
        session_id: int,
        buffers,
        source_tag: str,
        recorded_flags=None,
        display_meta=None,
    ) -> None:
        """GUI thread: bind a worker-loaded past scan to the viewer.
        Results from a superseded request (user clicked another scan
        while this one was loading) are dropped."""
        if seq != self._past_scan_load_seq:
            logger.info(
                "loadPastScan: dropping stale result for %r "
                "(seq %d, latest %d)",
                session_label, seq, self._past_scan_load_seq,
            )
            return
        if buffers is None:
            self.errorOccurred.emit(
                f"Could not load scan '{session_label}' for plotting.\n"
                "See the app log for details."
            )
            self.pastScanLoadFinished.emit(session_label, False)
            return
        if buffers_are_empty(buffers):
            logger.info(
                "loadPastScan: %r contains no samples — not displaying",
                session_label,
            )
            self.errorOccurred.emit(
                f"Scan '{session_label}' contains no data.\n"
                "It was interrupted before any data could be recorded."
            )
            self.pastScanLoadFinished.emit(session_label, False)
            return
        try:
            dm = display_meta or {}
            past = PastScanSource(
                scan_db=None,
                session_id=session_id,
                parent=self,
                preloaded_buffers=buffers,
                recorded_flags=recorded_flags,
                user_label=dm.get("userLabel", ""),
                date_time=dm.get("dateTime", ""),
            )
            self._set_current_scan_source(past)
            # Diagnostic — left in for now so we can spot regressions
            # in past-scan loading. Cheap (one log line per click).
            n_buffers = len(past.buffers)
            n_samples = sum(b.n for b in past.buffers.values())
            sample_keys = sorted(past.buffers.keys())[:8]
            # Grid masks are what the viewer actually lays out from; log them
            # next to the data shape so a config/data mismatch (issue #175) is
            # diagnosable from one line. flags=no means an older scan with no
            # stored sdk_flags (masks derived from data instead). -1 prints as
            # "unknown" (viewer falls back to the live selection).
            def _mstr(v):
                return f"0x{v & 0xFF:02X}" if v >= 0 else "unknown"
            logger.info(
                "[Plot] loaded past scan %r (session_id=%d) source=%s: "
                "buffers=%d samples=%d liveEdge=%.3f gridMasks=L%s/R%s "
                "clinical=%d flags=%s sample_keys=%s",
                session_label, session_id, source_tag,
                n_buffers, n_samples, past.liveEdge,
                _mstr(past.leftMask), _mstr(past.rightMask),
                past.clinicalMode,
                "yes" if recorded_flags else "no",
                sample_keys,
            )
            self.pastScanLoadFinished.emit(session_label, True)
        except Exception:
            logger.exception("loadPastScan failed for label %r", session_label)
            self.pastScanLoadFinished.emit(session_label, False)

    @pyqtSlot(int, str)
    def exportScanCsv(self, session_id: int, output_path: str) -> None:
        """Export a scan's session_data to a corrected-format CSV, on a
        worker thread. Called from the History modal's "Export CSV"
        button after the user picks a save path; the result arrives via
        scanCsvExportFinished(ok, path) — materializing a long scan is
        multi-second and used to freeze the GUI for the duration.
        Resolves the scan by its unique DB id — a by-label lookup can
        silently pick a different session when labels collide (#254)."""
        if session_id < 0 or not output_path:
            self.scanCsvExportFinished.emit(False, output_path or "")
            return
        threading.Thread(
            target=self._export_scan_csv_worker,
            args=(int(session_id), output_path),
            name="scan-csv-export", daemon=True,
        ).start()

    def _export_scan_csv_worker(self, session_id: int,
                                output_path: str) -> None:
        ok = self._export_scan_csv_sync(session_id, output_path)
        self.scanCsvExportFinished.emit(ok, output_path)

    def _export_scan_csv_sync(self, session_id: int,
                              output_path: str) -> bool:
        """Worker body of exportScanCsv; unit tests call it directly."""
        try:
            from omotion.ScanDatabase import ScanDatabase
            from omotion.SessionPlayback import materialize_corrected_csv

            db_path = getattr(self._interface, "scan_db_path", None)
            if not db_path:
                self.errorOccurred.emit("No scan database available.")
                return False
            with ScanDatabase(db_path) as db:
                session = db.get_session(int(session_id))
            if not session:
                self.errorOccurred.emit(
                    f"No database session found for id {session_id}."
                )
                return False
            materialize_corrected_csv(
                str(db_path), int(session_id), output_path,
                include_quality=True,
            )
            logger.info(
                "exportScanCsv: exported %r (sid=%d) → %s",
                session.get("session_label"), session_id, output_path,
            )
            return True
        except Exception as exc:
            logger.exception("exportScanCsv failed for sid %s", session_id)
            self.errorOccurred.emit(f"Export failed:\n{exc}")
            return False

    @pyqtSlot('QVariantList', str)
    def exportScansToFolder(self, session_ids, folder) -> None:
        """Export several scans, one CSV each, into ``folder`` — on a
        worker thread. Completion arrives via
        scansExportFinished(exported, skipped, folder); a big batch used
        to freeze the GUI for its whole materialize loop. Takes each
        label from the id-resolved DB session (ids are unique; labels
        can collide — #254). (Callers pre-filter interrupted scans,
        which can't be materialized.)"""
        session_ids = [int(x) for x in (session_ids or [])]
        if not session_ids or not folder:
            self.scansExportFinished.emit(0, 0, str(folder or ""))
            return
        self.notify(f"Exporting {len(session_ids)} scan(s)…", type_="info",
                    tag="scan-export")
        threading.Thread(
            target=self._export_scans_worker, args=(session_ids, str(folder)),
            name="scan-batch-export", daemon=True,
        ).start()

    def _export_scans_worker(self, session_ids, folder) -> None:
        result = self._export_scans_sync(session_ids, folder)
        self.scansExportFinished.emit(
            result["exported"], result["skipped"], folder)

    def _export_scans_sync(self, session_ids, folder) -> dict:
        """Worker body of exportScansToFolder. Writes
        ``<folder>/<label>_export.csv`` for every session id; opens the
        scan DB once for the whole batch. Missing or failing scans are
        counted as skipped and never raise. Returns
        ``{"exported": int, "skipped": int}``. Unit tests call it
        directly."""
        result = {"exported": 0, "skipped": 0}
        if not session_ids or not folder:
            return result
        try:
            from omotion.ScanDatabase import ScanDatabase
            from omotion.SessionPlayback import materialize_corrected_csv

            db_path = getattr(self._interface, "scan_db_path", None)
            if not db_path:
                self.errorOccurred.emit("No scan database available.")
                return result
            with ScanDatabase(db_path) as db:
                for sid in session_ids:
                    try:
                        sid = int(sid)
                        session = db.get_session(sid)
                        if not session:
                            result["skipped"] += 1
                            continue
                        label = session.get("session_label") or f"scan_{sid}"
                        out_path = os.path.join(
                            folder, f"{label}_export.csv")
                        materialize_corrected_csv(
                            str(db_path), sid, out_path,
                            include_quality=True,
                        )
                        result["exported"] += 1
                        logger.info(
                            "exportScansToFolder: exported %r (sid=%d) -> %s",
                            label, sid, out_path,
                        )
                    except Exception:
                        logger.exception(
                            "exportScansToFolder: failed for %r", sid)
                        result["skipped"] += 1
            return result
        except Exception as exc:
            logger.exception("exportScansToFolder failed")
            self.errorOccurred.emit(f"Export failed:\n{exc}")
            return result
