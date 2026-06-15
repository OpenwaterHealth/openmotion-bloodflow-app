"""Unit tests for data_sources.py — ScanDataSource + helpers."""

import math
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from data_sources import _CameraBuffer

pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────────────────────────────────────
# _CameraBuffer
# ─────────────────────────────────────────────────────────────────────────────

def test_camera_buffer_starts_empty():
    buf = _CameraBuffer(initial_capacity=8)
    assert buf.n == 0
    assert buf.t.shape == (8,)
    assert buf.v.shape == (8,)
    assert buf.frame_id.shape == (8,)
    assert buf.dropped_at is None


def test_camera_buffer_append_grows_high_water_mark():
    buf = _CameraBuffer(initial_capacity=8)
    buf.append(t=0.025, v=1.5, frame_id=10)
    buf.append(t=0.050, v=2.5, frame_id=11)
    assert buf.n == 2
    assert buf.t[0] == 0.025
    assert buf.t[1] == 0.050
    assert buf.v[0] == np.float32(1.5)
    assert buf.v[1] == np.float32(2.5)
    assert buf.frame_id[0] == 10
    assert buf.frame_id[1] == 11


def test_camera_buffer_small_max_capacity_ring_trims():
    """A small max_capacity (test/debug live cache) ring-trims the
    oldest half and flags ring_trimmed once it fills — the trigger for
    the LiveScanSource DB tail."""
    buf = _CameraBuffer(max_capacity=10)
    for i in range(10):
        buf.append(t=float(i), v=float(i), frame_id=i)
    assert buf.n == 10
    assert not buf.ring_trimmed
    # 11th append fills past cap → ring-trim drops oldest half.
    buf.append(t=10.0, v=10.0, frame_id=10)
    assert buf.ring_trimmed
    assert buf.n == 6  # kept most-recent 5 + the new one
    # Oldest retained sample is t=5 (0..4 dropped).
    assert float(buf.t[0]) == 5.0
    assert float(buf.t[buf.n - 1]) == 10.0


def test_camera_buffer_unbounded_never_ring_trims():
    """max_capacity=None (past / DB-window buffers) grows without bound and
    never ring-trims — it must keep every row it bulk-loads. Append well past
    what would be the default cap had it been finite, and assert nothing is
    dropped."""
    buf = _CameraBuffer(initial_capacity=4, max_capacity=None)
    n = 5000
    for i in range(n):
        buf.append(t=float(i), v=float(i), frame_id=i)
    assert buf.n == n
    assert not buf.ring_trimmed
    # First and last rows both survive — no oldest-half drop occurred.
    assert float(buf.t[0]) == 0.0
    assert float(buf.t[buf.n - 1]) == float(n - 1)
    assert buf.t.shape[0] >= n


def test_camera_buffer_doubles_capacity_on_overflow():
    buf = _CameraBuffer(initial_capacity=2)
    for i in range(5):
        buf.append(t=float(i), v=float(i), frame_id=i)
    assert buf.n == 5
    # Capacity doubled 2 → 4 → 8 to fit 5 samples
    assert buf.t.shape[0] >= 5
    assert buf.t.shape[0] == 8  # 2 doubled to 4 (still too small) doubled to 8
    # Values preserved across resize
    assert list(buf.v[:5]) == [np.float32(i) for i in range(5)]


def test_camera_buffer_window_indices_binary_search():
    buf = _CameraBuffer(initial_capacity=64)
    for i in range(40):
        buf.append(t=i * 0.025, v=float(i), frame_id=i)
    # Window covering t in [0.250, 0.500] should give indices [10, 21) (right-exclusive)
    i_lo, i_hi = buf.window_indices(0.250, 0.500)
    assert i_lo == 10
    assert i_hi == 21  # searchsorted right-side gives one past the last match


def test_camera_buffer_window_indices_empty_buffer():
    buf = _CameraBuffer(initial_capacity=8)
    i_lo, i_hi = buf.window_indices(0.0, 1.0)
    assert i_lo == 0
    assert i_hi == 0


def test_camera_buffer_mark_dropped_sets_timestamp_once():
    buf = _CameraBuffer(initial_capacity=8)
    buf.mark_dropped(t=1.5)
    assert buf.dropped_at == 1.5
    # Idempotent — second call doesn't overwrite the first dropout time
    buf.mark_dropped(t=2.7)
    assert buf.dropped_at == 1.5


# ─────────────────────────────────────────────────────────────────────────────
# ScanDataSource (base)
# ─────────────────────────────────────────────────────────────────────────────

from data_sources import ScanDataSource


def test_scan_data_source_starts_with_no_buffers():
    src = ScanDataSource(plot_t0=100.0)
    assert src.live is False  # base class default; LiveScanSource overrides
    assert src.liveEdge == 0.0
    assert src.plot_t0 == 100.0
    assert src.buffers == {}


def test_scan_data_source_get_or_create_buffer_returns_same_instance():
    src = ScanDataSource(plot_t0=0.0)
    b1 = src.get_or_create_buffer("left", 0, "bfi")
    b2 = src.get_or_create_buffer("left", 0, "bfi")
    assert b1 is b2


def test_scan_data_source_live_edge_tracks_max_timestamp():
    src = ScanDataSource(plot_t0=0.0)
    b = src.get_or_create_buffer("left", 0, "bfi")
    b.append(t=0.5, v=1.0, frame_id=1)
    b.append(t=1.25, v=2.0, frame_id=2)
    b2 = src.get_or_create_buffer("right", 3, "bvi")
    b2.append(t=0.75, v=3.0, frame_id=3)
    assert src.liveEdge == 1.25


def test_scan_data_source_samples_appended_emits_after_flush():
    src = ScanDataSource(plot_t0=0.0)
    received: list = []
    src.samplesAppended.connect(lambda s, c, m, n: received.append((s, c, m, n)))

    src.note_dirty("left", 0, "bfi", added=3)
    src.note_dirty("left", 0, "bfi", added=2)  # coalesces
    src.note_dirty("right", 4, "bvi", added=1)
    assert received == []  # not yet flushed

    src._flush()
    assert sorted(received) == [("left", 0, "bfi", 5), ("right", 4, "bvi", 1)]


def test_scan_data_source_flush_clears_pending():
    src = ScanDataSource(plot_t0=0.0)
    received: list = []
    src.samplesAppended.connect(lambda s, c, m, n: received.append((s, c, m, n)))
    src.note_dirty("left", 0, "bfi", added=1)
    src._flush()
    src._flush()  # nothing pending now
    assert received == [("left", 0, "bfi", 1)]


# ─────────────────────────────────────────────────────────────────────────────
# LiveScanSource
# ─────────────────────────────────────────────────────────────────────────────

from data_sources import LiveScanSource


def test_live_scan_source_live_flag_true():
    src = LiveScanSource(plot_t0=0.0)
    assert src.live is True


def test_live_scan_source_append_uncorrected_populates_all_metrics():
    src = LiveScanSource(plot_t0=0.0)
    src.append_uncorrected(
        side="left", cam_id=2, frame_id=42, t=0.025,
        bfi=4.5, bvi=3.1, mean=120.0, contrast=0.31,
    )
    assert src.buffers[("left", 2, "bfi")].v[0] == np.float32(4.5)
    assert src.buffers[("left", 2, "bvi")].v[0] == np.float32(3.1)
    assert src.buffers[("left", 2, "mean")].v[0] == np.float32(120.0)
    assert src.buffers[("left", 2, "contrast")].v[0] == np.float32(0.31)
    for metric in ("bfi", "bvi", "mean", "contrast"):
        assert src.buffers[("left", 2, metric)].n == 1
        assert src.buffers[("left", 2, metric)].t[0] == 0.025
        assert src.buffers[("left", 2, metric)].frame_id[0] == 42


