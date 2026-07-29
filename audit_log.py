"""Append-only, machine-readable audit log stored in scans.db.

A small, self-contained logger used primarily by auditors. Writes one
row per event to a ``logs`` table in the same SQLite file the SDK's
ScanDatabase uses (scans.db). Owns its own long-lived connection so it
can append alongside ScanDatabase's on-demand handles.

Fail-soft by contract: construction with a missing path, or any DB
error, degrades the instance to a no-op — a failed audit write must
never crash or block the app.
"""

from __future__ import annotations

import csv
import datetime
import json
import logging
import platform
import socket
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("openmotion.bloodflow-app.audit")

# ── Event-type constants ────────────────────────────────────────────────
EV_SYSTEM_STARTUP = "system_startup"
EV_SYSTEM_INFO = "system_info"
EV_SYSTEM_SHUTDOWN = "system_shutdown"
EV_DEVICE_CONNECTED = "device_connected"
EV_DEVICE_DISCONNECTED = "device_disconnected"
EV_DEVICE_STATS = "device_stats"
EV_SCAN_STARTED = "scan_started"
EV_SCAN_ENDED = "scan_ended"
EV_CALIBRATION_STARTED = "calibration_started"
EV_CALIBRATION_ENDED = "calibration_ended"
EV_SETTINGS_CHANGED = "settings_changed"
EV_SCAN_VIEWED = "scan_viewed"
EV_SCAN_DELETED = "scan_deleted"
EV_AUDIT_LOG_VIEWED = "audit_log_viewed"
EV_AUDIT_LOG_EXPORTED = "audit_log_exported"
EV_DEBUG_BUNDLE_CREATED = "debug_bundle_created"

# CSV export column order.
_CSV_FIELDS = ["ts_iso", "ts_epoch", "event_type", "details"]


