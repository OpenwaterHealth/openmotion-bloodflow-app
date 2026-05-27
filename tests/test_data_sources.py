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


def test_camera_buffer_apply_corrected_overwrites_in_place():
    buf = _CameraBuffer(initial_capacity=8)
    buf.append(t=0.0, v=1.0, frame_id=100)
    buf.append(t=0.025, v=2.0, frame_id=101)
    buf.append(t=0.050, v=3.0, frame_id=102)

    buf.apply_corrected(frame_id=101, value=99.9)

    assert buf.v[0] == np.float32(1.0)
    assert buf.v[1] == np.float32(99.9)
    assert buf.v[2] == np.float32(3.0)


def test_camera_buffer_apply_corrected_unknown_frame_is_noop():
    buf = _CameraBuffer(initial_capacity=8)
    buf.append(t=0.0, v=1.0, frame_id=100)
    # frame_id 999 was never appended — must not raise, must not corrupt state
    buf.apply_corrected(frame_id=999, value=42.0)
    assert buf.n == 1
    assert buf.v[0] == np.float32(1.0)


def test_camera_buffer_apply_corrected_skips_sentinel_frame_id():
    """frame_id=-1 means 'unknown'; we don't index it, so apply_corrected(-1, ...)
    must not silently overwrite the most-recent -1 sample."""
    buf = _CameraBuffer(initial_capacity=8)
    buf.append(t=0.0, v=1.0, frame_id=-1)
    buf.append(t=0.025, v=2.0, frame_id=-1)
    buf.apply_corrected(frame_id=-1, value=99.9)
    assert buf.v[0] == np.float32(1.0)
    assert buf.v[1] == np.float32(2.0)


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


def test_live_scan_source_apply_corrected_batch_overwrites_in_place():
    src = LiveScanSource(plot_t0=0.0)
    # Seed live samples for frame_ids 100, 101, 102.
    for i, fid in enumerate((100, 101, 102)):
        src.append_uncorrected(
            side="left", cam_id=0, frame_id=fid, t=i * 0.025,
            bfi=1.0 + i, bvi=10.0 + i, mean=100.0 + i, contrast=0.30 + i * 0.01,
        )
    src._flush()  # drain seed dirty state

    batch = [
        {"side": "left", "camId": 0, "frameId": 101, "ts": 0.025,
         "bfi": 99.0, "bvi": 88.0, "mean": 77.0, "contrast": 0.66},
    ]
    src.apply_corrected_batch(batch)

    bfi_buf = src.buffers[("left", 0, "bfi")]
    bvi_buf = src.buffers[("left", 0, "bvi")]
    mean_buf = src.buffers[("left", 0, "mean")]
    contrast_buf = src.buffers[("left", 0, "contrast")]

    assert bfi_buf.v[1] == np.float32(99.0)   # overwritten
    assert bfi_buf.v[0] == np.float32(1.0)    # untouched
    assert bfi_buf.v[2] == np.float32(3.0)    # untouched
    assert bvi_buf.v[1] == np.float32(88.0)
    assert mean_buf.v[1] == np.float32(77.0)
    assert contrast_buf.v[1] == np.float32(0.66)


def test_live_scan_source_apply_corrected_batch_unknown_frame_silently_skipped():
    src = LiveScanSource(plot_t0=0.0)
    src.append_uncorrected(
        side="left", cam_id=0, frame_id=100, t=0.0,
        bfi=1.0, bvi=10.0,
    )
    # frame_id 999 was never seen on the live path (race window)
    src.apply_corrected_batch([
        {"side": "left", "camId": 0, "frameId": 999, "ts": 0.0,
         "bfi": 99.0, "bvi": 88.0, "mean": 77.0, "contrast": 0.66},
    ])
    assert src.buffers[("left", 0, "bfi")].v[0] == np.float32(1.0)
    assert ("left", 0, "mean") not in src.buffers


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

    def iter_session_data(self, session_id: int, side=None, cam_id=None):
        sql = "SELECT * FROM session_data WHERE session_id = ?"
        bindings = [session_id]
        if side is not None:
            sql += " AND side = ?"
            bindings.append(side)
        if cam_id is not None:
            sql += " AND cam_id = ?"
            bindings.append(cam_id)
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

    # 4 (side, cam) combos × 4 metrics = 16 buffers expected
    expected_keys = {
        (side, cam, metric)
        for side in ("left", "right")
        for cam in (0, 3)
        for metric in ("bfi", "bvi", "mean", "contrast")
    }
    assert set(src.buffers.keys()) == expected_keys

    # Each buffer holds the 5 rows for its (side, cam)
    for key, buf in src.buffers.items():
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
# currentScanSource holder pattern — verified via a parallel mini-class
# (MOTIONConnector uses the same pattern; see motion_connector.py)
# ─────────────────────────────────────────────────────────────────────────────

from PyQt6.QtCore import QObject, pyqtSignal
from data_sources import LiveScanSource, ScanDataSource


def _make_holder():
    """Returns a fresh QObject with the same currentScanSource surface as
    MOTIONConnector. Kept in-test rather than exported so production code
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


def test_camera_buffer_window_decimated_returns_unstrided_when_under_max():
    buf = _CameraBuffer(initial_capacity=64)
    for i in range(20):
        buf.append(t=i * 0.025, v=float(i), frame_id=i)
    t_dec, v_dec = buf.window_decimated(t_lo=0.0, t_hi=0.5, max_points=100)
    # 20 samples in window, max_points=100 → no decimation
    assert len(t_dec) == 20
    assert list(v_dec) == [np.float32(i) for i in range(20)]


def test_camera_buffer_window_decimated_strides_when_over_max():
    buf = _CameraBuffer(initial_capacity=1024)
    for i in range(400):
        buf.append(t=i * 0.025, v=float(i), frame_id=i)
    # Whole window (10 s), max_points=100 → stride = ceil(400/100) = 4
    t_dec, v_dec = buf.window_decimated(t_lo=0.0, t_hi=10.0, max_points=100)
    assert len(t_dec) == 100
    assert list(v_dec[:5]) == [np.float32(0), np.float32(4), np.float32(8), np.float32(12), np.float32(16)]


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
    # Window covers indices [10, 21) per the existing window_indices test.
    t_dec, v_dec = buf.window_decimated(t_lo=0.250, t_hi=0.500, max_points=100)
    assert len(t_dec) == 11  # indices 10..20 inclusive
    assert list(v_dec) == [np.float32(i) for i in range(10, 21)]