def test_live_scan_source_skips_none_mean_or_contrast():
    """mean/contrast are optional — None means 'this sample's value wasn't
    available' and the buffer for that metric is not appended to."""
    src = LiveScanSource(plot_t0=0.0)
    src.append_uncorrected(
        side="left", cam_id=0, frame_id=1, t=0.0,
        bfi=4.0, bvi=3.0, mean=None, contrast=None,
    )
    assert src.buffers[("left", 0, "bfi")].n == 1
    assert src.buffers[("left", 0, "bvi")].n == 1
    assert ("left", 0, "mean") not in src.buffers
    assert ("left", 0, "contrast") not in src.buffers


def test_live_scan_source_append_uncorrected_stores_temp():
    """temp is an optional fifth metric — stored under metric key "temp"
    so PlotCell can read it via value_at (issue #165)."""
    src = LiveScanSource(plot_t0=0.0)
    src.append_uncorrected(
        side="left", cam_id=2, frame_id=42, t=0.025,
        bfi=4.5, bvi=3.1, temp=54.3,
    )
    buf = src.buffers[("left", 2, "temp")]
    assert buf.n == 1
    assert buf.v[0] == np.float32(54.3)
    assert buf.t[0] == 0.025
    assert src.value_at("left", 2, "temp", 0.025) == pytest.approx(54.3, abs=1e-4)


def test_live_scan_source_skips_none_temp():
    """temp=None (the default) creates no "temp" buffer — same contract
    as mean/contrast."""
    src = LiveScanSource(plot_t0=0.0)
    src.append_uncorrected(
        side="left", cam_id=0, frame_id=1, t=0.0,
        bfi=4.0, bvi=3.0,
    )
    assert ("left", 0, "temp") not in src.buffers


def test_live_scan_source_stores_nan_values():
    """NaN survives. The renderer filters non-finite, not the source."""
    src = LiveScanSource(plot_t0=0.0)
    src.append_uncorrected(
        side="left", cam_id=0, frame_id=1, t=0.0,
        bfi=float("nan"), bvi=3.0,
    )
    assert math.isnan(float(src.buffers[("left", 0, "bfi")].v[0]))
    assert src.buffers[("left", 0, "bfi")].n == 1


def test_live_scan_source_append_uncorrected_notes_dirty():
    src = LiveScanSource(plot_t0=0.0)
    received: list = []
    src.samplesAppended.connect(lambda s, c, m, n: received.append((s, c, m, n)))

    src.append_uncorrected(
        side="left", cam_id=0, frame_id=1, t=0.0,
        bfi=4.0, bvi=3.0, mean=120.0, contrast=0.3,
    )
    src.append_uncorrected(
        side="left", cam_id=0, frame_id=2, t=0.025,
        bfi=4.2, bvi=3.1, mean=121.0, contrast=0.31,
    )
    src._flush()

    # One emit per (side, cam, metric), with added=2 each
    assert sorted(received) == [
        ("left", 0, "bfi", 2),
        ("left", 0, "bvi", 2),
        ("left", 0, "contrast", 2),
        ("left", 0, "mean", 2),
    ]


def test_live_scan_source_mark_dropped_sets_buffer_dropped_at():
    src = LiveScanSource(plot_t0=0.0)
    src.append_uncorrected(
        side="right", cam_id=5, frame_id=1, t=0.0,
        bfi=4.0, bvi=3.0, mean=120.0, contrast=0.3,
    )
    src.mark_dropped(side="right", cam_id=5, t=2.5)
    # All 4 metrics share the dropped_at — the marker is per-(side, cam),
    # not per-metric, so we set it on every existing metric buffer.
    for metric in ("bfi", "bvi", "mean", "contrast"):
        assert src.buffers[("right", 5, metric)].dropped_at == 2.5


# ─────────────────────────────────────────────────────────────────────────────
# PastScanSource
# ─────────────────────────────────────────────────────────────────────────────

from data_sources import PastScanSource


# Schema mirrors openmotion-sdk/omotion/ScanDatabase.py:115-129.
_SESSION_DATA_DDL = """
    CREATE TABLE session_data (
        id               INTEGER PRIMARY KEY,
        session_id       INTEGER NOT NULL,
        session_raw_id   INTEGER,
        cam_id           INTEGER NOT NULL,
        side             INTEGER NOT NULL CHECK(side IN (0, 1)),
        frame_id         INTEGER NOT NULL DEFAULT -1,
        timestamp_s      REAL    NOT NULL,
        bfi              REAL,
        bvi              REAL,
        contrast         REAL,
        mean             REAL
    );
"""


