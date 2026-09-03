"""Unit tests for plot pair mode (issue #289) — row-pair averaging in
data_sources.py.

Pair mode collapses each U-shape grid row's two cameras (1+8, 2+7, 3+6,
4+5 per side, 1-based) into one panel rendering the mean of the two
cameras' BFI/BVI. The averaging lives Python-side:

  - merge_pair_points()       — common-grid resampled mean of two
                                decimated [t, v] series with per-camera
                                gap gating (a dead camera stops
                                contributing where its data ends).
  - pair_points_for_window()  — QML-facing slot; rides the polymorphic
                                points_for_window so DB-tail / replay
                                behavior is inherited.
  - pair_value_at()           — staleness-gated mean for the cell value
                                labels + hover tooltip.
  - _CameraBuffer.sample_near — (t, v) of the nearest sample, backing
                                the staleness gate.

The QML grid-model half of the feature (cell pairing/labels/fallback
layout) is tested in tests/test_plot_viewer_masks.py.
"""

import math

import pytest

from data_sources import (
    LiveScanSource,
    _CameraBuffer,
    merge_pair_points,
)

pytestmark = pytest.mark.unit


def _pts(value, t0=0.0, t1=10.0, hz=40.0):
    """Constant-valued [t, v] series over [t0, t1) at `hz` — the shape
    points_for_window returns for an undecimated window."""
    n = int(round((t1 - t0) * hz))
    return [[t0 + i / hz, value] for i in range(n)]


# ─────────────────────────────────────────────────────────────────────────────
# merge_pair_points
# ─────────────────────────────────────────────────────────────────────────────

def test_merge_averages_two_full_series():
    merged = merge_pair_points(_pts(1.0), _pts(3.0), 0.0, 10.0, 1000)
    assert len(merged) > 0
    assert all(v == pytest.approx(2.0) for _, v in merged)
    assert all(0.0 <= t <= 10.0 for t, _ in merged)


def test_merge_empty_side_passes_other_through_verbatim():
    a = _pts(1.0)
    assert merge_pair_points(a, [], 0.0, 10.0, 1000) is a
    assert merge_pair_points([], a, 0.0, 10.0, 1000) is a
    assert merge_pair_points([], [], 0.0, 10.0, 1000) == []


def test_merge_respects_point_budget():
    merged = merge_pair_points(
        _pts(1.0, t1=60.0), _pts(3.0, t1=60.0), 0.0, 60.0, 100)
    # Same stride math as window_decimated: the count can exceed the
    # budget by at most the fencepost sample.
    assert 0 < len(merged) <= 101
    assert all(v == pytest.approx(2.0) for _, v in merged)


def test_merge_dropped_camera_falls_back_to_survivor():
    """Camera B stops delivering mid-window: grid points past its last
    sample (+ gap) must carry camera A's value alone — the pair trace
    never goes blank because one member died."""
    a = _pts(1.0, t1=10.0)
    b = _pts(3.0, t1=5.0)  # B dies at t=5
    merged = merge_pair_points(a, b, 0.0, 10.0, 1000)
    early = [v for t, v in merged if t < 4.5]
    late = [v for t, v in merged if t > 5.5]
    assert early and late
    assert all(v == pytest.approx(2.0) for v in early)
    assert all(v == pytest.approx(1.0) for v in late)


def test_merge_interior_gap_not_bridged():
    """Camera B has an interior dropout [3, 7]: inside the gap only A
    contributes; interp must not flat-line B across it."""
    a = _pts(1.0, t1=10.0)
    b = [p for p in _pts(3.0, t1=10.0) if not (3.0 <= p[0] <= 7.0)]
    merged = merge_pair_points(a, b, 0.0, 10.0, 1000)
    inside = [v for t, v in merged if 3.5 < t < 6.5]
    outside = [v for t, v in merged if t < 2.5 or t > 7.5]
    assert inside and outside
    assert all(v == pytest.approx(1.0) for v in inside)
    assert all(v == pytest.approx(2.0) for v in outside)


