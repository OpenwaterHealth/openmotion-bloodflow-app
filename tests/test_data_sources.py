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