class _FakeScanDatabase:
    """Tiny stand-in for omotion.ScanDatabase that just yields session_data
    rows for a given session_id from a synthetic sqlite DB. Matches the
    real iter_session_data signature."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    def iter_session_data(self, session_id: int, side=None, cam_id=None,
                          t_lo=None, t_hi=None):
        sql = "SELECT * FROM session_data WHERE session_id = ?"
        bindings = [session_id]
        if side is not None:
            sql += " AND side = ?"
            bindings.append(side)
        if cam_id is not None:
            sql += " AND cam_id = ?"
            bindings.append(cam_id)
        if t_lo is not None:
            sql += " AND timestamp_s >= ?"
            bindings.append(float(t_lo))
        if t_hi is not None:
            sql += " AND timestamp_s <= ?"
            bindings.append(float(t_hi))
        sql += " ORDER BY timestamp_s ASC"
        for row in self._conn.execute(sql, bindings):
            yield dict(row)


@pytest.fixture
def session_data_db(tmp_path):
    """Returns a (db, session_id) pair pre-populated with synthetic rows
    spanning two sides × two cameras × 5 timestamps each."""
    db_path = tmp_path / "scan.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SESSION_DATA_DDL)
    session_id = 7

    rows = []
    for side_int, side_offset in ((0, 0), (1, 100)):  # left = 0, right = 1
        for cam_id in (0, 3):
            for i in range(5):
                rows.append((
                    session_id,
                    None,        # session_raw_id
                    cam_id,
                    side_int,
                    1000 + i,    # frame_id
                    i * 0.025,   # timestamp_s
                    1.0 + side_offset + cam_id + i * 0.1,  # bfi
                    10.0 + side_offset + cam_id + i * 0.1, # bvi
                    0.30 + i * 0.01,                       # contrast
                    100.0 + side_offset + cam_id + i,      # mean
                ))
    conn.executemany(
        "INSERT INTO session_data "
        "(session_id, session_raw_id, cam_id, side, frame_id, timestamp_s, "
        " bfi, bvi, contrast, mean) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return _FakeScanDatabase(conn), session_id


def test_past_scan_source_live_flag_false(session_data_db):
    db, sid = session_data_db
    src = PastScanSource(scan_db=db, session_id=sid)
    assert src.live is False


def test_past_scan_source_bucketizes_rows_into_per_metric_buffers(session_data_db):
    db, sid = session_data_db
    src = PastScanSource(scan_db=db, session_id=sid)

    # This fixture is a normal-mode scan: 4 (side, cam) combos × 4 metrics =
    # 16 per-cam buffers, each holding its 5 rows. No cam_id=-1 (that's the
    # reduced-mode side average, read straight from the DB when present).
    expected_keys = {
        (side, cam, metric)
        for side in ("left", "right")
        for cam in (0, 3)
        for metric in ("bfi", "bvi", "mean", "contrast")
    }
    assert set(src.buffers.keys()) == expected_keys
    for buf in src.buffers.values():
        assert buf.n == 5


def test_past_scan_source_normalizes_side_integer_to_string(session_data_db):
    db, sid = session_data_db
    src = PastScanSource(scan_db=db, session_id=sid)
    # No integer-keyed entries; all side keys are strings.
    for (side, _, _) in src.buffers.keys():
        assert side in ("left", "right")


def test_past_scan_source_preserves_frame_ids(session_data_db):
    db, sid = session_data_db
    src = PastScanSource(scan_db=db, session_id=sid)
    buf = src.buffers[("left", 0, "bfi")]
    # frame_ids 1000..1004 in input order
    assert list(int(x) for x in buf.frame_id[:5]) == [1000, 1001, 1002, 1003, 1004]


def test_past_scan_source_live_edge_is_max_timestamp(session_data_db):
    db, sid = session_data_db
    src = PastScanSource(scan_db=db, session_id=sid)
    assert src.liveEdge == pytest.approx(0.025 * 4)  # last timestamp = 0.100


def test_past_scan_source_empty_session_yields_empty_source(tmp_path):
    db_path = tmp_path / "scan.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SESSION_DATA_DDL)
    conn.commit()
    db = _FakeScanDatabase(conn)

    src = PastScanSource(scan_db=db, session_id=42)
    assert src.buffers == {}
    assert src.liveEdge == 0.0


def test_past_scan_source_reads_cam_id_minus_1_from_db(tmp_path):
    """A reduced-mode scan stores the corrected per-side average at cam_id=-1
    (cam_id=-1 frames on the 'final' channel, persisted by ScanDBSink).
    PastScanSource exposes those directly —
    no derivation — so reduced-mode replay reads the corrected trace verbatim."""
    db_path = tmp_path / "scan.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SESSION_DATA_DDL)
    rows = []
    for i in range(3):
        for side in (0, 1):
            rows.append((9, None, -1, side, 1000 + i, i * 0.025,
                         2.0 + side + i * 0.1,    # bfi
                         5.0 + side + i * 0.1,    # bvi
                         0.3, 100.0))
    conn.executemany(
        "INSERT INTO session_data "
        "(session_id, session_raw_id, cam_id, side, frame_id, timestamp_s, "
        " bfi, bvi, contrast, mean) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    db = _FakeScanDatabase(conn)

    src = PastScanSource(scan_db=db, session_id=9)
    # Only cam_id=-1 buffers (reduced-mode scan persists average only).
    assert all(cam == -1 for (_s, cam, _m) in src.buffers.keys())
    left_bfi = src.buffers[("left", -1, "bfi")]
    assert left_bfi.n == 3
    assert float(left_bfi.v[0]) == pytest.approx(2.0)   # side 0, i 0
    right_bvi = src.buffers[("right", -1, "bvi")]
    assert float(right_bvi.v[0]) == pytest.approx(6.0)  # side 1, i 0


def test_past_scan_source_buffers_are_unbounded(session_data_db):
    """Buffers a past scan creates must be unbounded so a long bulk load never
    ring-trims away its own oldest rows."""
    db, sid = session_data_db
    src = PastScanSource(scan_db=db, session_id=sid)
    for buf in src.buffers.values():
        assert buf._max_capacity is None


def test_past_scan_source_csv_fallback_when_db_has_no_bfi(tmp_path):
    """A pre-pipeline scan with no BFI/BVI in the DB falls back to the reduced
    corrected CSV, which populates the cam_id=-1 side buffers."""
    db_path = tmp_path / "scan.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SESSION_DATA_DDL)
    conn.commit()
    db = _FakeScanDatabase(conn)  # empty DB → no BFI → CSV fallback

    csv_path = tmp_path / "reduced.csv"
    csv_path.write_text(
        "frame_id,timestamp_s,bfi_left,bfi_right,bvi_left,bvi_right\n"
        "0,0.0,2.5,3.5,12.0,13.0\n"
        "1,0.025,2.6,3.6,12.1,13.1\n",
        encoding="utf-8",
    )
    src = PastScanSource(scan_db=db, session_id=1, corrected_csv_path=str(csv_path))

    left_bfi = src.buffers[("left", -1, "bfi")]
    assert left_bfi.n == 2
    assert float(left_bfi.v[0]) == pytest.approx(2.5)


def test_past_scan_source_skips_null_metric_values(tmp_path):
    """A row with NULL bfi/bvi/mean/contrast must not crash and must not
    insert a NaN where the SQL was NULL — that metric just isn't recorded
    for that frame."""
    db_path = tmp_path / "scan.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SESSION_DATA_DDL)
    # One row with all metrics NULL except bfi
    conn.execute(
        "INSERT INTO session_data "
        "(session_id, cam_id, side, frame_id, timestamp_s, bfi, bvi, contrast, mean) "
        "VALUES (1, 0, 0, 100, 0.5, 4.0, NULL, NULL, NULL)"
    )
    conn.commit()
    db = _FakeScanDatabase(conn)

    src = PastScanSource(scan_db=db, session_id=1)
    assert src.buffers[("left", 0, "bfi")].n == 1
    assert src.buffers[("left", 0, "bfi")].v[0] == np.float32(4.0)
    # NULL metrics yield no buffer entry at all
    assert ("left", 0, "bvi") not in src.buffers
    assert ("left", 0, "mean") not in src.buffers
    assert ("left", 0, "contrast") not in src.buffers


# ─────────────────────────────────────────────────────────────────────────────
# load_past_scan_buffers + PastScanSource(preloaded_buffers=...) — the
# off-thread bulk-load path used by the async loadPastScan (issue #152).
# ─────────────────────────────────────────────────────────────────────────────

from data_sources import load_past_scan_buffers


def test_load_past_scan_buffers_matches_sync_constructor(session_data_db):
    """The Qt-free bulk loader must produce exactly what the synchronous
    PastScanSource constructor path produces."""
    db, sid = session_data_db
    buffers, has_bfi = load_past_scan_buffers(db, sid)
    sync_src = PastScanSource(scan_db=db, session_id=sid)

    assert has_bfi is True
    assert set(buffers.keys()) == set(sync_src.buffers.keys())
    for key, buf in buffers.items():
        sync_buf = sync_src.buffers[key]
        assert buf.n == sync_buf.n
        np.testing.assert_array_equal(buf.t[:buf.n], sync_buf.t[:sync_buf.n])
        np.testing.assert_array_equal(buf.v[:buf.n], sync_buf.v[:sync_buf.n])


def test_load_past_scan_buffers_csv_fallback(tmp_path):
    """No BFI/BVI in the DB → the corrected-CSV fallback populates the
    reduced-mode cam_id=-1 side buffers, same as the sync path."""
    db_path = tmp_path / "scan.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SESSION_DATA_DDL)
    conn.commit()
    db = _FakeScanDatabase(conn)  # empty DB → no BFI → CSV fallback

    csv_path = tmp_path / "reduced.csv"
    csv_path.write_text(
        "frame_id,timestamp_s,bfi_left,bfi_right,bvi_left,bvi_right\n"
        "0,0.0,2.5,3.5,12.0,13.0\n"
        "1,0.025,2.6,3.6,12.1,13.1\n",
        encoding="utf-8",
    )
    buffers, has_bfi = load_past_scan_buffers(db, 1, str(csv_path))
    assert has_bfi is False  # DB itself carried no BFI
    left_bfi = buffers[("left", -1, "bfi")]
    assert left_bfi.n == 2
    assert float(left_bfi.v[0]) == pytest.approx(2.5)
    # CSV-fallback buffers must be unbounded too.
    for buf in buffers.values():
        assert buf._max_capacity is None


def test_past_scan_source_adopts_preloaded_buffers(session_data_db):
    """preloaded_buffers skips the DB walk entirely — scan_db=None must
    never be touched — and the source exposes the adopted buffers."""
    db, sid = session_data_db
    buffers, _ = load_past_scan_buffers(db, sid)

    src = PastScanSource(
        scan_db=None,  # would raise if the constructor tried to walk it
        session_id=sid,
        preloaded_buffers=buffers,
    )
    assert src.live is False
    assert src.session_id == sid
    assert src.buffers is buffers
    assert src.liveEdge == pytest.approx(0.025 * 4)
    # QML-facing window query works against the adopted buffers.
    pts = src.points_for_window("left", 0, "bfi", 0.0, 1.0, 100)
    assert len(pts) == 5


def test_past_scan_source_preloaded_empty_dict_is_empty_source():
    """An empty preloaded dict (failed/empty session) still yields a
    valid, empty source — no fallback to the (None) scan_db."""
    src = PastScanSource(scan_db=None, session_id=3, preloaded_buffers={})
    assert src.buffers == {}
    assert src.liveEdge == 0.0


# ── Issue #175 — past scan carries its own camera configuration ────────
# The plot viewer lays its grid out from leftMask/rightMask. When a past
# scan is replayed, the viewer must use THAT scan's camera config (which
# cameras actually recorded), not the operator's live Scan Settings
# selection. PastScanSource reconstructs the masks from which per-camera
# (cam_id 0..7) streams it loaded so the viewer can prefer them.


def test_past_scan_source_derives_masks_from_buffer_cam_ids(session_data_db):
    db, sid = session_data_db
    src = PastScanSource(scan_db=db, session_id=sid)
    # Fixture records cams 0 and 3 on both sides → bits 0 and 3 → 0x09.
    assert src.leftMask == 0x09
    assert src.rightMask == 0x09


def test_past_scan_source_derives_masks_for_far_config():
    """A 'Far' (0xC3 = cams 1,2,7,8 → camIds 0,1,6,7) scan recorded only
    those cameras; the derived masks reflect that regardless of any live
    selection."""
    buffers = {}
    for side in ("left", "right"):
        for cam_id in (0, 1, 6, 7):
            buf = _CameraBuffer(max_capacity=None)
            buf.append(t=0.0, v=1.0, frame_id=0)
            buffers[(side, cam_id, "bfi")] = buf
    src = PastScanSource(scan_db=None, session_id=1, preloaded_buffers=buffers)
    assert src.leftMask == 0xC3
    assert src.rightMask == 0xC3


def test_past_scan_source_derives_per_side_masks_independently():
    """Left and right masks are reconstructed separately — a one-sided or
    asymmetric scan keeps each side's true configuration."""
    buffers = {}
    for cam_id in (0, 1, 6, 7):  # left = Far
        buf = _CameraBuffer(max_capacity=None)
        buf.append(t=0.0, v=1.0, frame_id=0)
        buffers[("left", cam_id, "bfi")] = buf
    # right side: no cameras recorded
    src = PastScanSource(scan_db=None, session_id=1, preloaded_buffers=buffers)
    assert src.leftMask == 0xC3
    assert src.rightMask == 0x00