def test_merge_all_nan_partner_yields_survivor_values():
    a = _pts(1.0)
    b = [[t, float("nan")] for t, _ in _pts(0.0)]
    merged = merge_pair_points(a, b, 0.0, 10.0, 1000)
    assert len(merged) > 0
    assert all(v == pytest.approx(1.0) for _, v in merged)


def test_merge_grid_is_absolute_time_aligned():
    """Output timestamps sit at absolute multiples of the output spacing,
    so a panning window re-lands points on the same grid (no morphing).
    Windows chosen so both resolve the same stride."""
    a = _pts(1.0, t1=20.0)
    b = _pts(3.0, t1=20.0)
    m1 = merge_pair_points(a, b, 0.0, 20.0, 100)      # stride 8 → 0.2 s
    m2 = merge_pair_points(a, b, 0.55, 20.0, 100)     # stride 8 → 0.2 s
    t1s = {round(t, 6) for t, _ in m1}
    t2s = {round(t, 6) for t, _ in m2}
    assert t2s <= t1s  # the shifted window's grid is a subset
    v1 = {round(t, 6): v for t, v in m1}
    for t, v in m2:
        assert v == pytest.approx(v1[round(t, 6)])


def test_merge_no_overlap_with_window_returns_empty():
    a = _pts(1.0, t0=100.0, t1=101.0)
    b = _pts(3.0, t0=100.0, t1=101.0)
    assert merge_pair_points(a, b, 0.0, 0.001, 100) == []


# ─────────────────────────────────────────────────────────────────────────────
# _CameraBuffer.sample_near
# ─────────────────────────────────────────────────────────────────────────────

def test_sample_near_empty_buffer_is_nan_nan():
    buf = _CameraBuffer(initial_capacity=4)
    t, v = buf.sample_near(1.0)
    assert math.isnan(t) and math.isnan(v)


def test_sample_near_returns_nearest_sample_pair():
    buf = _CameraBuffer(initial_capacity=4)
    buf.append(t=0.5, v=1.0, frame_id=0)
    buf.append(t=1.0, v=2.0, frame_id=1)
    assert buf.sample_near(0.6) == (0.5, 1.0)
    assert buf.sample_near(0.9) == (1.0, 2.0)
    assert buf.sample_near(-5.0) == (0.5, 1.0)   # clamped to oldest
    assert buf.sample_near(50.0) == (1.0, 2.0)   # clamped to newest


def test_sample_near_passes_nan_value_through():
    """Unlike value_near, sample_near returns the stored value verbatim
    (with its timestamp) so callers can distinguish 'no sample near t'
    from 'the nearest sample is non-finite'."""
    buf = _CameraBuffer(initial_capacity=4)
    buf.append(t=2.0, v=float("nan"), frame_id=0)
    t, v = buf.sample_near(2.0)
    assert t == 2.0 and math.isnan(v)


# ─────────────────────────────────────────────────────────────────────────────
# ScanDataSource.pair_points_for_window (via LiveScanSource, in-memory)
# ─────────────────────────────────────────────────────────────────────────────

def _feed(src, side, cam_id, value, t0=0.0, t1=10.0, hz=40.0):
    n = int(round((t1 - t0) * hz))
    for i in range(n):
        src.append_uncorrected(side=side, cam_id=cam_id, frame_id=i,
                               t=t0 + i / hz, bfi=value, bvi=value + 10.0)


def test_pair_points_averages_row_pair():
    src = LiveScanSource(plot_t0=0.0)
    _feed(src, "left", 0, 1.0)
    _feed(src, "left", 7, 3.0)
    pts = src.pair_points_for_window("left", 0, 7, "bfi", 0.0, 10.0, 1000)
    assert len(pts) > 0
    assert all(v == pytest.approx(2.0) for _, v in pts)