def _escape_like(needle: str) -> str:
    """Escape LIKE wildcards so ``needle`` matches literally (used with
    ``ESCAPE '\\'``)."""
    return (
        needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def parse_date_bound(text: Any, end: bool = False) -> Optional[float]:
    """Parse a ``YYYY-MM-DD`` date string into a local-time epoch bound
    for filtering on ``ts_epoch``.

    Returns midnight at the start of the day, or — when ``end`` is True —
    midnight at the start of the *next* day (an exclusive upper bound
    covering the whole named day). ``None`` for empty/invalid input, so
    a malformed filter simply doesn't constrain the query.
    """
    if not text:
        return None
    try:
        day = datetime.datetime.strptime(str(text).strip(), "%Y-%m-%d")
    except ValueError:
        return None
    if end:
        day += datetime.timedelta(days=1)
    return day.timestamp()


def gather_host_info() -> Dict[str, Any]:
    """Collect host/system facts for the ``system_info`` event. Best-effort
    — any probe that fails is simply omitted."""
    info: Dict[str, Any] = {}
    try:
        info["hostname"] = socket.gethostname()
        info["platform"] = platform.platform()
        info["system"] = f"{platform.system()} {platform.release()}".strip()
        info["arch"] = platform.machine()
        info["processor"] = platform.processor()
        info["python"] = platform.python_version()
    except Exception:
        logger.warning("gather_host_info: core probe failed", exc_info=True)
    try:
        if platform.system() == "Windows":
            import ctypes

            class _MEMSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            mem = _MEMSTATUSEX()
            mem.dwLength = ctypes.sizeof(_MEMSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
            info["ram_gb"] = round(mem.ullTotalPhys / (1024 ** 3), 2)
    except Exception:
        pass
    return info


class AuditLog:
    """Append-only audit-event store backed by the ``logs`` table in
    scans.db. Thread-safe (single connection + write lock). No-op when
    constructed without a usable path."""

    def __init__(self, db_path: Optional[str | Path]) -> None:
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        if not db_path:
            logger.info(
                "AuditLog: no db_path — audit logging disabled (no-op)."
            )
            return
        try:
            # Open through the SDK helper, not stdlib sqlite3. On a clinical
            # build scans.db is SQLCipher-encrypted, and a stdlib connection
            # raises "file is not a database" — which the fail-soft handler
            # below would swallow, silently disabling the audit log on exactly
            # the builds that need it. db_open picks the driver from the
            # process encryption policy, applies the key, and sets the same
            # WAL/busy_timeout PRAGMAs this used to set by hand. AuditLog
            # itself never sees the key.
            from omotion import db_open

            self._conn = db_open.connect(db_path)
            self._init_schema()
        except Exception:
            logger.warning(
                "AuditLog: failed to open %s — disabling audit logging.",
                db_path, exc_info=True,
            )
            self._conn = None

    def _init_schema(self) -> None:
        assert self._conn is not None
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS logs (
                id          INTEGER PRIMARY KEY,
                ts_epoch    REAL NOT NULL,
                ts_iso      TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                details     TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts_epoch);
            """
        )

    @property
    def enabled(self) -> bool:
        return self._conn is not None

    def log(
        self, event_type: str, details: Optional[Dict[str, Any]] = None
    ) -> None:
        """Append one event. ``details`` (if not None) is stored as compact,
        deterministic JSON. Never raises."""
        ts_epoch = time.time()
        ts_iso = (
            datetime.datetime.fromtimestamp(ts_epoch)
            .astimezone()
            .isoformat(timespec="seconds")
        )
        details_json = (
            json.dumps(details, separators=(",", ":"), sort_keys=True,
                       default=str)
            if details is not None else None
        )
        try:
            with self._lock:
                if self._conn is None:
                    return
                self._conn.execute(
                    "INSERT INTO logs (ts_epoch, ts_iso, event_type, details)"
                    " VALUES (?, ?, ?, ?)",
                    (ts_epoch, ts_iso, str(event_type), details_json),
                )
                self._conn.commit()
        except Exception:
            logger.warning(
                "AuditLog: failed to write %s event", event_type, exc_info=True
            )

    def query(
        self,
        limit: int = 500,
        event_type: Optional[str] = None,
        since_epoch: Optional[float] = None,
        until_epoch: Optional[float] = None,
        contains: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return up to ``limit`` rows, newest first, as plain dicts.

        Optional filters (ANDed together; #226):
          - ``event_type``: exact match on the event type.
          - ``since_epoch``: inclusive lower bound on ``ts_epoch``.
          - ``until_epoch``: exclusive upper bound on ``ts_epoch`` (pair
            with ``parse_date_bound(..., end=True)`` for whole days).
          - ``contains``: case-insensitive substring across event_type
            and details; LIKE wildcards in the needle match literally.

        ``limit`` applies to the filtered rows.
        """
        sql = "SELECT id, ts_epoch, ts_iso, event_type, details FROM logs"
        where: List[str] = []
        params: List[Any] = []
        if event_type:
            where.append("event_type = ?")
            params.append(str(event_type))
        if since_epoch is not None:
            where.append("ts_epoch >= ?")
            params.append(float(since_epoch))
        if until_epoch is not None:
            where.append("ts_epoch < ?")
            params.append(float(until_epoch))
        if contains:
            pat = f"%{_escape_like(str(contains))}%"
            where.append(
                "(event_type LIKE ? ESCAPE '\\'"
                " OR IFNULL(details, '') LIKE ? ESCAPE '\\')"
            )
            params += [pat, pat]
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        try:
            with self._lock:
                if self._conn is None:
                    return []
                cur = self._conn.execute(sql, params)
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception:
            logger.warning("AuditLog: query failed", exc_info=True)
            return []

    def distinct_event_types(self) -> List[str]:
        """Sorted list of the distinct event types present in the log
        (for filter dropdowns). Empty when disabled / on error."""
        try:
            with self._lock:
                if self._conn is None:
                    return []
                cur = self._conn.execute(
                    "SELECT DISTINCT event_type FROM logs"
                    " ORDER BY event_type ASC"
                )
                return [row[0] for row in cur.fetchall()]
        except Exception:
            logger.warning("AuditLog: distinct_event_types failed",
                           exc_info=True)
            return []

    def count(self) -> int:
        """Total number of log rows (0 when disabled / on error)."""
        try:
            with self._lock:
                if self._conn is None:
                    return 0
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM logs"
                ).fetchone()
                return int(row[0]) if row else 0
        except Exception:
            logger.warning("AuditLog: count failed", exc_info=True)
            return 0

    def export_csv(self, dest_path: str | Path) -> int:
        """Write the full log (oldest first) to ``dest_path`` as CSV.
        Returns the number of data rows written (0 on any failure)."""
        with self._lock:
            if self._conn is None:
                return 0
            try:
                rows = self._conn.execute(
                    "SELECT ts_iso, ts_epoch, event_type, details"
                    " FROM logs ORDER BY id ASC"
                ).fetchall()
            except Exception:
                logger.warning("AuditLog: export_csv query failed",
                               exc_info=True)
                return 0
        try:
            with open(dest_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(_CSV_FIELDS)
                w.writerows(rows)
        except OSError:
            logger.warning("AuditLog: export_csv write to %s failed",
                           dest_path, exc_info=True)
            return 0
        return len(rows)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