def test_live_scan_source_masks_unknown():
    """A live source reports unknown (-1) masks so the viewer keeps using
    the operator's live Scan Settings selection during a live scan."""
    src = LiveScanSource(plot_t0=0.0)
    assert src.leftMask == -1
    assert src.rightMask == -1


def test_past_scan_source_masks_unknown_when_no_per_cam_streams():
    """Reduced-mode scans only carry the cam_id=-1 side average; with no
    per-camera streams there is no normal-mode mask to derive, so the
    masks read -1 (unknown) and the viewer falls back to its live masks."""
    buffers = {}
    for side in ("left", "right"):
        buf = _CameraBuffer(max_capacity=None)
        buf.append(t=0.0, v=1.0, frame_id=0)
        buffers[(side, -1, "bfi")] = buf
    src = PastScanSource(scan_db=None, session_id=1, preloaded_buffers=buffers)
    assert src.leftMask == -1
    assert src.rightMask == -1


# ─────────────────────────────────────────────────────────────────────────────
# currentScanSource holder pattern — verified via a parallel mini-class
# (MotionConnector uses the same pattern; see motion_connector.py)
# ─────────────────────────────────────────────────────────────────────────────

from PyQt6.QtCore import QObject, pyqtSignal
from data_sources import LiveScanSource, ScanDataSource


def _make_holder():
    """Returns a fresh QObject with the same currentScanSource surface as
    MotionConnector. Kept in-test rather than exported so production code
    doesn't grow a separate abstraction layer just for testability."""

    class _ScanSourceHolder(QObject):
        currentScanSourceChanged = pyqtSignal()

        def __init__(self):
            super().__init__()
            self._current_scan_source: ScanDataSource | None = None

        @property
        def currentScanSource(self):
            return self._current_scan_source

        def _set_current_scan_source(self, source):
            if source is self._current_scan_source:
                return
            self._current_scan_source = source
            self.currentScanSourceChanged.emit()

    return _ScanSourceHolder()


def test_current_scan_source_default_is_none():
    holder = _make_holder()
    assert holder.currentScanSource is None


def test_set_current_scan_source_emits_notify():
    holder = _make_holder()
    received: list = []
    holder.currentScanSourceChanged.connect(
        lambda: received.append(holder.currentScanSource)
    )

    src1 = LiveScanSource(plot_t0=0.0)
    holder._set_current_scan_source(src1)
    assert holder.currentScanSource is src1
    assert received == [src1]

    src2 = LiveScanSource(plot_t0=10.0)
    holder._set_current_scan_source(src2)
    assert holder.currentScanSource is src2
    assert received == [src1, src2]


def test_set_current_scan_source_dedupes_same_instance():
    holder = _make_holder()
    src = LiveScanSource(plot_t0=0.0)
    received: list = []
    holder.currentScanSourceChanged.connect(
        lambda: received.append(holder.currentScanSource)
    )

    holder._set_current_scan_source(src)
    holder._set_current_scan_source(src)  # no-op — same instance
    assert received == [src]


def test_live_scan_source_mark_dropped_no_buffers_yet_is_noop():
    """If a camera drops before any sample arrived, mark_dropped silently
    does nothing — no buffer to mark. Phase 2's PlotViewer won't have a
    cell for a camera with no samples anyway."""
    src = LiveScanSource(plot_t0=0.0)
    src.mark_dropped(side="left", cam_id=0, t=1.5)
    assert src.buffers == {}  # nothing created


def test_live_scan_source_mark_dropped_multiple_metric_buffers():
    """When a camera has samples in all 4 metric buffers, mark_dropped
    sets dropped_at on all 4 — the marker is per-(side, cam), per-metric
    bookkeeping just mirrors it."""
    src = LiveScanSource(plot_t0=0.0)
    src.append_uncorrected(
        side="left", cam_id=1, frame_id=10, t=0.0,
        bfi=4.0, bvi=3.0, mean=120.0, contrast=0.3,
    )
    src.append_uncorrected(  # second sample without mean — only 3 metrics
        side="left", cam_id=2, frame_id=11, t=0.025,
        bfi=4.1, bvi=3.1, mean=None, contrast=0.31,
    )

    src.mark_dropped(side="left", cam_id=1, t=2.5)
    src.mark_dropped(side="left", cam_id=2, t=2.7)

    # cam 1: all 4 buffers exist, all marked at 2.5
    for metric in ("bfi", "bvi", "mean", "contrast"):
        assert src.buffers[("left", 1, metric)].dropped_at == 2.5
    # cam 2: 3 buffers exist (no mean), all marked at 2.7
    for metric in ("bfi", "bvi", "contrast"):
        assert src.buffers[("left", 2, metric)].dropped_at == 2.7
    assert ("left", 2, "mean") not in src.buffers


# ─────────────────────────────────────────────────────────────────────────────
# _CameraBuffer.window_decimated (Phase 2a)
# ─────────────────────────────────────────────────────────────────────────────