def test_pair_points_missing_partner_matches_single_camera():
    """A pair whose second camera never delivered (masked out at the
    firmware level, dead from t=0, …) renders EXACTLY the survivor's
    own single-camera series."""
    src = LiveScanSource(plot_t0=0.0)
    _feed(src, "left", 0, 1.5)
    single = src.points_for_window("left", 0, "bfi", 0.0, 10.0, 1000)
    paired = src.pair_points_for_window("left", 0, 7, "bfi", 0.0, 10.0, 1000)
    assert paired == single
    assert len(paired) > 0


def test_pair_points_both_missing_is_empty():
    src = LiveScanSource(plot_t0=0.0)
    assert src.pair_points_for_window(
        "left", 0, 7, "bfi", 0.0, 10.0, 1000) == []


def test_pair_points_partner_dropout_survivor_continues():
    src = LiveScanSource(plot_t0=0.0)
    _feed(src, "right", 1, 2.0, t1=10.0)
    _feed(src, "right", 6, 4.0, t1=5.0)  # dies halfway
    pts = src.pair_points_for_window("right", 1, 6, "bfi", 0.0, 10.0, 1000)
    early = [v for t, v in pts if t < 4.5]
    late = [v for t, v in pts if t > 5.5]
    assert early and late
    assert all(v == pytest.approx(3.0) for v in early)
    assert all(v == pytest.approx(2.0) for v in late)


# ─────────────────────────────────────────────────────────────────────────────
# ScanDataSource.pair_value_at
# ─────────────────────────────────────────────────────────────────────────────

def test_pair_value_at_means_fresh_cameras():
    src = LiveScanSource(plot_t0=0.0)
    _feed(src, "left", 0, 1.0)
    _feed(src, "left", 7, 3.0)
    assert src.pair_value_at("left", 0, 7, "bfi", 9.9) == pytest.approx(2.0)
    # bvi streams got value + 10.
    assert src.pair_value_at("left", 0, 7, "bvi", 9.9) == pytest.approx(12.0)


def test_pair_value_at_excludes_stale_camera():
    """Camera 8 stalled 5 s ago: its last value must NOT pin the pair
    readout — only the delivering camera counts."""
    src = LiveScanSource(plot_t0=0.0)
    _feed(src, "left", 0, 1.0, t1=10.0)
    _feed(src, "left", 7, 3.0, t1=5.0)
    assert src.pair_value_at("left", 0, 7, "bfi", 9.9) == pytest.approx(1.0)


def test_pair_value_at_missing_partner_uses_survivor():
    src = LiveScanSource(plot_t0=0.0)
    _feed(src, "left", 0, 1.0)
    assert src.pair_value_at("left", 0, 7, "bfi", 9.9) == pytest.approx(1.0)


def test_pair_value_at_nothing_fresh_is_nan():
    src = LiveScanSource(plot_t0=0.0)
    _feed(src, "left", 0, 1.0, t1=10.0)
    _feed(src, "left", 7, 3.0, t1=10.0)
    # Query 5 s past the last sample — both stale → NaN (renders "--").
    assert math.isnan(src.pair_value_at("left", 0, 7, "bfi", 15.0))


def test_pair_value_at_no_buffers_is_nan():
    src = LiveScanSource(plot_t0=0.0)
    assert math.isnan(src.pair_value_at("left", 0, 7, "bfi", 1.0))


def test_pair_value_at_nan_nearest_sample_excluded():
    """The nearest sample being non-finite excludes that camera (same
    semantics as value_at), leaving the finite partner."""
    src = LiveScanSource(plot_t0=0.0)
    _feed(src, "left", 0, 1.0)
    buf = src.get_or_create_buffer("left", 7, "bfi")
    buf.append(t=9.9, v=float("nan"), frame_id=0)
    assert src.pair_value_at("left", 0, 7, "bfi", 9.9) == pytest.approx(1.0)
