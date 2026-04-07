"""
bfstorage.py — SQLite-backed storage for OpenMotion bloodflow.

Schema
------
sessions    : session_id (TEXT PK, alphanumeric e.g. "ow98NSF5"),
              session_start (REAL, Unix timestamp), session_end (REAL),
              session_notes (TEXT), session_meta (TEXT, JSON)

session_raw : id (TEXT, UUID4 PK), session_id (FK), side (TEXT 'left'/'right'),
              cam_id, frame_id, timestamp_s, hist (BLOB, 1024×uint32 LE),
              temp, sum, tcm, tcl, pdc

session_data: id (TEXT, UUID4 PK), session_id (FK), cam_id,
              side (TEXT 'left'/'right'), time_s,
              bfi, bvi, contrast, mean

Writers
-------
RawWriter  — background-thread batched writer for session_raw.
DataWriter — background-thread batched writer for session_data.

Readers
-------
RawReader  — cursor-based generator yielding Sample objects from session_raw.
DataReader — cursor-based generator yielding dicts from session_data.

Export
------
export_raw_to_csv()  — write session_raw rows to a CSV file.
export_data_to_csv() — write session_data rows to a CSV file.
"""

from __future__ import annotations

import csv
import json
import logging
import queue
import sqlite3
import threading
import uuid
from typing import Generator, Iterator, List, Optional

import numpy as np

from api.session_samples import Sample

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HIST_BINS = 1024
_HIST_DTYPE = np.uint32

_DEFAULT_BATCH_SIZE = 200
_DEFAULT_QUEUE_SIZE = 8192

_SENTINEL = object()

# ---------------------------------------------------------------------------
# Histogram helpers
# ---------------------------------------------------------------------------


def _pack_hist(hist) -> bytes:
    """Pack 1024 uint32 values as little-endian bytes (4096 bytes)."""
    arr = np.asarray(hist, dtype=np.uint32)
    if arr.size != _HIST_BINS:
        raise ValueError(f"hist must have {_HIST_BINS} bins, got {arr.size}")
    return arr.tobytes()