def test_camera_buffer_window_decimated_returns_raw_when_window_fits():
    """When the time window times the nominal sample rate is ≤ max_points
    (zoomed in tight enough that every sample fits), stride drops to 1
    and we short-circuit to raw samples. Earlier code forced a minimum
    stride of 2 + 6-sample causal smoothing, which over-filtered the
    tightest zoom views and hid real signal detail."""
    buf = _CameraBuffer(initial_capacity=64)
    for i in range(20):
        buf.append(t=i * 0.025, v=float(i), frame_id=i)
    # 0.5 s window × 40 Hz nominal = 20 expected samples ≤ max_points=100
    # → stride=1 → raw return path.
    t_dec, v_dec = buf.window_decimated(t_lo=0.0, t_hi=0.5, max_points=100)
    assert len(t_dec) == 20
    assert list(v_dec) == [np.float32(i) for i in range(20)]


def test_camera_buffer_window_decimated_strides_when_over_max():
    buf = _CameraBuffer(initial_capacity=1024)
    for i in range(400):
        buf.append(t=i * 0.025, v=float(i), frame_id=i)
    # Time-keyed stride: 10 s window × 40 Hz / max_points=100 = 4.
    # window = stride = 4 (Nyquist-minimum). Stride-aligned abs idxs
    # [0, 4, 8, 12, 16, ...]. Causal window per output:
    # [abs_idx - stride + 1, abs_idx], clipped at 0.
    t_dec, v_dec = buf.window_decimated(t_lo=0.0, t_hi=10.0, max_points=100)
    assert len(t_dec) == 100
    # abs=0:  window [0..0]   → mean v[0]                = 0.0
    # abs=4:  window [1..4]   → mean(1,2,3,4)            = 2.5
    # abs=8:  window [5..8]   → mean(5,6,7,8)            = 6.5
    # abs=12: window [9..12]  → mean(9,10,11,12)         = 10.5
    # abs=16: window [13..16] → mean(13,14,15,16)        = 14.5
    assert list(v_dec[:5]) == [
        np.float32(0.0), np.float32(2.5), np.float32(6.5),
        np.float32(10.5), np.float32(14.5),
    ]


def test_camera_buffer_window_decimated_smoothing_kills_alternation():
    """At stride=2 a plain subsample alternates between odd-index and
    even-index samples as the window scrolls, producing per-paint
    flicker on noisy data. The Nyquist-minimum causal smoothing
    (window = stride) averages each pair so alternation collapses to
    the local mean."""
    buf = _CameraBuffer(initial_capacity=1024)
    # Strong noise pattern: alternating 0 and 10.
    for i in range(400):
        buf.append(t=i * 0.025, v=10.0 if (i % 2) else 0.0, frame_id=i)
    # 10 s × 40 Hz / max_points=200 → stride=2, window=2.
    t_dec, v_dec = buf.window_decimated(t_lo=0.0, t_hi=10.0, max_points=200)
    assert len(t_dec) == 200
    # abs=0: window [0..0] = v[0] = 0  (edge — single-sample only)
    # abs=2 onward: window [N-1, N] over alternating 10/0 → mean = 5.0
    for v in v_dec[1:]:
        assert v == np.float32(5.0)


def test_camera_buffer_window_decimated_empty_window_returns_empty_arrays():
    buf = _CameraBuffer(initial_capacity=64)
    for i in range(10):
        buf.append(t=i * 0.025, v=float(i), frame_id=i)
    # Window after the last sample
    t_dec, v_dec = buf.window_decimated(t_lo=10.0, t_hi=11.0, max_points=100)
    assert len(t_dec) == 0
    assert len(v_dec) == 0


def test_camera_buffer_window_decimated_empty_buffer_returns_empty_arrays():
    buf = _CameraBuffer(initial_capacity=8)
    t_dec, v_dec = buf.window_decimated(t_lo=0.0, t_hi=1.0, max_points=50)
    assert len(t_dec) == 0
    assert len(v_dec) == 0


def test_camera_buffer_window_decimated_partial_window():
    buf = _CameraBuffer(initial_capacity=64)
    for i in range(40):
        buf.append(t=i * 0.025, v=float(i), frame_id=i)
    # Window time range = 0.25 s × 40 Hz nominal = 10 expected samples.
    # max_points=100 → stride=1 → raw return path. window_indices for
    # [0.25, 0.5] returns (10, 21) (searchsorted right at 0.5 includes
    # samples up through index 20 at t=0.500). 11 raw samples 10..20.
    t_dec, v_dec = buf.window_decimated(t_lo=0.250, t_hi=0.500, max_points=100)
    assert len(t_dec) == 11
    assert list(v_dec) == [np.float32(i) for i in range(10, 21)]


# ─────────────────────────────────────────────────────────────────────────────
# ScanDataSource.points_for_window (Phase 2a)
# ─────────────────────────────────────────────────────────────────────────────


def test_scan_data_source_points_for_window_returns_pairs():
    src = ScanDataSource(plot_t0=0.0)
    buf = src.get_or_create_buffer("left", 0, "bfi")
    for i in range(10):
        buf.append(t=i * 0.025, v=4.0 + i * 0.1, frame_id=i)

    pts = src.points_for_window("left", 0, "bfi", 0.0, 0.25, max_points=100)

    # 0.25 s × 40 Hz = 10 expected samples ≤ max_points=100 → stride=1,
    # raw return. All 10 samples pass through.
    assert len(pts) == 10
    assert pts[0] == pytest.approx([0.0, 4.0])
    assert pts[-1] == pytest.approx([0.225, 4.9])


def test_scan_data_source_points_for_window_missing_buffer_returns_empty():
    src = ScanDataSource(plot_t0=0.0)
    pts = src.points_for_window("left", 7, "bvi", 0.0, 1.0, max_points=100)
    assert pts == []


def test_scan_data_source_points_for_window_decimates_when_over_max():
    src = ScanDataSource(plot_t0=0.0)
    buf = src.get_or_create_buffer("right", 3, "mean")
    for i in range(200):
        buf.append(t=i * 0.025, v=float(i), frame_id=i)

    pts = src.points_for_window("right", 3, "mean", 0.0, 5.0, max_points=50)
    # Time-keyed stride: 5 s × 40 Hz / max=50 = 4. window = stride = 4.
    # Stride-aligned abs idxs [0, 4, 8, ...]. Causal window per output:
    # [abs_idx - stride + 1, abs_idx] clipped at 0.
    assert len(pts) == 50
    # abs=0: window [0..0]  → v[0]                = 0.0,  t = 0.0
    assert pts[0] == pytest.approx([0.0, 0.0])
    # abs=4: window [1..4]  → mean(1,2,3,4)       = 2.5,  t = 0.1
    assert pts[1] == pytest.approx([0.1, 2.5])


# ─────────────────────────────────────────────────────────────────────────────
# ScanDataSource.compute_bounds_for_metric (Phase 2b-i autoscale)
# ─────────────────────────────────────────────────────────────────────────────


def test_compute_bounds_neutral_fallback_when_no_buffers():
    src = ScanDataSource(plot_t0=0.0)
    b = src.compute_bounds_for_metric("bfi")
    assert b == {"yMin": 0.0, "yMax": 1.0}


def test_compute_bounds_neutral_fallback_when_under_4_samples():
    src = ScanDataSource(plot_t0=0.0)
    buf = src.get_or_create_buffer("left", 0, "bfi")
    for i in range(3):
        buf.append(t=i * 0.025, v=float(i), frame_id=i)
    b = src.compute_bounds_for_metric("bfi")
    assert b == {"yMin": 0.0, "yMax": 1.0}


