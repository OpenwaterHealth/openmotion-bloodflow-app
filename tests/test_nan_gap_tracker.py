"""Unit tests for NanGapTracker — sustained-gap detection over finite
BFI/BVI sample timestamps (spec: 2026-06-11-nan-gap-notes-footer)."""

import pytest

from nan_gap_tracker import NanGapTracker, format_gaps, gap_note_line

pytestmark = pytest.mark.unit

LEFT0 = ("left", 0)
LEFT1 = ("left", 1)
RIGHT0 = ("right", 0)


def _feed(tracker, key, times):
    for t in times:
        tracker.record(key, t)


def test_healthy_40hz_stream_has_no_gaps():
    tr = NanGapTracker()
    # 4 s of continuous 40 Hz samples (25 ms apart) — no gap anywhere.
    _feed(tr, LEFT0, [i * 0.025 for i in range(160)])
    assert tr.merged_gaps() == []


def test_single_dark_frame_skip_is_not_a_gap():
    tr = NanGapTracker()
    # One missing 25 ms sample (a dark-frame NaN) — interval is 50 ms,
    # far below the 1 s threshold.
    _feed(tr, LEFT0, [0.0, 0.025, 0.075, 0.1])
    assert tr.merged_gaps() == []


def test_gap_longer_than_threshold_is_recorded():
    tr = NanGapTracker()
    _feed(tr, LEFT0, [0.0, 0.025, 0.05])
    _feed(tr, LEFT0, [3.05, 3.075])  # 3 s of silence mid-scan
    gaps = tr.merged_gaps()
    assert gaps == [(pytest.approx(0.05), pytest.approx(3.05))]


def test_gap_exactly_at_threshold_is_not_recorded():
    tr = NanGapTracker(min_gap_s=1.0)
    _feed(tr, LEFT0, [0.0, 1.0, 2.0])  # exactly 1.0 s spacing — not > 1.0
    assert tr.merged_gaps() == []


def test_overlapping_gaps_across_cameras_merge_into_one_range():
    # Camera A streams 0→1 and 4→7 (silent 1.0→4.0); camera B streams
    # 0→2 and 6→7 (silent 2.0→6.0). Overlapping gaps union to 1.0→6.0.
    tr = NanGapTracker()
    _feed(tr, LEFT0, [i * 0.025 for i in range(41)])            # 0..1.0
    _feed(tr, LEFT0, [4.0 + i * 0.025 for i in range(121)])     # 4..7.0
    _feed(tr, LEFT1, [i * 0.025 for i in range(81)])            # 0..2.0
    _feed(tr, LEFT1, [6.0 + i * 0.025 for i in range(41)])      # 6..7.0
    gaps = tr.merged_gaps()
    assert len(gaps) == 1
    lo, hi = gaps[0]
    assert lo == pytest.approx(1.0)
    assert hi == pytest.approx(6.0)


def test_adjacent_gaps_sharing_an_endpoint_chain_merge():
    # One camera: gaps 1.0→4.0, 4.0→10.0, 10.0→13.0 all share touching
    # endpoints (single isolated samples at t=4 and t=10), so the union
    # chain-merges into one 1.0→13.0 range.
    tr = NanGapTracker()
    _feed(tr, LEFT0, [0.0, 1.0, 4.0, 10.0, 13.0, 14.0])
    assert tr.merged_gaps() == [(pytest.approx(1.0), pytest.approx(13.0))]


def test_disjoint_gaps_across_cameras_stay_separate():
    # LEFT0 streams 0→1 then 4→14 (silent 1.0→4.0); RIGHT0 streams 0→8
    # then 12→14 (silent 8.0→12.0). Two separate aggregate ranges.
    tr = NanGapTracker()
    _feed(tr, LEFT0, [i * 0.025 for i in range(41)])          # 0..1.0
    _feed(tr, LEFT0, [4.0 + i * 0.025 for i in range(401)])   # 4..14.0
    _feed(tr, RIGHT0, [i * 0.025 for i in range(321)])        # 0..8.0
    _feed(tr, RIGHT0, [12.0 + i * 0.025 for i in range(81)])  # 12..14.0
    assert tr.merged_gaps() == [
        (pytest.approx(1.0), pytest.approx(4.0)),
        (pytest.approx(8.0), pytest.approx(12.0)),
    ]