def _unpack_hist(blob: bytes) -> np.ndarray:
    """Restore histogram ndarray from packed bytes."""
    return np.frombuffer(blob, dtype=np.uint32).copy()


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    session_start REAL NOT NULL,
    session_end   REAL,
    session_notes TEXT,
    session_meta  TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS session_raw (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    side        TEXT NOT NULL CHECK(side IN ('left','right')),
    cam_id      INTEGER NOT NULL,
    frame_id    INTEGER NOT NULL,
    timestamp_s REAL NOT NULL,
    hist        BLOB NOT NULL,
    temp        REAL NOT NULL,
    sum         INTEGER NOT NULL,
    tcm         REAL NOT NULL DEFAULT 0,
    tcl         REAL NOT NULL DEFAULT 0,
    pdc         REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS session_data (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    cam_id      INTEGER NOT NULL,
    side        TEXT NOT NULL CHECK(side IN ('left','right')),
    time_s      REAL NOT NULL,
    bfi         REAL NOT NULL,
    bvi         REAL NOT NULL,
    contrast    REAL NOT NULL,
    mean        REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_session_side
    ON session_raw (session_id, side);
CREATE INDEX IF NOT EXISTS idx_data_session_side_time
    ON session_data (session_id, side, time_s);
"""

# ---------------------------------------------------------------------------
# Schema init
# ---------------------------------------------------------------------------


def init_db(conn: sqlite3.Connection) -> None:
    """Ensure WAL mode and current schema exist."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_DDL)
    conn.commit()


def open_db(db_path: str) -> sqlite3.Connection:
    """Open (or create) a database file and initialise the schema."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    init_db(conn)
    return conn


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def create_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    session_start: float,
    session_end: Optional[float] = None,
    session_notes: Optional[str] = None,
    session_meta: Optional[dict] = None,
) -> str:
    """
    Insert a new session row and return its session_id.

    Parameters
    ----------
    conn          : open sqlite3.Connection
    session_id    : alphanumeric ID from the scan filename (e.g. "ow98NSF5")
    session_start : Unix timestamp (float)
    session_end   : Unix timestamp when session closed, or None
    session_notes : free-text annotation (multi-line OK)
    session_meta  : dict serialised to JSON; store fps, mask, side info, etc.
    """
    meta_json = json.dumps(session_meta or {})
    conn.execute(
        """
        INSERT OR IGNORE INTO sessions
            (session_id, session_start, session_end, session_notes, session_meta)
        VALUES (?, ?, ?, ?, ?)
        """,
        (session_id, session_start, session_end, session_notes, meta_json),
    )
    conn.commit()
    return session_id


def close_session(
    conn: sqlite3.Connection,
    session_id: str,
    session_end: float,
) -> None:
    """Set session_end timestamp."""
    conn.execute(
        "UPDATE sessions SET session_end=? WHERE session_id=?",
        (session_end, session_id),
    )
    conn.commit()


def list_sessions(conn: sqlite3.Connection) -> list:
    """Return all session rows as a list of dicts."""
    rows = conn.execute(
        """
        SELECT s.session_id, s.session_start, s.session_end, s.session_notes,
               (SELECT COUNT(*) FROM session_raw  r WHERE r.session_id = s.session_id) AS raw_count,
               (SELECT COUNT(*) FROM session_data d WHERE d.session_id = s.session_id) AS data_count
        FROM sessions s
        ORDER BY s.session_start
        """
    ).fetchall()
    keys = ("session_id", "session_start", "session_end", "session_notes",
            "raw_count", "data_count")
    return [dict(zip(keys, row)) for row in rows]


# ---------------------------------------------------------------------------
# Internal batch commit helpers
# ---------------------------------------------------------------------------


def _commit_raw_batch(conn: sqlite3.Connection, batch: list) -> None:
    rows = [
        (
            str(uuid.uuid4()),
            s.session_id,        # injected by writer
            s.side,
            int(s.cam_id),
            int(s.frame_id),
            float(s.timestamp),
            _pack_hist(s.hist),
            float(s.temp),
            int(s.summ),
            float(s.tcm),
            float(s.tcl),
            float(s.pdc),
        )
        for s in batch
    ]
    conn.executemany(
        """
        INSERT INTO session_raw
            (id, session_id, side, cam_id, frame_id, timestamp_s,
             hist, temp, sum, tcm, tcl, pdc)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    conn.commit()
    log.debug("RawWriter: committed %d rows", len(rows))


def _commit_data_batch(conn: sqlite3.Connection, batch: list) -> None:
    rows = [
        (
            str(uuid.uuid4()),
            d["session_id"],
            int(d["cam_id"]),
            d["side"],
            float(d["time_s"]),
            float(d["bfi"]),
            float(d["bvi"]),
            float(d["contrast"]),
            float(d["mean"]),
        )
        for d in batch
    ]
    conn.executemany(
        """
        INSERT INTO session_data
            (id, session_id, cam_id, side, time_s, bfi, bvi, contrast, mean)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    conn.commit()
    log.debug("DataWriter: committed %d rows", len(rows))


# ---------------------------------------------------------------------------
# A lightweight named-tuple adapter so Session.sample can carry session_id
# ---------------------------------------------------------------------------

class _SampleWithSession:
    """Thin wrapper that binds a Sample to a session_id for the writer."""
    __slots__ = ("session_id", "side", "cam_id", "frame_id", "timestamp",
                 "hist", "temp", "summ", "tcm", "tcl", "pdc")

    def __init__(self, session_id: str, sample: Sample) -> None:
        self.session_id = session_id
        self.side      = sample.side
        self.cam_id    = sample.cam_id
        self.frame_id  = sample.frame_id
        self.timestamp = sample.timestamp
        self.hist      = sample.hist
        self.temp      = sample.temp
        self.summ      = sample.summ
        self.tcm       = sample.tcm
        self.tcl       = sample.tcl
        self.pdc       = sample.pdc


# ---------------------------------------------------------------------------
# Stream-In: RawWriter
# ---------------------------------------------------------------------------


class RawWriter:
    """
    Background-thread batched writer for the session_raw table.

    Usage (preferred context-manager form)::

        with RawWriter(db_path, session_id) as w:
            for sample in source:
                w.submit(sample)

    Parameters
    ----------
    db_path    : path to the SQLite database file
    session_id : alphanumeric session ID (must already exist in sessions table)
    batch_size : rows committed per transaction
    queue_size : internal queue depth
    """

    def __init__(
        self,
        db_path: str,
        session_id: str,
        *,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        queue_size: int = _DEFAULT_QUEUE_SIZE,
    ) -> None:
        self._db_path    = db_path
        self._session_id = session_id
        self._batch_size = batch_size
        self._q: queue.Queue = queue.Queue(maxsize=queue_size)
        self._error: Optional[Exception] = None
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"raw-writer-{session_id}",
        )
        self._thread.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit(self, sample: Sample) -> None:
        """Enqueue *sample* for insertion into session_raw."""
        if self._error is not None:
            raise RuntimeError("RawWriter thread failed") from self._error
        self._q.put(_SampleWithSession(self._session_id, sample))

    def flush(self) -> None:
        """Block until all enqueued samples have been committed."""
        evt = threading.Event()
        self._q.put(evt)
        evt.wait()
        if self._error is not None:
            raise RuntimeError("RawWriter thread failed") from self._error

    def close(self) -> None:
        """Flush remaining samples and stop the worker thread."""
        self._q.put(_SENTINEL)
        self._thread.join()
        if self._error is not None:
            raise RuntimeError("RawWriter thread failed") from self._error

    def __enter__(self) -> "RawWriter":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _run(self) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            batch: List = []
            while True:
                try:
                    item = self._q.get(timeout=0.05)
                except queue.Empty:
                    if batch:
                        _commit_raw_batch(conn, batch)
                        batch = []
                    continue

                if item is _SENTINEL:
                    if batch:
                        _commit_raw_batch(conn, batch)
                    break

                if isinstance(item, threading.Event):
                    if batch:
                        _commit_raw_batch(conn, batch)
                        batch = []
                    item.set()
                    continue

                batch.append(item)
                if len(batch) >= self._batch_size:
                    _commit_raw_batch(conn, batch)
                    batch = []

        except Exception as exc:
            log.exception("RawWriter worker error")
            self._error = exc
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Stream-In: DataWriter
# ---------------------------------------------------------------------------


class DataWriter:
    """
    Background-thread batched writer for the session_data table.

    Usage::

        with DataWriter(db_path, session_id) as w:
            for bfdict in corrected_values:
                w.submit(bfdict)

    Each *bfdict* must have keys:
        cam_id, side, time_s, bfi, bvi, contrast, mean
    """

    def __init__(
        self,
        db_path: str,
        session_id: str,
        *,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        queue_size: int = _DEFAULT_QUEUE_SIZE,
    ) -> None:
        self._db_path    = db_path
        self._session_id = session_id
        self._batch_size = batch_size
        self._q: queue.Queue = queue.Queue(maxsize=queue_size)
        self._error: Optional[Exception] = None
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"data-writer-{session_id}",
        )
        self._thread.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit(self, bfdict: dict) -> None:
        """
        Enqueue one BF value record for insertion into session_data.

        *bfdict* keys: cam_id, side, time_s, bfi, bvi, contrast, mean
        """
        if self._error is not None:
            raise RuntimeError("DataWriter thread failed") from self._error
        d = dict(bfdict)
        d["session_id"] = self._session_id
        self._q.put(d)

    def flush(self) -> None:
        evt = threading.Event()
        self._q.put(evt)
        evt.wait()
        if self._error is not None:
            raise RuntimeError("DataWriter thread failed") from self._error

    def close(self) -> None:
        self._q.put(_SENTINEL)
        self._thread.join()
        if self._error is not None:
            raise RuntimeError("DataWriter thread failed") from self._error

    def __enter__(self) -> "DataWriter":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _run(self) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            batch: List = []
            while True:
                try:
                    item = self._q.get(timeout=0.05)
                except queue.Empty:
                    if batch:
                        _commit_data_batch(conn, batch)
                        batch = []
                    continue

                if item is _SENTINEL:
                    if batch:
                        _commit_data_batch(conn, batch)
                    break

                if isinstance(item, threading.Event):
                    if batch:
                        _commit_data_batch(conn, batch)
                        batch = []
                    item.set()
                    continue

                batch.append(item)
                if len(batch) >= self._batch_size:
                    _commit_data_batch(conn, batch)
                    batch = []

        except Exception as exc:
            log.exception("DataWriter worker error")
            self._error = exc
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Stream-Out: RawReader
# ---------------------------------------------------------------------------


class RawReader:
    """
    Streams Sample objects for a session from session_raw in insertion order.

    Parameters
    ----------
    conn       : open sqlite3.Connection
    session_id : session to read
    """

    def __init__(self, conn: sqlite3.Connection, session_id: str) -> None:
        self._conn       = conn
        self._session_id = session_id

    def stream(self, side: Optional[str] = None) -> Iterator[Sample]:
        """
        Yield Sample objects.

        Parameters
        ----------
        side : 'left', 'right', or None (both)
        """
        sql = (
            "SELECT side, cam_id, frame_id, timestamp_s, hist, temp, sum, tcm, tcl, pdc "
            "FROM session_raw WHERE session_id=?"
        )
        params: list = [self._session_id]
        if side is not None:
            sql += " AND side=?"
            params.append(side)
        sql += " ORDER BY rowid"

        for row in self._conn.execute(sql, params):
            side_v, cam_id, frame_id, ts, hist_blob, temp, summ, tcm, tcl, pdc = row
            yield Sample(
                side     = side_v,
                cam_id   = np.uint32(cam_id),
                frame_id = np.uint32(frame_id),
                timestamp= np.float32(ts),
                hist     = _unpack_hist(hist_blob),
                temp     = np.float32(temp),
                summ     = np.uint64(summ),
                tcm      = np.float32(tcm),
                tcl      = np.float32(tcl),
                pdc      = np.float32(pdc),
            )

    def count(self, side: Optional[str] = None) -> int:
        """Return total raw samples for this session (optionally filtered by side)."""
        sql = "SELECT COUNT(*) FROM session_raw WHERE session_id=?"
        params: list = [self._session_id]
        if side is not None:
            sql += " AND side=?"
            params.append(side)
        return self._conn.execute(sql, params).fetchone()[0]


# ---------------------------------------------------------------------------
# Stream-Out: DataReader
# ---------------------------------------------------------------------------


class DataReader:
    """
    Streams BF value dicts for a session from session_data ordered by time.

    Parameters
    ----------
    conn       : open sqlite3.Connection
    session_id : session to read
    """

    def __init__(self, conn: sqlite3.Connection, session_id: str) -> None:
        self._conn       = conn
        self._session_id = session_id

    def stream(self, side: Optional[str] = None) -> Iterator[dict]:
        """
        Yield dicts with keys: cam_id, side, time_s, bfi, bvi, contrast, mean.

        Parameters
        ----------
        side : 'left', 'right', or None (both)
        """
        sql = (
            "SELECT cam_id, side, time_s, bfi, bvi, contrast, mean "
            "FROM session_data WHERE session_id=?"
        )
        params: list = [self._session_id]
        if side is not None:
            sql += " AND side=?"
            params.append(side)
        sql += " ORDER BY time_s, cam_id"

        for row in self._conn.execute(sql, params):
            cam_id, side_v, time_s, bfi, bvi, contrast, mean = row
            yield {
                "cam_id":   cam_id,
                "side":     side_v,
                "time_s":   time_s,
                "bfi":      bfi,
                "bvi":      bvi,
                "contrast": contrast,
                "mean":     mean,
            }

    def count(self, side: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) FROM session_data WHERE session_id=?"
        params: list = [self._session_id]
        if side is not None:
            sql += " AND side=?"
            params.append(side)
        return self._conn.execute(sql, params).fetchone()[0]


# ---------------------------------------------------------------------------
# CSV Export
# ---------------------------------------------------------------------------


def export_raw_to_csv(
    db_path: str,
    session_id: str,
    out_csv_path: str,
    include_hist: bool = True,
) -> int:
    """
    Export session_raw for *session_id* to a CSV file.

    CSV columns
    -----------
    session_id, side, cam_id, frame_id, timestamp_s, temp, sum, tcm, tcl, pdc
    [, hist[0] ... hist[1023]]

    Returns the number of data rows written.
    """
    fieldnames = [
        "session_id", "side", "cam_id", "frame_id", "timestamp_s",
        "temp", "sum", "tcm", "tcl", "pdc",
    ]
    if include_hist:
        fieldnames += [f"hist[{i}]" for i in range(_HIST_BINS)]

    conn = open_db(db_path)
    rows_written = 0
    try:
        cursor = conn.execute(
            "SELECT side, cam_id, frame_id, timestamp_s, hist, temp, sum, tcm, tcl, pdc "
            "FROM session_raw WHERE session_id=? ORDER BY rowid",
            (session_id,),
        )
        with open(out_csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for row in cursor:
                side_v, cam_id, frame_id, ts, hist_blob, temp, summ, tcm, tcl, pdc = row
                d: dict = {
                    "session_id": session_id,
                    "side":        side_v,
                    "cam_id":      int(cam_id),
                    "frame_id":    int(frame_id),
                    "timestamp_s": float(ts),
                    "temp":        float(temp),
                    "sum":         int(summ),
                    "tcm":         float(tcm),
                    "tcl":         float(tcl),
                    "pdc":         float(pdc),
                }
                if include_hist:
                    hist = _unpack_hist(hist_blob)
                    for i, v in enumerate(hist):
                        d[f"hist[{i}]"] = int(v)
                writer.writerow(d)
                rows_written += 1
    finally:
        conn.close()
    return rows_written


def export_data_to_csv(
    db_path: str,
    session_id: str,
    out_csv_path: str,
) -> int:
    """
    Export session_data (computed BF values) for *session_id* to a CSV file.

    CSV columns: session_id, cam_id, side, time_s, bfi, bvi, contrast, mean

    Returns the number of data rows written.
    """
    fieldnames = [
        "session_id", "cam_id", "side", "time_s",
        "bfi", "bvi", "contrast", "mean",
    ]
    conn = open_db(db_path)
    rows_written = 0
    try:
        cursor = conn.execute(
            "SELECT cam_id, side, time_s, bfi, bvi, contrast, mean "
            "FROM session_data WHERE session_id=? ORDER BY time_s, cam_id",
            (session_id,),
        )
        with open(out_csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for row in cursor:
                cam_id, side_v, time_s, bfi, bvi, contrast, mean = row
                writer.writerow({
                    "session_id": session_id,
                    "cam_id":     int(cam_id),
                    "side":       side_v,
                    "time_s":     float(time_s),
                    "bfi":        float(bfi),
                    "bvi":        float(bvi),
                    "contrast":   float(contrast),
                    "mean":       float(mean),
                })
                rows_written += 1
    finally:
        conn.close()
    return rows_written


# ---------------------------------------------------------------------------
# Module self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import tempfile
    import time as _time

    logging.basicConfig(level=logging.INFO)

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        _db = f.name

    try:
        _conn = open_db(_db)
        _sid = create_session(
            _conn,
            session_id="testABC1",
            session_start=_time.time(),
            session_notes="self-test",
            session_meta={"fps": 40, "mask": 0xFF},
        )
        print(f"Created session_id={_sid}")

        _N = 10
        _hist_base = np.arange(_HIST_BINS, dtype=np.uint32)
        with RawWriter(_db, _sid, batch_size=5) as _rw:
            for _i in range(_N):
                _h = _hist_base + _i
                _rw.submit(
                    Sample(
                        side     = "left",
                        cam_id   = np.uint32(0),
                        frame_id = np.uint32(_i),
                        timestamp= np.float32(_i * 0.025),
                        hist     = _h,
                        temp     = np.float32(36.5),
                        summ     = np.uint64(int(_h.sum())),
                    )
                )
        print(f"Wrote {_N} raw samples")

        with DataWriter(_db, _sid, batch_size=5) as _dw:
            for _i in range(_N):
                _dw.submit({
                    "cam_id": 0, "side": "left", "time_s": _i * 0.025,
                    "bfi": 5.0, "bvi": 6.0, "contrast": 0.3, "mean": 120.0,
                })
        print(f"Wrote {_N} data rows")

        _raw_reader = RawReader(_conn, _sid)
        _raw_samples = list(_raw_reader.stream())
        assert len(_raw_samples) == _N, f"Expected {_N} raw, got {len(_raw_samples)}"
        print(f"Read back {len(_raw_samples)} raw samples - OK")

        _data_reader = DataReader(_conn, _sid)
        _data_rows = list(_data_reader.stream())
        assert len(_data_rows) == _N, f"Expected {_N} data, got {len(_data_rows)}"
        print(f"Read back {len(_data_rows)} data rows - OK")

        _out_raw_csv  = _db.replace(".sqlite", "_raw.csv")
        _out_data_csv = _db.replace(".sqlite", "_data.csv")
        _r = export_raw_to_csv(_db, _sid, _out_raw_csv, include_hist=False)
        _d = export_data_to_csv(_db, _sid, _out_data_csv)
        print(f"Exported {_r} raw rows → {_out_raw_csv}")
        print(f"Exported {_d} data rows → {_out_data_csv}")

        _conn.close()
        print("Self-test PASSED")
    finally:
        os.remove(_db)
        for _p in [_db.replace(".sqlite", "_raw.csv"), _db.replace(".sqlite", "_data.csv")]:
            if os.path.exists(_p):
                os.remove(_p)