def test_compute_bounds_pads_by_25_percent_each_side():
    src = ScanDataSource(plot_t0=0.0)
    buf = src.get_or_create_buffer("left", 0, "bfi")
    # Values 0..99 — percentile 2 = ~2, percentile 98 = ~97. Pad = 0.25 * (97 - 2) = 23.75
    for i in range(100):
        buf.append(t=i * 0.025, v=float(i), frame_id=i)
    b = src.compute_bounds_for_metric("bfi", percentile_lo=2.0, percentile_hi=98.0, pad_frac=0.25)
    # Approx: yMin ≈ 2 - 23.75 = -21.75; yMax ≈ 97 + 23.75 = 120.75
    assert b["yMin"] == pytest.approx(-21.75, abs=1.0)
    assert b["yMax"] == pytest.approx(120.75, abs=1.0)


def test_compute_bounds_aggregates_across_cameras():
    src = ScanDataSource(plot_t0=0.0)
    # cam 0 covers 0..49, cam 1 covers 50..99
    buf0 = src.get_or_create_buffer("left", 0, "bvi")
    buf1 = src.get_or_create_buffer("left", 1, "bvi")
    for i in range(50):
        buf0.append(t=i * 0.025, v=float(i), frame_id=i)
    for i in range(50):
        buf1.append(t=i * 0.025, v=50.0 + i, frame_id=100 + i)
    b = src.compute_bounds_for_metric("bvi")
    # Aggregated range is 0..99 ; percentile bounds + padding
    assert b["yMin"] < 0
    assert b["yMax"] > 100


def test_compute_bounds_ignores_other_metrics():
    src = ScanDataSource(plot_t0=0.0)
    bfi_buf = src.get_or_create_buffer("left", 0, "bfi")
    bvi_buf = src.get_or_create_buffer("left", 0, "bvi")
    for i in range(10):
        bfi_buf.append(t=i * 0.025, v=float(i), frame_id=i)
        bvi_buf.append(t=i * 0.025, v=float(i) * 100.0, frame_id=i)
    b = src.compute_bounds_for_metric("bfi")
    # Should only consider bfi samples (0..9), not bvi (0..900)
    assert b["yMax"] < 50  # bvi would give yMax > 1000


def test_compute_bounds_skips_non_finite():
    src = ScanDataSource(plot_t0=0.0)
    buf = src.get_or_create_buffer("left", 0, "bfi")
    for i in range(10):
        buf.append(t=i * 0.025, v=float(i), frame_id=i)
    buf.append(t=0.25, v=float("nan"), frame_id=10)
    buf.append(t=0.275, v=float("inf"), frame_id=11)
    # Should not crash and should produce finite bounds
    b = src.compute_bounds_for_metric("bfi")
    assert math.isfinite(b["yMin"])
    assert math.isfinite(b["yMax"])


def test_compute_bounds_expands_when_lo_equals_hi():
    src = ScanDataSource(plot_t0=0.0)
    buf = src.get_or_create_buffer("left", 0, "bfi")
    for i in range(10):
        buf.append(t=i * 0.025, v=5.0, frame_id=i)  # all the same value
    b = src.compute_bounds_for_metric("bfi", pad_frac=0.0)
    # When lo == hi, expand by ±0.5; with pad_frac=0 the result is [4.5, 5.5]
    assert b["yMin"] == pytest.approx(4.5)
    assert b["yMax"] == pytest.approx(5.5)


# ─────────────────────────────────────────────────────────────────────────────
# ScanDataSource.value_at (Phase 2b-ii — hover tooltip)
# ─────────────────────────────────────────────────────────────────────────────


def test_value_at_returns_nearest_sample():
    src = ScanDataSource(plot_t0=0.0)
    buf = src.get_or_create_buffer("left", 0, "bfi")
    for i in range(10):
        buf.append(t=i * 0.1, v=float(i), frame_id=i)

    # Exact match
    assert src.value_at("left", 0, "bfi", 0.3) == pytest.approx(3.0)
    # Nearest-left wins on tie-break (0.35 is equidistant; searchsorted
    # left returns idx=4 then the elif prefers idx=3 because abs equal)
    v = src.value_at("left", 0, "bfi", 0.35)
    assert v in (pytest.approx(3.0), pytest.approx(4.0))
    # Past the live edge — returns last sample
    assert src.value_at("left", 0, "bfi", 99.0) == pytest.approx(9.0)
    # Before the first sample — returns first sample
    assert src.value_at("left", 0, "bfi", -1.0) == pytest.approx(0.0)


def test_value_at_missing_buffer_returns_nan():
    src = ScanDataSource(plot_t0=0.0)
    assert math.isnan(src.value_at("left", 7, "bfi", 0.1))


def test_value_at_empty_buffer_returns_nan():
    src = ScanDataSource(plot_t0=0.0)
    src.get_or_create_buffer("left", 0, "bfi")  # exists but n=0
    assert math.isnan(src.value_at("left", 0, "bfi", 0.1))


def test_value_at_nan_sample_returns_nan():
    src = ScanDataSource(plot_t0=0.0)
    buf = src.get_or_create_buffer("left", 0, "bfi")
    buf.append(t=0.1, v=float("nan"), frame_id=1)
    assert math.isnan(src.value_at("left", 0, "bfi", 0.1))


def test_value_at_handles_searchsorted_at_buffer_end():
    """Regression for the race-condition fix — searchsorted("left")
    can return idx == n when t equals or exceeds every sample's
    timestamp. Without the snapshot, a stale-n bounds check could let
    idx-1 reach an out-of-bounds slot. We test the snapshot path
    behaves correctly even at the boundary."""
    src = ScanDataSource(plot_t0=0.0)
    buf = src.get_or_create_buffer("left", 0, "bfi")
    for i in range(5):
        buf.append(t=float(i), v=float(i) * 2.0, frame_id=i)
    # t equal to the last timestamp → searchsorted left returns idx=4
    # (insert before the last). We clamp via the elif tie-break.
    v = src.value_at("left", 0, "bfi", 4.0)
    assert v == pytest.approx(8.0)
    # t past every sample → searchsorted returns idx=5 (== n);
    # the >= n clamp pulls back to last sample.
    v = src.value_at("left", 0, "bfi", 1000.0)
    assert v == pytest.approx(8.0)


# ─────────────────────────────────────────────────────────────────────────────
# ScanDataSource.dropped_at_for (Phase 2b-ii — dropout marker)
# ─────────────────────────────────────────────────────────────────────────────


def test_dropped_at_for_returns_dropout_time():
    """mark_dropped lives on LiveScanSource (only live streams can
    drop mid-scan). After appending one sample to create a buffer,
    marking that (side, cam) dropped should surface the timestamp."""
    src = LiveScanSource(plot_t0=0.0)
    src.append_uncorrected(side="left", cam_id=0, frame_id=1, t=0.1,
                           bfi=4.0, bvi=2.0)
    src.mark_dropped(side="left", cam_id=0, t=12.5)
    assert src.dropped_at_for("left", 0) == pytest.approx(12.5)


def test_dropped_at_for_returns_nan_when_no_dropout():
    src = LiveScanSource(plot_t0=0.0)
    src.append_uncorrected(side="left", cam_id=0, frame_id=1, t=0.1,
                           bfi=4.0, bvi=2.0)
    assert math.isnan(src.dropped_at_for("left", 0))


def test_dropped_at_for_returns_nan_when_buffer_missing():
    src = LiveScanSource(plot_t0=0.0)
    # No samples appended → no buffers for (left, 7)
    assert math.isnan(src.dropped_at_for("left", 7))


