"""
bfstorage.py - SQLite-backed sample storage for OpenMotion bloodflow.

Schema
------
sessions : session_id, uid, started_at, notes, sensor_config (JSON)
samples  : sample_id, session_id, sequence, timestamp_s, cam_id,
           frame_id, side, temperature, summ, hist (BLOB, packed uint32)

Stream-In
---------
DatabaseWriter - background-thread writer with batched inserts and
backpressure.  Call submit(sample) from the producer thread; the worker
flushes in configurable batch sizes.

Stream-Out
----------
DatabaseReader - cursor-based generator that yields Sample objects in
sequence order with optional throttling and offset/limit pagination.

Export
------
export_session_to_csv() - write all samples for a session to a CSV file.

Legacy compatibility
--------------------
SamplesDBsqlite - drop-in replacement for the old class of the same name
that serialised entire session blobs using pickle.  The old insert()/
retrieve() API is preserved; internally it uses DatabaseWriter/Reader.
"""

from __future__ import annotations

import csv
import json
import logging
import queue
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Generator, List, Optional

import numpy as np

from api.session_samples import Sample

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HIST_BINS = 1024
_HIST_DTYPE = np.uint32

_DEFAULT_BATCH_SIZE = 500
_DEFAULT_QUEUE_SIZE = 4096

_SENTINEL = object()  # signals the worker thread to flush and stop

# ---------------------------------------------------------------------------
# Histogram helpers
# ---------------------------------------------------------------------------


def _pack_hist(hist) -> bytes:
    """Return 1024 uint32 values packed as little-endian bytes (4096 bytes)."""
    arr = np.asarray(hist, dtype=np.uint32)
    if arr.size != _HIST_BINS:
        raise ValueError(f"hist must have {_HIST_BINS} bins, got {arr.size}")
    return arr.tobytes()


def _unpack_hist(blob: bytes) -> np.ndarray:
    """Restore a histogram ndarray from packed bytes."""
    return np.frombuffer(blob, dtype=np.uint32).copy()


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    uid           TEXT    NOT NULL,
    started_at    TEXT    NOT NULL,
    notes         TEXT,
    sensor_config TEXT    NOT NULL
);
"""

_DDL_SAMPLES = """
CREATE TABLE IF NOT EXISTS samples (
    sample_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(session_id),
    sequence    INTEGER NOT NULL,
    timestamp_s REAL    NOT NULL,
    cam_id      INTEGER NOT NULL,
    frame_id    INTEGER NOT NULL,
    side        INTEGER NOT NULL,
    temperature REAL    NOT NULL,
    summ        INTEGER NOT NULL,
    hist        BLOB    NOT NULL
);
"""

_DDL_SAMPLES_IDX = """
CREATE INDEX IF NOT EXISTS idx_samples_session_seq
    ON samples (session_id, sequence);
"""

# ---------------------------------------------------------------------------
# Schema init and migration
# ---------------------------------------------------------------------------


def _is_legacy_schema(conn: sqlite3.Connection) -> bool:
    """Return True when the DB has the old single-blob schema."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='samples'"
    )
    if cur.fetchone() is None:
        return False
    cur = conn.execute("PRAGMA table_info(samples)")
    cols = {row[1] for row in cur.fetchall()}
    return "data" in cols and "sample_id" not in cols