def test_disjoint_gaps_on_one_camera():
    tr = NanGapTracker()
    _feed(tr, LEFT0, [0.0, 0.5, 3.0, 3.5, 4.0, 9.0, 9.5])
    assert tr.merged_gaps() == [
        (pytest.approx(0.5), pytest.approx(3.0)),
        (pytest.approx(4.0), pytest.approx(9.0)),
    ]


def test_trailing_gap_closed_at_global_end():
    tr = NanGapTracker()
    # LEFT0 stops at t=10; RIGHT0 keeps streaming to t=60.
    _feed(tr, LEFT0, [0.0 + i * 0.025 for i in range(401)])    # 0..10.0
    _feed(tr, RIGHT0, [0.0 + i * 0.025 for i in range(2401)])  # 0..60.0
    gaps = tr.merged_gaps()
    assert len(gaps) == 1
    lo, hi = gaps[0]
    assert lo == pytest.approx(10.0)
    assert hi == pytest.approx(60.0)


def test_trailing_gap_respects_explicit_end_t():
    tr = NanGapTracker()
    _feed(tr, LEFT0, [0.0, 0.5, 1.0])
    assert tr.merged_gaps(end_t=5.0) == [(pytest.approx(1.0), pytest.approx(5.0))]
    # Default end (global max = 1.0) → no trailing gap.
    assert tr.merged_gaps() == []


def test_leading_warmup_is_not_a_gap():
    tr = NanGapTracker()
    # First finite sample arrives at t=2.0 — no gap before it.
    _feed(tr, LEFT0, [2.0 + i * 0.025 for i in range(80)])
    assert tr.merged_gaps() == []


def test_empty_tracker_returns_no_gaps():
    tr = NanGapTracker()
    assert tr.merged_gaps() == []
    assert tr.t0 is None


def test_t0_is_first_finite_timestamp_across_keys():
    tr = NanGapTracker()
    tr.record(RIGHT0, 5.0)
    tr.record(LEFT0, 3.0)
    assert tr.t0 == pytest.approx(3.0)


def test_record_ignores_non_finite_t():
    tr = NanGapTracker()
    tr.record(LEFT0, float("nan"))
    tr.record(LEFT0, float("inf"))
    assert tr.t0 is None
    assert tr.merged_gaps() == []


def test_side_average_keys_work_like_camera_keys():
    # Clinical mode records under cam_id=-1 — just another key.
    tr = NanGapTracker()
    _feed(tr, ("left", -1), [0.0, 0.025, 2.0, 2.025])
    assert tr.merged_gaps() == [(pytest.approx(0.025), pytest.approx(2.0))]


def test_format_gaps_is_scan_relative_one_decimal():
    # t0=2.0 → ranges shift down by 2.0 and render with 1 decimal.
    s = format_gaps([(14.4, 17.8), (49.0, 51.2)], t0=2.0)
    assert s == "12.4–15.8s, 47.0–49.2s"


def test_format_gaps_with_none_t0_treats_zero_as_origin():
    assert format_gaps([(1.0, 2.5)], t0=None) == "1.0–2.5s"


def test_gap_note_line_empty_when_no_gaps():
    tr = NanGapTracker()
    _feed(tr, LEFT0, [i * 0.025 for i in range(80)])
    assert gap_note_line(tr) == ""
    assert gap_note_line(NanGapTracker()) == ""  # never recorded


def test_gap_note_line_formats_threshold_and_ranges():
    tr = NanGapTracker()
    _feed(tr, LEFT0, [0.0, 0.5, 3.5, 4.0])
    line = gap_note_line(tr)
    assert line == "\nData gaps (>1.0s): 0.5–3.5s"