def test_window_decimated_stride_stable_as_buffer_grows():
    """Regression: when zoomed out, the visible window's time extent
    is constant but n_window grows by 1 each new sample. The old
    n_window-keyed stride crept up (e.g. ceil(800/200)=4 → ceil(801/200)=5
    on the 801st sample), relocating every output to a new absolute
    index with a new causal window — visible as the trace shape
    'morphing' every few frames. The fix keys stride on (t_hi - t_lo)
    so it's invariant under sample growth at fixed zoom."""
    buf = _CameraBuffer(initial_capacity=2048)
    for i in range(800):
        buf.append(t=i * 0.025, v=float(i), frame_id=i)  # 20 s of data

    # Decimate a window that fully contains the data. Use a wide
    # window (60 s) so the visible time range exceeds the data extent
    # — matches the user-reported "zoom all the way out" scenario.
    t_a, v_a = buf.window_decimated(-40.0, 20.0, max_points=200)

    # Add one more sample (followLive advancing by one frame).
    buf.append(t=20.0, v=800.0, frame_id=800)

    # Re-decimate with the SAME window. The outputs that were already
    # painted should not change — adding a sample at the right edge
    # must not alter the decimation of the earlier samples.
    t_b, v_b = buf.window_decimated(-40.0, 20.0, max_points=200)
    n_shared = min(len(t_a), len(t_b))
    assert n_shared > 0
    np.testing.assert_array_equal(t_a[:n_shared], t_b[:n_shared])
    np.testing.assert_array_equal(v_a[:n_shared], v_b[:n_shared])


def test_dropped_at_for_finds_dropout_under_any_metric_key():
    """dropped_at_for walks all 4 metric buffers for (side, cam) and
    returns the first dropout it finds — so the marker still surfaces
    even if mean/contrast happen to be absent for early samples."""
    src = LiveScanSource(plot_t0=0.0)
    src.append_uncorrected(side="left", cam_id=3, frame_id=1, t=0.1,
                           bfi=4.0, bvi=2.0)  # mean/contrast omitted
    src.mark_dropped(side="left", cam_id=3, t=8.0)
    assert src.dropped_at_for("left", 3) == pytest.approx(8.0)


# ─────────────────────────────────────────────────────────────────────────────
# LiveScanSource DB tail (Phase 3 lazy-load)
# ─────────────────────────────────────────────────────────────────────────────


def test_live_scan_source_serves_in_memory_window_without_db(session_data_db):
    """Fast path: when the requested window is within the in-memory
    buffer, the DB tail is never consulted (even when a DB is wired)."""
    db, sid = session_data_db
    src = LiveScanSource(plot_t0=0.0)
    for i in range(10):
        src.append_uncorrected(side="left", cam_id=0, frame_id=i,
                               t=10.0 + i * 0.025, bfi=5.0, bvi=6.0)
    src._db = db            # wired but should not be hit
    src._db_session_id = sid
    pts = src.points_for_window("left", 0, "bfi", 10.0, 10.25, max_points=100)
    assert len(pts) > 0
    # All values are the in-memory 5.0, not the DB's ~1.x values.
    assert all(abs(p[1] - 5.0) < 0.01 for p in pts)


def test_live_scan_source_pans_into_db_tail(session_data_db):
    """Pan-into-past: when t_lo is before the in-memory start (data
    ring-trimmed), points_for_window serves the older range from the
    DB tail. session_data_db has (left, 0) bfi samples at t≈0..0.1;
    the in-memory buffer starts at t=10, so a request for [0, 0.1]
    must come from the DB."""
    db, sid = session_data_db
    src = LiveScanSource(plot_t0=0.0)
    # In-memory buffer simulates post-ring-trim state: starts at t=10.
    for i in range(5):
        src.append_uncorrected(side="left", cam_id=0, frame_id=2000 + i,
                               t=10.0 + i * 0.025, bfi=99.0, bvi=99.0)
    # Mark trimmed so the guard treats t<10 as "older data in DB" rather
    # than "before scan start" (the pre-trim no-DB fast path).
    for buf in src.buffers.values():
        buf.ring_trimmed = True
    src._db = db
    src._db_session_id = sid

    pts = src.points_for_window("left", 0, "bfi", 0.0, 0.1, max_points=100)
    assert len(pts) > 0
    # DB bfi values for (left, 0) are 1.0 + cam(0) + i*0.1 ≈ 1.0..1.4 —
    # definitely not the in-memory 99.0.
    assert all(p[1] < 50 for p in pts)


def test_live_scan_source_pretrim_window_before_start_skips_db(session_data_db):
    """Regression: early in a scan, followLive sets t_lo = liveEdge -
    windowSeconds which can be BELOW the first sample (window extends
    before scan start). The buffer hasn't ring-trimmed, so there's no
    older data — must serve from memory and NEVER touch the DB (the
    bug made this query the DB every paint → ~1 Hz UI updates)."""
    db, sid = session_data_db
    src = LiveScanSource(plot_t0=0.0)
    # Scan just started: first sample at t=0.25, only ~0.3 s of data.
    for i in range(12):
        src.append_uncorrected(side="left", cam_id=0, frame_id=i,
                               t=0.25 + i * 0.025, bfi=5.0, bvi=6.0)
    # Sentinel DB that explodes if queried — proves the fast path never
    # reaches the DB pre-trim.
    class _Boom:
        def iter_session_data(self, *a, **k):
            raise AssertionError("DB must not be queried before ring-trim")
        def _connection(self):
            raise AssertionError("DB must not be opened before ring-trim")
    src._db = _Boom()
    src._db_session_id = sid
    # followLive window [-14.5, 0.5]: t_lo well before the first sample.
    pts = src.points_for_window("left", 0, "bfi", -14.5, 0.5, max_points=100)
    # Served from memory (the in-scan samples), no exception raised.
    assert len(pts) > 0
    assert all(abs(p[1] - 5.0) < 0.01 for p in pts)


def test_live_scan_source_straddle_stitches_db_and_memory(session_data_db):
    """A window whose left edge is in the DB tail but whose right edge
    reaches the in-memory live data must STITCH: old portion from the DB,
    recent portion from memory. Serving the whole window from the DB (the
    old behavior) froze the live trace, because the cached DB window only
    reloads when the in-memory boundary advances. session_data_db has
    (left,0) bfi ≈1.0..1.4 at t≈0..0.1; in-memory holds 99.0 at t≈10."""
    db, sid = session_data_db
    src = LiveScanSource(plot_t0=0.0)
    for i in range(5):
        src.append_uncorrected(side="left", cam_id=0, frame_id=2000 + i,
                               t=10.0 + i * 0.025, bfi=99.0, bvi=99.0)
    for buf in src.buffers.values():
        buf.ring_trimmed = True
    src._db = db
    src._db_session_id = sid

    pts = src.points_for_window("left", 0, "bfi", 0.0, 10.1, max_points=200)
    vals = [p[1] for p in pts]
    assert any(v < 50 for v in vals), "DB-tail (old) portion missing from stitch"
    assert any(v > 50 for v in vals), "in-memory (recent) portion missing from stitch"


def test_live_scan_source_db_window_not_padded_into_future(session_data_db):
    """_ensure_db_window must not claim coverage past the newest sample.
    Padding want_hi into the future used to make the staleness check
    short-circuit and serve a stale window while live data piled up unshown."""
    db, sid = session_data_db
    src = LiveScanSource(plot_t0=0.0)
    for i in range(5):
        src.append_uncorrected(side="left", cam_id=0, frame_id=2000 + i,
                               t=10.0 + i * 0.025, bfi=99.0, bvi=99.0)
    for buf in src.buffers.values():
        buf.ring_trimmed = True
    src._db = db
    src._db_session_id = sid

    src.points_for_window("left", 0, "bfi", 0.0, 10.1, max_points=200)
    # The DB window's claimed upper bound never exceeds the newest sample.
    assert src._db_window_hi <= src.liveEdge + 1e-9