def _migrate_legacy(conn: sqlite3.Connection) -> None:
    """
    Rename the old samples table to samples_legacy so no data is lost,
    then the caller creates the new schema.

    Deserialisation of pickled blobs is not attempted because the dependency
    on the exact Sample NamedTuple layout is fragile.  Re-ingest from the
    original CSV files to restore data in the new format.
    """
    log.warning(
        "Legacy schema detected - renaming 'samples' to 'samples_legacy'. "
        "Re-ingest from CSV source files to restore data in the new format."
    )
    conn.execute("ALTER TABLE samples RENAME TO samples_legacy")
    conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    """Ensure WAL journal mode and current schema exist, migrating if needed."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if _is_legacy_schema(conn):
        _migrate_legacy(conn)
    conn.executescript(_DDL_SESSIONS + _DDL_SAMPLES + _DDL_SAMPLES_IDX)
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
    uid: Optional[uuid.UUID] = None,
    started_at: Optional[datetime] = None,
    notes: Optional[str] = None,
    sensor_config: Optional[dict] = None,
) -> int:
    """Insert a new session row and return its INTEGER PRIMARY KEY."""
    uid = uid or uuid.uuid4()
    started_at = started_at or datetime.now(timezone.utc)
    config_json = json.dumps(sensor_config if sensor_config is not None else {})
    cur = conn.execute(
        "INSERT INTO sessions (uid, started_at, notes, sensor_config) VALUES (?, ?, ?, ?)",
        (str(uid), started_at.isoformat(), notes, config_json),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Stream-In: DatabaseWriter
# ---------------------------------------------------------------------------


class DatabaseWriter:
    """
    Background-thread writer that batches Sample inserts into the DB.

    The worker thread opens its own SQLite connection in WAL + NORMAL-
    synchronous mode, eliminating cross-thread connection sharing issues.

    Parameters
    ----------
    db_path      : path to the SQLite database file
    session_id   : session row that samples belong to
    batch_size   : rows committed per transaction (100-1000 recommended)
    queue_size   : maximum pending samples before backpressure activates
    drop_on_full : True  - submit() drops the sample and returns False when
                          the internal queue is full.
                   False - submit() returns False so the caller can decide.

    Usage
    -----
    Prefer the context-manager form so close() is always called::

        with DatabaseWriter(db_path, session_id) as w:
            for sample in source:
                w.submit(sample)
    """

    def __init__(
        self,
        db_path: str,
        session_id: int,
        *,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        queue_size: int = _DEFAULT_QUEUE_SIZE,
        drop_on_full: bool = False,
    ) -> None:
        self._db_path = db_path
        self._session_id = session_id
        self._batch_size = batch_size
        self._drop_on_full = drop_on_full
        self._q: queue.Queue = queue.Queue(maxsize=queue_size)
        self._seq_lock = threading.Lock()
        self._error: Optional[Exception] = None

        # Determine starting sequence so resuming an existing session works.
        # Use a short-lived connection before the worker thread starts to
        # avoid any cross-thread sharing of the connection object.
        _tmp = sqlite3.connect(db_path)
        row = _tmp.execute(
            "SELECT COALESCE(MAX(sequence) + 1, 0) FROM samples WHERE session_id=?",
            (session_id,),
        ).fetchone()
        _tmp.close()
        self._sequence: int = int(row[0])

        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"db-writer-sid{session_id}",
        )
        self._thread.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit(self, sample: Sample) -> bool:
        """
        Enqueue *sample* for insertion.

        Returns True when the sample was accepted.
        Returns False when the queue is full (sample is dropped or caller
        should apply backpressure depending on *drop_on_full*).
        Raises RuntimeError if the writer thread has already failed.
        """
        if self._error is not None:
            raise RuntimeError("DatabaseWriter thread failed") from self._error
        with self._seq_lock:
            seq = self._sequence
            self._sequence += 1
        try:
            self._q.put_nowait((seq, sample))
            return True
        except queue.Full:
            if not self._drop_on_full:
                log.warning(
                    "DatabaseWriter queue full; dropped sample with seq=%d", seq
                )
            return False

    def flush(self) -> None:
        """Block until all currently enqueued samples have been committed."""
        evt = threading.Event()
        self._q.put(evt)
        evt.wait()
        if self._error is not None:
            raise RuntimeError("DatabaseWriter thread failed") from self._error

    def close(self) -> None:
        """Flush all remaining samples and stop the background thread."""
        self._q.put(_SENTINEL)
        self._thread.join()
        if self._error is not None:
            raise RuntimeError("DatabaseWriter thread failed") from self._error

    def __enter__(self) -> "DatabaseWriter":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    def _run(self) -> None:
        conn = sqlite3.connect(self._db_path)
        session_id = self._session_id
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")

            batch: List[tuple] = []
            while True:
                try:
                    item = self._q.get(timeout=0.05)
                except queue.Empty:
                    if batch:
                        _commit_batch(conn, session_id, batch)
                        batch = []
                    continue

                if item is _SENTINEL:
                    if batch:
                        _commit_batch(conn, session_id, batch)
                    break

                if isinstance(item, threading.Event):
                    if batch:
                        _commit_batch(conn, session_id, batch)
                        batch = []
                    item.set()
                    continue

                batch.append(item)
                if len(batch) >= self._batch_size:
                    _commit_batch(conn, session_id, batch)
                    batch = []

        except Exception as exc:
            log.exception("DatabaseWriter worker error")
            self._error = exc
        finally:
            conn.close()


def _commit_batch(
    conn: sqlite3.Connection, session_id: int, batch: List[tuple]
) -> None:
    """Insert a batch of (sequence, Sample) tuples in a single transaction."""
    rows = [
        (
            session_id,
            seq,
            float(s.timestamp),
            int(s.cam_id),
            int(s.frame_id),
            int(s.side),
            float(s.temp),
            int(s.summ),
            _pack_hist(s.hist),
        )
        for seq, s in batch
    ]
    conn.executemany(
        "INSERT INTO samples "
        "(session_id, sequence, timestamp_s, cam_id, frame_id, side, "
        " temperature, summ, hist) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    log.debug("Committed %d sample(s) for session_id=%d", len(rows), session_id)


# ---------------------------------------------------------------------------
# Stream-Out: DatabaseReader
# ---------------------------------------------------------------------------


class DatabaseReader:
    """
    Streams Sample objects for a session in ascending sequence order.

    Uses cursor-based pagination internally so large sessions are never
    fully loaded into memory.

    Parameters
    ----------
    conn        : open sqlite3.Connection
    session_id  : session to read
    page_size   : rows fetched per SQL round-trip
    throttle_s  : if > 0, sleep between samples to approximate original timing
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        session_id: int,
        *,
        page_size: int = 1000,
        throttle_s: float = 0.0,
    ) -> None:
        self._conn = conn
        self._session_id = session_id
        self._page_size = page_size
        self._throttle_s = throttle_s

    def stream(
        self,
        *,
        offset: int = 0,
        limit: Optional[int] = None,
    ) -> Generator[Sample, None, None]:
        """
        Yield Sample objects in sequence order.

        Parameters
        ----------
        offset : rows to skip from the start of the session
        limit  : maximum rows to yield (None = no limit)
        """
        import time as _time

        max_rows = limit if limit is not None else 2 ** 63 - 1

        # Determine the cursor starting position.
        if offset > 0:
            row = self._conn.execute(
                "SELECT sequence FROM samples "
                "WHERE session_id=? ORDER BY sequence LIMIT 1 OFFSET ?",
                (self._session_id, offset),
            ).fetchone()
            if row is None:
                return
            cursor_seq = row[0] - 1
        else:
            cursor_seq = -1

        fetched = 0
        first_db_ts: Optional[float] = None
        first_yield_t: Optional[float] = None

        while fetched < max_rows:
            page = min(self._page_size, max_rows - fetched)
            db_rows = self._conn.execute(
                "SELECT sequence, timestamp_s, cam_id, frame_id, side, "
                "       temperature, summ, hist "
                "FROM   samples "
                "WHERE  session_id=? AND sequence > ? "
                "ORDER  BY sequence "
                "LIMIT  ?",
                (self._session_id, cursor_seq, page),
            ).fetchall()

            if not db_rows:
                break

            for db_row in db_rows:
                seq, ts, cam_id, frame_id, side, temp, summ, hist_blob = db_row
                sample = Sample(
                    side=np.uint32(side),
                    cam_id=np.uint32(cam_id),
                    frame_id=np.uint32(frame_id),
                    timestamp=np.float32(ts),
                    hist=_unpack_hist(hist_blob),
                    temp=np.float32(temp),
                    summ=np.uint64(summ),
                )

                if self._throttle_s > 0:
                    now = _time.monotonic()
                    if first_db_ts is None:
                        first_db_ts = ts
                        first_yield_t = now
                    else:
                        delay = (first_yield_t + (ts - first_db_ts)) - now  # type: ignore[operator]
                        if delay > 0:
                            _time.sleep(delay)

                yield sample
                cursor_seq = seq
                fetched += 1
                if fetched >= max_rows:
                    return

    def count(self) -> int:
        """Return the total number of samples stored for this session."""
        return self._conn.execute(
            "SELECT COUNT(*) FROM samples WHERE session_id=?",
            (self._session_id,),
        ).fetchone()[0]


