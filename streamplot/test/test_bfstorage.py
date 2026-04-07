"""
test_bfstorage.py - Unit tests for the new bfstorage module.

Run with:
    python -m pytest test_bfstorage.py -v

Or with timing info:
    python -m pytest test_bfstorage.py -v --tb=short -q
"""

import csv
import json
import os
import sqlite3
import tempfile
import time
import uuid

import numpy as np
import pytest

from session_samples import Sample
from api import bfstorage as bs

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HIST_BINS = 1024


def make_sample(seq: int, *, cam_id: int = 0, side: int = 0) -> Sample:
    """Return a deterministic Sample whose histogram depends on *seq*."""
    # np.arange with uint32 dtype wraps naturally at 2^32
    hist = np.arange(seq, seq + _HIST_BINS, dtype=np.uint32)
    return Sample(
        side=np.uint32(side),
        cam_id=np.uint32(cam_id),
        frame_id=np.uint32(seq),
        timestamp=np.float32(seq * 0.025),
        hist=hist,
        temp=np.float32(36.5),
        summ=np.uint64(int(hist.sum())),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path):
    """Yield a path to a fresh (empty) database file."""
    return str(tmp_path / "test.sqlite")


@pytest.fixture
def conn(tmp_db):
    """Yield an open, initialised connection that is closed after the test."""
    c = bs.open_db(tmp_db)
    yield c
    c.close()


@pytest.fixture
def session(conn):
    """Yield (conn, session_id) for a freshly created session."""
    sid = bs.create_session(conn, sensor_config={"fps": 40})
    return conn, sid


# ---------------------------------------------------------------------------
# Schema / init
# ---------------------------------------------------------------------------