def test_live_scan_source_negative_t_lo_paint_loop_hits_cache(session_data_db):
    """Regression (issue #151): after "Back to live" with a wheel-zoomed-out
    window WIDER than the elapsed scan, followLive requests
    t_lo = liveEdge - windowSeconds < 0 on every paint. _db_window_lo is
    floored at 0 when stored, so an unclamped negative t_lo could never
    satisfy the staleness check — every points_for_window call re-queried
    and re-bucketized the DB window synchronously on the GUI thread
    (cells × metrics × 30 Hz), freezing the app until the scan ended.
    The clamp in _ensure_db_window must make the second-and-later paints
    cache hits."""
    db, sid = session_data_db

    class _CountingDb:
        def __init__(self, inner):
            self._inner = inner
            self.calls = 0

        def iter_session_data(self, *a, **k):
            self.calls += 1
            return self._inner.iter_session_data(*a, **k)

    counting = _CountingDb(db)
    src = LiveScanSource(plot_t0=0.0)
    # Post-ring-trim in-memory state: data from t=10 onward only.
    for i in range(5):
        src.append_uncorrected(side="left", cam_id=0, frame_id=2000 + i,
                               t=10.0 + i * 0.025, bfi=99.0, bvi=99.0)
    for buf in src.buffers.values():
        buf.ring_trimmed = True
    src._db = counting
    src._db_session_id = sid

    # Simulate the followLive paint loop: windowSeconds=600 ≫ liveEdge,
    # so t_lo is negative and creeps up as the live edge advances.
    edge = src.liveEdge
    for tick in range(20):
        t_hi = edge + tick * 0.033
        src.points_for_window("left", 0, "bfi", t_hi - 600.0, t_hi,
                              max_points=200)

    # One initial load (per metric buffer touched) — NOT one per paint.
    assert counting.calls <= 2, (
        f"DB re-queried {counting.calls} times across 20 paints — "
        "negative-t_lo staleness thrash is back"
    )

    # And the stitched result still contains both DB and memory data.
    pts = src.points_for_window("left", 0, "bfi", -590.0, 10.1, max_points=200)
    vals = [p[1] for p in pts]
    assert any(v < 50 for v in vals)
    assert any(v > 50 for v in vals)


def test_live_scan_source_value_at_negative_t_cache_valid(session_data_db):
    """Companion to the paint-loop regression: value_at with a negative
    cursor time (hover over the empty left margin of a wider-than-scan
    window) goes through the same _ensure_db_window clamp and must not
    shrink/clobber the cached window into an unsatisfiable range."""
    db, sid = session_data_db
    src = LiveScanSource(plot_t0=0.0)
    for i in range(5):
        src.append_uncorrected(side="left", cam_id=0, frame_id=2000 + i,
                               t=10.0 + i * 0.025, bfi=99.0, bvi=99.0)
    for buf in src.buffers.values():
        buf.ring_trimmed = True
    src._db = db
    src._db_session_id = sid

    v = src.value_at("left", 0, "bfi", -5.0)
    # Cache bounds remain a valid (lo <= hi), non-negative range.
    assert src._db_window_lo >= 0.0
    assert src._db_window_hi >= src._db_window_lo
    # Nearest stored sample (t≈0) is served — finite, from the DB.
    assert math.isfinite(v)
    assert v < 50


def test_live_scan_source_value_at_uses_db_tail(session_data_db):
    """value_at also falls through to the DB tail for times before the
    in-memory window."""
    db, sid = session_data_db
    src = LiveScanSource(plot_t0=0.0)
    for i in range(5):
        src.append_uncorrected(side="left", cam_id=0, frame_id=2000 + i,
                               t=10.0 + i * 0.025, bfi=99.0, bvi=99.0)
    for buf in src.buffers.values():
        buf.ring_trimmed = True
    src._db = db
    src._db_session_id = sid

    v = src.value_at("left", 0, "bfi", 0.0)
    assert isfinite_or_nan(v)
    assert v < 50  # DB value, not in-memory 99.0


def test_live_scan_source_db_ready_resolve_path_no_nameerror(tmp_path):
    """Regression: _db_ready's resolve path called a module-level
    `logger` that wasn't defined → NameError crash the first time the
    DB tail actually engaged (only after ring-trim, so it slipped past
    the tests that injected _db/_db_session_id directly). Exercise the
    REAL resolve path against a temp DB with a session row."""
    from omotion.ScanDatabase import ScanDatabase
    import time as _t
    db_path = str(tmp_path / "scan.db")
    db = ScanDatabase(db_path=db_path)
    sid = db.create_session(session_label="regress", session_start=_t.time(),
                            session_notes=None, session_meta={})
    db.close()

    src = LiveScanSource(plot_t0=0.0, scan_db_path=db_path)
    # Opens the DB, resolves session via MAX(id), hits the logger.info
    # line that used to NameError. Must return True without raising.
    assert src._db_ready() is True
    assert src._db_session_id == sid


def test_live_scan_source_resolves_session_by_label_not_newest(tmp_path):
    """With a scan label bound, the DB tail resolves THIS scan's session row
    by exact label — even when a NEWER (other) session exists. MAX(id) alone
    would wrongly pick the newer one."""
    from omotion.ScanDatabase import ScanDatabase
    import time as _t
    db_path = str(tmp_path / "scan.db")
    db = ScanDatabase(db_path=db_path)
    mine = db.create_session(session_label="20260101_000000_subjA",
                             session_start=_t.time(), session_notes=None,
                             session_meta={})
    # A newer, unrelated session (higher id, later start).
    db.create_session(session_label="20260101_000500_subjB",
                      session_start=_t.time() + 5, session_notes=None,
                      session_meta={})
    db.close()

    src = LiveScanSource(plot_t0=0.0, scan_db_path=db_path)
    src.set_scan_label("20260101_000000_subjA")
    assert src._db_ready() is True
    assert src._db_session_id == mine  # the labelled one, not the newest


def test_live_scan_source_soft_retry_when_session_not_yet_present(tmp_path):
    """A not-yet-visible session row is a SOFT miss: _db_ready returns False
    without permanently disabling the tail, and a later call (once the row
    exists) succeeds. Guards against the old one-failure permanent disable."""
    from omotion.ScanDatabase import ScanDatabase
    import time as _t
    db_path = str(tmp_path / "scan.db")
    ScanDatabase(db_path=db_path).close()  # create schema, no sessions yet

    src = LiveScanSource(plot_t0=0.0, scan_db_path=db_path)
    src.set_scan_label("20260101_000000_subjA")
    assert src._db_ready() is False          # session row not there yet
    assert src._db_unavailable is False      # NOT permanently disabled

    # The session appears (as it would at scan start).
    db = ScanDatabase(db_path=db_path)
    sid = db.create_session(session_label="20260101_000000_subjA",
                            session_start=_t.time(), session_notes=None,
                            session_meta={})
    db.close()

    assert src._db_ready() is True           # retry now resolves it
    assert src._db_session_id == sid


def test_live_scan_source_db_tail_disabled_without_path():
    """No scan_db_path → DB tail permanently unavailable; pan-into-past
    returns empty rather than erroring."""
    src = LiveScanSource(plot_t0=0.0)  # no scan_db_path
    for i in range(5):
        src.append_uncorrected(side="left", cam_id=0, frame_id=2000 + i,
                               t=10.0 + i * 0.025, bfi=5.0, bvi=6.0)
    # Trimmed + pan before start, but no DB path configured → falls
    # through to the in-memory base impl, which has nothing for that
    # window (buffer starts at t=10) → empty.
    for buf in src.buffers.values():
        buf.ring_trimmed = True
    pts = src.points_for_window("left", 0, "bfi", 0.0, 0.1, max_points=100)
    assert pts == []


def isfinite_or_nan(v):
    return math.isfinite(v) or math.isnan(v)