# ---------------------------------------------------------------------------
# CSV Export
# ---------------------------------------------------------------------------


def export_session_to_csv(
    db_path: str,
    session_id: int,
    out_csv_path: str,
    include_hist: bool = True,
) -> int:
    """
    Export every sample for *session_id* to a CSV file.

    Returns the number of data rows written (header not counted).

    CSV columns
    -----------
    session_id, sequence, timestamp_s, cam_id, frame_id, side,
    temperature, summ [, hist[0] ... hist[1023]]
    """
    fieldnames = [
        "session_id", "sequence", "timestamp_s", "cam_id",
        "frame_id", "side", "temperature", "summ",
    ]
    if include_hist:
        fieldnames += [f"hist[{i}]" for i in range(_HIST_BINS)]

    conn = open_db(db_path)
    rows_written = 0
    try:
        cursor = conn.execute(
            "SELECT sequence, timestamp_s, cam_id, frame_id, side, "
            "       temperature, summ, hist "
            "FROM   samples "
            "WHERE  session_id=? "
            "ORDER  BY sequence",
            (session_id,),
        )
        with open(out_csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for db_row in cursor:
                seq, ts, cam_id, frame_id, side, temp, summ, hist_blob = db_row
                row: dict = {
                    "session_id": session_id,
                    "sequence": seq,
                    "timestamp_s": float(ts),
                    "cam_id": int(cam_id),
                    "frame_id": int(frame_id),
                    "side": int(side),
                    "temperature": float(temp),
                    "summ": int(summ),
                }
                if include_hist:
                    hist = _unpack_hist(hist_blob)
                    for i, v in enumerate(hist):
                        row[f"hist[{i}]"] = int(v)
                writer.writerow(row)
                rows_written += 1
    finally:
        conn.close()

    return rows_written


# ---------------------------------------------------------------------------
# Legacy compatibility shim
# ---------------------------------------------------------------------------

_SAFE_TABLE_NAME = re.compile(r"^\w+$")


class SamplesDBsqlite:
    """
    Drop-in replacement for the old SamplesDBsqlite that stored entire
    sessions as a single pickled BLOB.

    The public ``insert(session_id, samples_list)`` /
    ``retrieve(session_id)`` API is preserved.  Internally every sample is
    stored as an individual row using DatabaseWriter/DatabaseReader so
    sessions are never held fully in memory.
    """

    def __init__(self, db_file: str, uid: uuid.UUID) -> None:
        self.db_file = db_file
        self.uid = uid
        # Lightweight connection used only for metadata (session lookup /
        # creation).  DatabaseWriter opens its own write connection.
        self.conn = open_db(db_file)

    def __del__(self) -> None:
        conn = getattr(self, "conn", None)
        if conn:
            conn.close()

    # ------------------------------------------------------------------
    # Public API (backward-compatible with original SamplesDBsqlite)
    # ------------------------------------------------------------------

    def insert(self, session_id: int, samples_list: list) -> None:
        """Store all samples in *samples_list* for this session."""
        real_sid = self._ensure_session()
        with DatabaseWriter(self.db_file, real_sid) as writer:
            for sample in samples_list:
                writer.submit(sample)
        log.info(
            "Inserted %d sample(s) for session uid=%s (db_session_id=%d)",
            len(samples_list),
            self.uid,
            real_sid,
        )

    def retrieve(self, session_id: int) -> Optional[list]:
        """Return all samples for this session as a list, or None."""
        row = self.conn.execute(
            "SELECT session_id FROM sessions WHERE uid=? LIMIT 1",
            (str(self.uid),),
        ).fetchone()
        if row is None:
            return None
        reader = DatabaseReader(self.conn, row[0])
        results = list(reader.stream())
        return results if results else None

    def view_content(self) -> None:
        """Print a row-count summary for every table in the database."""
        cur = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        for (tbl,) in cur.fetchall():
            if not _SAFE_TABLE_NAME.match(tbl):
                log.warning("Skipping table with unexpected name: %r", tbl)
                continue
            count = self.conn.execute(
                f"SELECT COUNT(*) FROM {tbl}"  # noqa: S608
            ).fetchone()[0]
            print(f"  {tbl}: {count} row(s)")

    # ------------------------------------------------------------------

    def _ensure_session(self) -> int:
        """Return the session_id for self.uid, creating the row if absent."""
        row = self.conn.execute(
            "SELECT session_id FROM sessions WHERE uid=? LIMIT 1",
            (str(self.uid),),
        ).fetchone()
        if row:
            return row[0]
        return create_session(self.conn, uid=self.uid)


# ---------------------------------------------------------------------------
# Module self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import tempfile

    logging.basicConfig(level=logging.INFO)

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        _db = f.name

    try:
        _conn = open_db(_db)
        _sid = create_session(_conn, notes="self-test", sensor_config={"fps": 40})
        print(f"Created session_id={_sid}")

        _N = 300
        _hist_base = np.arange(_HIST_BINS, dtype=np.uint32)
        with DatabaseWriter(_db, _sid, batch_size=100) as _w:
            for _i in range(_N):
                _h = _hist_base + _i
                _w.submit(
                    Sample(
                        side=np.uint32(0),
                        cam_id=np.uint32(0),
                        frame_id=np.uint32(_i),
                        timestamp=np.float32(_i * 0.025),
                        hist=_h,
                        temp=np.float32(36.5),
                        summ=np.uint64(int(_h.sum())),
                    )
                )
        print(f"Wrote {_N} samples")

        _reader = DatabaseReader(_conn, _sid)
        _samples = list(_reader.stream())
        assert len(_samples) == _N, f"Expected {_N}, got {len(_samples)}"
        print(f"Read back {len(_samples)} samples - OK")

        _out_csv = _db.replace(".sqlite", ".csv")
        _rows = export_session_to_csv(_db, _sid, _out_csv, include_hist=False)
        print(f"Exported {_rows} rows to {_out_csv}")
        _conn.close()
    finally:
        os.remove(_db)
        _csv_path = _db.replace(".sqlite", ".csv")
        if os.path.exists(_csv_path):
            os.remove(_csv_path)

    print("Self-test passed.")