class TestSchemaInit:
    def test_tables_created(self, conn):
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        names = {r[0] for r in cur.fetchall()}
        assert "sessions" in names
        assert "samples" in names

    def test_wal_mode(self, conn):
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_init_idempotent(self, conn):
        """Calling init_db twice must not raise."""
        bs.init_db(conn)
        bs.init_db(conn)

    def test_legacy_migration(self, tmp_db):
        """A DB with the old single-blob schema is migrated without error."""
        old = sqlite3.connect(tmp_db)
        old.execute(
            """
            CREATE TABLE samples (
                session_id INTEGER PRIMARY KEY,
                id         BLOB NOT NULL,
                data       BLOB NOT NULL
            )
            """
        )
        old.commit()
        old.close()

        new = bs.open_db(tmp_db)
        names = {
            r[0]
            for r in new.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        new.close()

        assert "samples_legacy" in names, "Old table should be renamed"
        assert "sessions" in names
        assert "samples" in names


# ---------------------------------------------------------------------------
# Session creation
# ---------------------------------------------------------------------------


class TestSessionCreation:
    def test_returns_positive_integer(self, conn):
        sid = bs.create_session(conn, sensor_config={})
        assert isinstance(sid, int) and sid > 0

    def test_fields_stored_correctly(self, conn):
        uid = uuid.uuid4()
        sid = bs.create_session(
            conn, uid=uid, notes="unit test", sensor_config={"fps": 40}
        )
        row = conn.execute(
            "SELECT uid, notes, sensor_config FROM sessions WHERE session_id=?",
            (sid,),
        ).fetchone()
        assert row[0] == str(uid)
        assert row[1] == "unit test"
        assert json.loads(row[2])["fps"] == 40

    def test_started_at_is_iso8601(self, conn):
        from datetime import datetime
        sid = bs.create_session(conn)
        val = conn.execute(
            "SELECT started_at FROM sessions WHERE session_id=?", (sid,)
        ).fetchone()[0]
        # Must parse without exception
        datetime.fromisoformat(val)

    def test_multiple_sessions_have_unique_ids(self, conn):
        ids = [bs.create_session(conn) for _ in range(5)]
        assert len(set(ids)) == 5


# ---------------------------------------------------------------------------
# DatabaseWriter
# ---------------------------------------------------------------------------


class TestDatabaseWriter:
    N = 250

    def test_insert_correct_count(self, tmp_db):
        conn = bs.open_db(tmp_db)
        sid = bs.create_session(conn)
        conn.close()

        with bs.DatabaseWriter(tmp_db, sid, batch_size=50) as w:
            for i in range(self.N):
                assert w.submit(make_sample(i)) is True

        conn = bs.open_db(tmp_db)
        count = conn.execute(
            "SELECT COUNT(*) FROM samples WHERE session_id=?", (sid,)
        ).fetchone()[0]
        conn.close()
        assert count == self.N

    def test_sequence_is_monotonically_increasing(self, tmp_db):
        conn = bs.open_db(tmp_db)
        sid = bs.create_session(conn)
        conn.close()

        with bs.DatabaseWriter(tmp_db, sid, batch_size=50) as w:
            for i in range(self.N):
                w.submit(make_sample(i))

        conn = bs.open_db(tmp_db)
        seqs = [
            r[0]
            for r in conn.execute(
                "SELECT sequence FROM samples WHERE session_id=? ORDER BY sequence",
                (sid,),
            )
        ]
        conn.close()
        assert seqs == list(range(self.N))

    def test_histogram_stored_and_recovered_intact(self, tmp_db):
        conn = bs.open_db(tmp_db)
        sid = bs.create_session(conn)
        conn.close()

        sample = make_sample(7)
        with bs.DatabaseWriter(tmp_db, sid) as w:
            w.submit(sample)

        conn = bs.open_db(tmp_db)
        blob = conn.execute(
            "SELECT hist FROM samples WHERE session_id=?", (sid,)
        ).fetchone()[0]
        conn.close()

        recovered = bs._unpack_hist(blob)
        np.testing.assert_array_equal(recovered, sample.hist)

    def test_flush_makes_rows_visible_immediately(self, tmp_db):
        conn = bs.open_db(tmp_db)
        sid = bs.create_session(conn)
        conn.close()

        writer = bs.DatabaseWriter(tmp_db, sid, batch_size=1000)
        for i in range(60):
            writer.submit(make_sample(i))
        writer.flush()  # must commit even though batch_size not reached

        conn = bs.open_db(tmp_db)
        count = conn.execute(
            "SELECT COUNT(*) FROM samples WHERE session_id=?", (sid,)
        ).fetchone()[0]
        conn.close()
        assert count == 60
        writer.close()

    def test_context_manager_calls_close(self, tmp_db):
        conn = bs.open_db(tmp_db)
        sid = bs.create_session(conn)
        conn.close()

        with bs.DatabaseWriter(tmp_db, sid) as w:
            w.submit(make_sample(0))

        conn = bs.open_db(tmp_db)
        count = conn.execute(
            "SELECT COUNT(*) FROM samples WHERE session_id=?", (sid,)
        ).fetchone()[0]
        conn.close()
        assert count == 1

    def test_resume_sequence_after_reopen(self, tmp_db):
        """A new writer for the same session must continue the sequence."""
        conn = bs.open_db(tmp_db)
        sid = bs.create_session(conn)
        conn.close()

        with bs.DatabaseWriter(tmp_db, sid, batch_size=10) as w:
            for i in range(10):
                w.submit(make_sample(i))

        with bs.DatabaseWriter(tmp_db, sid, batch_size=10) as w:
            for i in range(10):
                w.submit(make_sample(i + 10))

        conn = bs.open_db(tmp_db)
        seqs = [
            r[0]
            for r in conn.execute(
                "SELECT sequence FROM samples WHERE session_id=? ORDER BY sequence",
                (sid,),
            )
        ]
        conn.close()
        assert seqs == list(range(20)), f"Gaps or duplicates in sequences: {seqs}"

    def test_backpressure_drop_on_full(self, tmp_db):
        """With a tiny queue and drop_on_full=True, submit returns False on overflow."""
        conn = bs.open_db(tmp_db)
        sid = bs.create_session(conn)
        conn.close()

        writer = bs.DatabaseWriter(
            tmp_db, sid, queue_size=3, drop_on_full=True, batch_size=2
        )
        results = [writer.submit(make_sample(i)) for i in range(300)]
        writer.close()

        conn = bs.open_db(tmp_db)
        inserted = conn.execute(
            "SELECT COUNT(*) FROM samples WHERE session_id=?", (sid,)
        ).fetchone()[0]
        conn.close()

        dropped = results.count(False)
        # Every sample was either inserted or dropped; none lost silently.
        assert inserted + dropped == 300
        assert inserted <= 300

    def test_insert_performance_10k(self, tmp_db):
        """10 000 rows with 4096-byte hists must insert in under 30 s."""
        conn = bs.open_db(tmp_db)
        sid = bs.create_session(conn)
        conn.close()

        N = 10_000
        t0 = time.perf_counter()
        # queue_size > N so no drops occur; we are measuring raw write throughput
        with bs.DatabaseWriter(tmp_db, sid, batch_size=500, queue_size=N + 100) as w:
            for i in range(N):
                w.submit(make_sample(i))
        elapsed = time.perf_counter() - t0

        conn = bs.open_db(tmp_db)
        count = conn.execute(
            "SELECT COUNT(*) FROM samples WHERE session_id=?", (sid,)
        ).fetchone()[0]
        conn.close()

        assert count == N
        assert elapsed < 30.0, f"Too slow: {elapsed:.2f}s for {N} rows"
        print(f"\n  10k insert time: {elapsed:.2f}s ({N / elapsed:.0f} rows/s)")


# ---------------------------------------------------------------------------
# DatabaseReader
# ---------------------------------------------------------------------------


class TestDatabaseReader:
    N = 120

    @pytest.fixture
    def populated(self, tmp_db):
        """Database with one session containing N samples."""
        conn = bs.open_db(tmp_db)
        sid = bs.create_session(conn)
        conn.close()

        with bs.DatabaseWriter(tmp_db, sid, batch_size=50) as w:
            for i in range(self.N):
                w.submit(make_sample(i))

        conn = bs.open_db(tmp_db)
        yield conn, sid
        conn.close()

    def test_stream_yields_all_rows(self, populated):
        conn, sid = populated
        samples = list(bs.DatabaseReader(conn, sid).stream())
        assert len(samples) == self.N

    def test_stream_order_is_correct(self, populated):
        conn, sid = populated
        samples = list(bs.DatabaseReader(conn, sid).stream())
        frame_ids = [int(s.frame_id) for s in samples]
        assert frame_ids == sorted(frame_ids), "Samples not in sequence order"

    def test_stream_with_offset(self, populated):
        conn, sid = populated
        samples = list(bs.DatabaseReader(conn, sid).stream(offset=10))
        assert len(samples) == self.N - 10
        assert int(samples[0].frame_id) == 10

    def test_stream_with_limit(self, populated):
        conn, sid = populated
        samples = list(bs.DatabaseReader(conn, sid).stream(limit=30))
        assert len(samples) == 30

    def test_stream_with_offset_and_limit(self, populated):
        conn, sid = populated
        samples = list(bs.DatabaseReader(conn, sid).stream(offset=5, limit=15))
        assert len(samples) == 15
        assert int(samples[0].frame_id) == 5
        assert int(samples[-1].frame_id) == 19

    def test_stream_offset_past_end_yields_nothing(self, populated):
        conn, sid = populated
        samples = list(bs.DatabaseReader(conn, sid).stream(offset=self.N + 100))
        assert samples == []

    def test_count(self, populated):
        conn, sid = populated
        assert bs.DatabaseReader(conn, sid).count() == self.N

    def test_histogram_roundtrip(self, populated):
        conn, sid = populated
        original = make_sample(0)
        recovered = next(bs.DatabaseReader(conn, sid).stream())
        np.testing.assert_array_equal(recovered.hist, original.hist)

    def test_two_sessions_independent(self, tmp_db):
        """Rows from session A must not appear when reading session B."""
        conn = bs.open_db(tmp_db)
        sid_a = bs.create_session(conn)
        sid_b = bs.create_session(conn)
        conn.close()

        with bs.DatabaseWriter(tmp_db, sid_a, batch_size=50) as w:
            for i in range(20):
                w.submit(make_sample(i, side=1))

        with bs.DatabaseWriter(tmp_db, sid_b, batch_size=50) as w:
            for i in range(10):
                w.submit(make_sample(i, side=2))

        conn = bs.open_db(tmp_db)
        a_samples = list(bs.DatabaseReader(conn, sid_a).stream())
        b_samples = list(bs.DatabaseReader(conn, sid_b).stream())
        conn.close()

        assert len(a_samples) == 20
        assert len(b_samples) == 10
        assert all(int(s.side) == 1 for s in a_samples)
        assert all(int(s.side) == 2 for s in b_samples)


# ---------------------------------------------------------------------------
# CSV Export
# ---------------------------------------------------------------------------


class TestCSVExport:
    N = 60

    @pytest.fixture
    def db_with_session(self, tmp_db):
        """Populated DB; yields (db_path, session_id)."""
        conn = bs.open_db(tmp_db)
        sid = bs.create_session(conn, sensor_config={"fps": 40})
        conn.close()

        with bs.DatabaseWriter(tmp_db, sid, batch_size=50) as w:
            for i in range(self.N):
                w.submit(make_sample(i))

        return tmp_db, sid

    def test_returns_correct_row_count(self, db_with_session, tmp_path):
        db, sid = db_with_session
        out = str(tmp_path / "export.csv")
        written = bs.export_session_to_csv(db, sid, out)
        assert written == self.N

    def test_headers_with_hist(self, db_with_session, tmp_path):
        db, sid = db_with_session
        out = str(tmp_path / "export.csv")
        bs.export_session_to_csv(db, sid, out, include_hist=True)
        with open(out, newline="", encoding="utf-8") as fh:
            headers = next(csv.reader(fh))
        expected_base = [
            "session_id", "sequence", "timestamp_s", "cam_id",
            "frame_id", "side", "temperature", "summ",
        ]
        assert headers[:8] == expected_base
        assert headers[8] == "hist[0]"
        assert headers[-1] == f"hist[{_HIST_BINS - 1}]"
        assert len(headers) == 8 + _HIST_BINS

    def test_headers_without_hist(self, db_with_session, tmp_path):
        db, sid = db_with_session
        out = str(tmp_path / "export.csv")
        bs.export_session_to_csv(db, sid, out, include_hist=False)
        with open(out, newline="", encoding="utf-8") as fh:
            headers = next(csv.reader(fh))
        assert headers == [
            "session_id", "sequence", "timestamp_s", "cam_id",
            "frame_id", "side", "temperature", "summ",
        ]

    def test_hist_values_match_original(self, db_with_session, tmp_path):
        db, sid = db_with_session
        out = str(tmp_path / "export.csv")
        bs.export_session_to_csv(db, sid, out, include_hist=True)
        with open(out, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == self.N
        # Verify first row histogram
        expected = make_sample(0)
        for i in range(_HIST_BINS):
            assert int(rows[0][f"hist[{i}]"]) == int(expected.hist[i]), \
                f"hist[{i}] mismatch"

    def test_session_id_column_correct(self, db_with_session, tmp_path):
        db, sid = db_with_session
        out = str(tmp_path / "export.csv")
        bs.export_session_to_csv(db, sid, out, include_hist=False)
        with open(out, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        for row in rows:
            assert int(row["session_id"]) == sid

    def test_sequence_column_is_zero_based_and_gapless(self, db_with_session, tmp_path):
        db, sid = db_with_session
        out = str(tmp_path / "export.csv")
        bs.export_session_to_csv(db, sid, out, include_hist=False)
        with open(out, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        seqs = [int(r["sequence"]) for r in rows]
        assert seqs == list(range(self.N))

    def test_nonexistent_session_exports_zero_rows(self, tmp_db, tmp_path):
        out = str(tmp_path / "empty.csv")
        written = bs.export_session_to_csv(tmp_db, 9999, out, include_hist=False)
        assert written == 0
        # File must still be a valid CSV with a header
        with open(out, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        assert len(rows) == 1  # header only


# ---------------------------------------------------------------------------
# SamplesDBsqlite shim (backward-compatibility)
# ---------------------------------------------------------------------------


class TestSamplesDBsqliteShim:
    def test_insert_and_retrieve_roundtrip(self, tmp_db):
        uid = uuid.uuid4()
        sdb = bs.SamplesDBsqlite(tmp_db, uid)
        samples_in = [make_sample(i) for i in range(10)]
        sdb.insert(session_id=1, samples_list=samples_in)

        retrieved = sdb.retrieve(session_id=1)
        assert retrieved is not None
        assert len(retrieved) == 10

    def test_retrieve_preserves_hist(self, tmp_db):
        uid = uuid.uuid4()
        sdb = bs.SamplesDBsqlite(tmp_db, uid)
        original = make_sample(0)
        sdb.insert(session_id=1, samples_list=[original])
        retrieved = sdb.retrieve(session_id=1)
        np.testing.assert_array_equal(retrieved[0].hist, original.hist)

    def test_unknown_uid_returns_none(self, tmp_db):
        uid = uuid.uuid4()
        sdb = bs.SamplesDBsqlite(tmp_db, uid)
        # retrieve without any prior insert
        result = sdb.retrieve(session_id=1)
        assert result is None

    def test_view_content_does_not_raise(self, tmp_db):
        uid = uuid.uuid4()
        sdb = bs.SamplesDBsqlite(tmp_db, uid)
        sdb.insert(session_id=1, samples_list=[make_sample(0)])
        sdb.view_content()  # must not raise
