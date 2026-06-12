# NaN-Gap Notes Footer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a scan completes, append a footer line to the session notes listing aggregate timestamp ranges where finite BFI/BVI data was lost for >1 s (e.g. `Data gaps (>1.0s): 12.4–15.8s, 47.0–49.2s`).

**Architecture:** A new pure-Python `NanGapTracker` (no Qt) records the timestamp of every *finite* BFI/BVI sample per `(side, cam_id)` key as it flows through `_LivePlotSink`. A gap is recorded when a key's consecutive finite samples are >1 s apart; trailing gaps (camera stops before scan end) are closed against the global max timestamp. At completion, per-key gaps are unioned into merged ranges and appended to `scanNotes` before it is emitted and persisted. `NotesModal.qml` needs no changes — it renders `MotionInterface.scanNotes` as-is.

**Tech Stack:** Python 3.13, PyQt6 (connector only — tracker itself is Qt-free), pytest with the repo's `unit` marker (`tests/pytest.ini`; conftest autouse fixtures short-circuit on this marker, so no app launch).

**Spec:** `docs/superpowers/specs/2026-06-11-nan-gap-notes-footer-design.md`

## File structure

- **Create** `nan_gap_tracker.py` — repo root, beside `motion_config.py` (the precedent for extracting logic out of the 4000-line `motion_connector.py`). Contains `NanGapTracker`, `format_gaps()`, `gap_note_line()`.
- **Create** `tests/test_nan_gap_tracker.py` — pure unit tests for the tracker and formatters.
- **Modify** `motion_connector.py` — three small touches: `_LivePlotSink` gains an optional `nan_gap_tracker` parameter and records finite samples; `startCapture` creates a fresh tracker per scan and passes it to the sink; `_on_pipeline_complete` appends the gap line.
- **Modify** `tests/test_live_plot_sink.py` — new tests proving the sink records into the tracker.

Key behavioral decisions locked in the spec:

- Threshold is a hardcoded `MIN_GAP_S = 1.0` module constant — **no config knob**.
- Aggregate granularity: union of per-key gap intervals, merged.
- `record()` is called **after** the finiteness check but **before** the camera-dropout gate — gaps reflect actual absence of finite pipeline data, not display suppression.
- Leading gaps (before a key's first finite sample) are never recorded (warmup is expected). Trailing gaps are closed at `merged_gaps()` time using the global max timestamp seen.
- Reduced mode feeds per-side-average keys (`cam_id = -1`); full mode feeds per-camera keys. Both paths record.
- No footer line when there are no gaps.

All commands below run from the repo root (the worktree). Tests use `python -m pytest tests/<file> -v`.

---

### Task 1: `NanGapTracker` core (record / t0 / merged_gaps)

**Files:**
- Create: `nan_gap_tracker.py`
- Create: `tests/test_nan_gap_tracker.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_nan_gap_tracker.py`:

```python
"""Unit tests for NanGapTracker — sustained-gap detection over finite
BFI/BVI sample timestamps (spec: 2026-06-11-nan-gap-notes-footer)."""

import pytest

from nan_gap_tracker import NanGapTracker

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
    tr = NanGapTracker()
    # Camera A silent 1.0→4.0, camera B silent 2.0→6.0 — union is 1.0→6.0.
    _feed(tr, LEFT0, [0.0, 1.0, 4.0, 7.0])
    _feed(tr, LEFT1, [0.0, 2.0, 6.0, 7.0])
    gaps = tr.merged_gaps()
    assert len(gaps) == 1
    lo, hi = gaps[0]
    assert lo == pytest.approx(1.0)
    assert hi == pytest.approx(6.0)


def test_adjacent_gaps_sharing_an_endpoint_merge():
    # One camera: gaps 1.0→4.0 and 4.0→10.0 share the endpoint 4.0,
    # so the union is a single 1.0→10.0 range.
    tr = NanGapTracker()
    _feed(tr, LEFT0, [0.0, 1.0, 4.0, 10.0, 13.0, 14.0])
    gaps = tr.merged_gaps()
    assert len(gaps) == 2  # 1→10 merged, plus 10→13
    assert gaps[0] == (pytest.approx(1.0), pytest.approx(10.0))
    assert gaps[1] == (pytest.approx(10.0), pytest.approx(13.0))


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
    # Reduced mode records under cam_id=-1 — just another key.
    tr = NanGapTracker()
    _feed(tr, ("left", -1), [0.0, 0.025, 2.0, 2.025])
    assert tr.merged_gaps() == [(pytest.approx(0.025), pytest.approx(2.0))]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_nan_gap_tracker.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'nan_gap_tracker'`

- [ ] **Step 3: Write the implementation**

Create `nan_gap_tracker.py`:

```python
"""Sustained-gap detection over finite BFI/BVI sample timestamps.

During a scan, _LivePlotSink (motion_connector.py) calls record() for every
sample whose BFI and BVI are both finite, keyed by (side, cam_id) — cam_id is
-1 for the reduced-mode per-side average. NaN is routine in this data (every
dark frame, early warmup frames), but light frames flow at ~40 Hz, so during
healthy capture each key's finite timestamp advances every ~25 ms. A stretch
where a key produced no finite sample for longer than MIN_GAP_S therefore
means data was genuinely lost (camera dropout, stall, sustained NaN-fill).

At scan completion the connector unions the per-key gaps into aggregate
ranges (a range = "at least one camera had no finite data here") and appends
them to the session notes via gap_note_line().

Pure Python, no Qt — unit-testable without hardware
(tests/test_nan_gap_tracker.py). Extracted per the motion_config.py
precedent rather than growing motion_connector.py further.

Spec: docs/superpowers/specs/2026-06-11-nan-gap-notes-footer-design.md
"""

from __future__ import annotations

import math
from typing import Hashable, Optional

# Minimum silence (seconds) between consecutive finite samples of one key
# for the stretch to count as a gap. ~40 missed frames at 40 Hz. A single
# dark-frame NaN produces a ~50 ms interval and never registers.
MIN_GAP_S = 1.0


class NanGapTracker:
    """Tracks per-key finite-sample timestamps and the gaps between them.

    Timestamps are the pipeline's plot timestamps (sensor firmware clock,
    seconds). Leading gaps — before a key's first finite sample — are never
    recorded: that is warmup, expected. Trailing gaps — a key falling silent
    before the scan's last data — are closed at merged_gaps() time against
    the global max timestamp seen (or an explicit end_t).
    """

    def __init__(self, min_gap_s: float = MIN_GAP_S):
        self.min_gap_s = float(min_gap_s)
        self._last: dict[Hashable, float] = {}
        self._gaps: list[tuple[float, float]] = []
        self._t0: Optional[float] = None
        self._t_max: Optional[float] = None

    @property
    def t0(self) -> Optional[float]:
        """First finite timestamp seen across all keys (scan-relative zero
        for display), or None if nothing was recorded."""
        return self._t0

    def record(self, key: Hashable, t: float) -> None:
        """Note one finite sample for `key` at time `t`. Non-finite `t` is
        ignored defensively (callers already gate on finiteness)."""
        if not math.isfinite(t):
            return
        if self._t0 is None or t < self._t0:
            self._t0 = t
        if self._t_max is None or t > self._t_max:
            self._t_max = t
        last = self._last.get(key)
        if last is not None and (t - last) > self.min_gap_s:
            self._gaps.append((last, t))
        self._last[key] = t

    def merged_gaps(self, end_t: Optional[float] = None) -> list[tuple[float, float]]:
        """Union of all per-key gap intervals, sorted and merged.

        end_t closes trailing gaps: any key whose last finite sample is more
        than min_gap_s before end_t contributes (last, end_t). Defaults to
        the global max timestamp seen, so a camera that died mid-scan shows
        a gap running to the end of the data.
        """
        if end_t is None:
            end_t = self._t_max
        gaps = list(self._gaps)
        if end_t is not None:
            for last in self._last.values():
                if (end_t - last) > self.min_gap_s:
                    gaps.append((last, end_t))
        if not gaps:
            return []
        gaps.sort()
        merged = [gaps[0]]
        for lo, hi in gaps[1:]:
            if lo <= merged[-1][1]:
                if hi > merged[-1][1]:
                    merged[-1] = (merged[-1][0], hi)
            else:
                merged.append((lo, hi))
        return merged
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_nan_gap_tracker.py -v`
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add nan_gap_tracker.py tests/test_nan_gap_tracker.py
git commit -m "feat: NanGapTracker — sustained finite-sample gap detection"
```

---

### Task 2: `format_gaps` and `gap_note_line`

**Files:**
- Modify: `nan_gap_tracker.py` (append two functions)
- Modify: `tests/test_nan_gap_tracker.py` (append tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_nan_gap_tracker.py` (and extend the import line at the
top to `from nan_gap_tracker import NanGapTracker, format_gaps, gap_note_line`):

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_nan_gap_tracker.py -v`
Expected: the four new tests FAIL with `ImportError: cannot import name 'format_gaps'`

- [ ] **Step 3: Write the implementation**

Append to `nan_gap_tracker.py`:

```python
def format_gaps(ranges: list[tuple[float, float]], t0: Optional[float]) -> str:
    """Render ranges as scan-relative '12.4–15.8s, 47.0–49.2s' (1 decimal).
    t0 is the scan's first finite timestamp (NanGapTracker.t0); None means
    timestamps are already zero-based."""
    origin = t0 or 0.0
    return ", ".join(
        f"{lo - origin:.1f}–{hi - origin:.1f}s" for lo, hi in ranges
    )


def gap_note_line(tracker: NanGapTracker) -> str:
    """The complete notes-footer line for this scan's gaps, starting with a
    newline so the caller can append it directly after the duration line.
    Empty string when there were no gaps (the common case — no footer noise).
    """
    ranges = tracker.merged_gaps()
    if not ranges:
        return ""
    return (
        f"\nData gaps (>{tracker.min_gap_s:.1f}s): "
        + format_gaps(ranges, tracker.t0)
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_nan_gap_tracker.py -v`
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add nan_gap_tracker.py tests/test_nan_gap_tracker.py
git commit -m "feat: format NaN-gap ranges as a session-notes footer line"
```

---

### Task 3: `_LivePlotSink` records finite samples into the tracker

**Files:**
- Modify: `motion_connector.py` — `_LivePlotSink.__init__` (~line 170), `consume()` per-camera loop (~line 231), `_consume_side_avg()` (~line 304)
- Modify: `tests/test_live_plot_sink.py` (append tests)

Context for the engineer: `_LivePlotSink` is a pipeline sink defined near the
top of `motion_connector.py`. Its `consume("live", batch)` path iterates
frames × cameras and already skips samples whose BFI/BVI are non-finite
(`if not (math.isfinite(bfi) and math.isfinite(bvi)): continue` at ~line 231).
The tracker records **right after** that check and **before** the
camera-dropout gate (`_check_dropped_camera_emit`) — a dropout-suppressed
camera that is still sending finite data is *not* a NaN gap. The
`consume("live_side", sample)` path (reduced mode) currently appends BFI/BVI
unconditionally, NaN included, so there the tracker must gate on finiteness
itself.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_live_plot_sink.py`:

```python
def test_live_plot_sink_records_finite_samples_into_nan_gap_tracker():
    from nan_gap_tracker import NanGapTracker

    conn = _connector()
    tracker = NanGapTracker()
    src = _RecorderLiveSource()
    sink = _LivePlotSink(connector=conn, plot_t0=0.0, live_source=src,
                         nan_gap_tracker=tracker)
    batch = SimpleNamespace(
        bfi_live=np.zeros((3, 2, 8), dtype=np.float32),
        bvi_live=np.zeros((3, 2, 8), dtype=np.float32),
        mean_dc_rt=np.zeros((3, 2, 8), dtype=np.float32),
        contrast_sn_rt=np.zeros((3, 2, 8), dtype=np.float32),
        temperature_c=np.full((3, 2, 8), 35.0, dtype=np.float32),
        frame_type=np.array(["light", "light", "light"], dtype="<U8"),
        timestamp_s=np.array([0.1, 0.125, 3.0], dtype=np.float64),
        abs_frame_ids=np.array([10, 11, 12], dtype=np.int64),
        side_ids=np.array([0, 0, 0], dtype=np.int8),
        cam_ids=np.array([1, 1, 1], dtype=np.int8),
    )
    # Middle sample is NaN-filled → skipped by the sink AND absent from
    # the tracker, leaving a 0.125→3.0 gap for ("left", 1).
    batch.bfi_live[:] = 7.0
    batch.bvi_live[:] = 4.0
    batch.bfi_live[1, 0, 1] = np.nan

    sink.consume("live", batch)

    assert tracker.merged_gaps() == [
        (pytest.approx(0.125), pytest.approx(3.0))
    ]
    # Sink behavior unchanged: NaN sample not appended to the live source.
    assert [r["t"] for r in src.appended] == [
        pytest.approx(0.1), pytest.approx(3.0)
    ]


def test_live_plot_sink_records_dropout_suppressed_samples_in_tracker():
    """A camera in the dropped set is suppressed from the live source, but
    its finite samples still reach the tracker — record() sits before the
    dropout gate (arriving finite data is not a NaN gap)."""
    from nan_gap_tracker import NanGapTracker

    conn = _connector()
    conn._camera_dropped = {("left", 1)}
    tracker = NanGapTracker()
    src = _RecorderLiveSource()
    sink = _LivePlotSink(connector=conn, plot_t0=0.0, live_source=src,
                         nan_gap_tracker=tracker)
    batch = SimpleNamespace(
        bfi_live=np.full((1, 2, 8), 7.0, dtype=np.float32),
        bvi_live=np.full((1, 2, 8), 4.0, dtype=np.float32),
        mean_dc_rt=np.zeros((1, 2, 8), dtype=np.float32),
        contrast_sn_rt=np.zeros((1, 2, 8), dtype=np.float32),
        temperature_c=np.full((1, 2, 8), 35.0, dtype=np.float32),
        frame_type=np.array(["light"], dtype="<U8"),
        timestamp_s=np.array([0.1], dtype=np.float64),
        abs_frame_ids=np.array([10], dtype=np.int64),
        side_ids=np.array([0], dtype=np.int8),
        cam_ids=np.array([1], dtype=np.int8),
    )

    sink.consume("live", batch)

    assert src.appended == []          # suppressed from the live source
    assert tracker.t0 == pytest.approx(0.1)  # but recorded in the tracker


def test_live_plot_sink_side_average_records_only_finite_into_tracker():
    from nan_gap_tracker import NanGapTracker

    conn = _connector()
    tracker = NanGapTracker()
    src = _RecorderLiveSource()
    sink = _LivePlotSink(connector=conn, plot_t0=0.0, live_source=src,
                         nan_gap_tracker=tracker)

    sink.consume("live_side", SimpleNamespace(
        t=0.5, frame_id=100, side=0, bfi=0.42, bvi=5.0))
    sink.consume("live_side", SimpleNamespace(
        t=0.6, frame_id=101, side=0, bfi=float("nan"), bvi=5.0))
    sink.consume("live_side", SimpleNamespace(
        t=2.0, frame_id=102, side=0, bfi=0.40, bvi=4.8))

    # Side-average appends still store NaN (existing behavior)...
    assert len(src.appended) == 3
    # ...but the tracker only saw the finite samples → 0.5→2.0 gap.
    assert tracker.merged_gaps() == [
        (pytest.approx(0.5), pytest.approx(2.0))
    ]


def test_live_plot_sink_tracker_is_optional():
    """Default nan_gap_tracker=None keeps the sink working unchanged —
    existing callers and tests construct it without a tracker."""
    conn = _connector()
    sink, src = _make_sink(conn)
    sink.consume("live_side", SimpleNamespace(
        t=0.5, frame_id=100, side=0, bfi=0.42, bvi=5.0))
    assert len(src.appended) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_live_plot_sink.py -v`
Expected: the three tracker tests FAIL with `TypeError: __init__() got an unexpected keyword argument 'nan_gap_tracker'`; `test_live_plot_sink_tracker_is_optional` and all pre-existing tests PASS.

- [ ] **Step 3: Implement the sink changes**

In `motion_connector.py`, change `_LivePlotSink.__init__` (~line 170) from:

```python
    def __init__(self, connector: "MotionConnector", plot_t0: float,
                 live_source: "LiveScanSource"):
        self._connector = connector
        self._plot_t0 = plot_t0
        self._live_source = live_source
        self._temp_alerted: dict[tuple[str, int], bool] = {}
```

to:

```python
    def __init__(self, connector: "MotionConnector", plot_t0: float,
                 live_source: "LiveScanSource",
                 nan_gap_tracker: "NanGapTracker | None" = None):
        self._connector = connector
        self._plot_t0 = plot_t0
        self._live_source = live_source
        self._temp_alerted: dict[tuple[str, int], bool] = {}
        # Records every finite BFI/BVI sample so the scan-complete handler
        # can report sustained data gaps in the notes footer. Optional so
        # the sink works standalone (tests, future callers).
        self._nan_gap_tracker = nan_gap_tracker
```

In `consume()`, immediately after the existing finiteness check (~line 231):

```python
                if not (math.isfinite(bfi) and math.isfinite(bvi)):
                    continue

                # Finite sample — feed the NaN-gap tracker before the
                # dropout gate: a dropout-suppressed camera still sending
                # finite data is not a NaN gap.
                if self._nan_gap_tracker is not None:
                    self._nan_gap_tracker.record((side, cam_id), plot_ts)
```

In `_consume_side_avg()`, the current body ends with the
`append_uncorrected` call. Change the tail of the method from:

```python
        self._live_source.append_uncorrected(
            side=_SIDE_NAMES[side_idx],
            cam_id=-1,
            frame_id=int(getattr(sample, "frame_id", -1)),
            t=float(getattr(sample, "t", 0.0)),
            bfi=float(getattr(sample, "bfi", float("nan"))),
            bvi=float(getattr(sample, "bvi", float("nan"))),
        )
```

to:

```python
        bfi = float(getattr(sample, "bfi", float("nan")))
        bvi = float(getattr(sample, "bvi", float("nan")))
        t = float(getattr(sample, "t", 0.0))
        # The side-average path stores NaN as-is (the renderer skips it),
        # so the tracker must gate on finiteness here — only finite
        # samples count as "data present".
        if (self._nan_gap_tracker is not None
                and math.isfinite(bfi) and math.isfinite(bvi)):
            self._nan_gap_tracker.record((_SIDE_NAMES[side_idx], -1), t)
        self._live_source.append_uncorrected(
            side=_SIDE_NAMES[side_idx],
            cam_id=-1,
            frame_id=int(getattr(sample, "frame_id", -1)),
            t=t,
            bfi=bfi,
            bvi=bvi,
        )
```

Add the import near the top of `motion_connector.py`, beside the existing
local imports (e.g. right after `from motion_config import ...` — search for
`motion_config` to find the spot):

```python
from nan_gap_tracker import NanGapTracker, gap_note_line
```

(`gap_note_line` is used in Task 4; importing both now avoids touching the
import line twice.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_live_plot_sink.py tests/test_nan_gap_tracker.py -v`
Expected: all tests PASS (including all pre-existing `test_live_plot_sink.py` tests — the new parameter defaults to None).

- [ ] **Step 5: Commit**

```bash
git add motion_connector.py tests/test_live_plot_sink.py
git commit -m "feat: _LivePlotSink feeds finite samples to the NaN-gap tracker"
```

---

### Task 4: Connector wiring — fresh tracker per scan, gap line in the completion footer

**Files:**
- Modify: `motion_connector.py` — `MotionConnector.__init__` (~line 538, beside the dropout-watchdog state), `startCapture` (~line 1934 watchdog reset block and the `sinks=[...]` list at ~line 2032), `_on_pipeline_complete` closure (~line 1977)

This task has no new unit test: `_on_pipeline_complete` is a closure inside
`startCapture` and only runs with a live pipeline. All gap/format logic is
already covered by Tasks 1–2; this task is 3 small integration touches,
verified by the full unit suite plus a syntax/lint pass.

- [ ] **Step 1: Initialize the tracker attribute in `MotionConnector.__init__`**

Find the dropout-watchdog state block (~line 535):

```python
        # Camera dropout watchdog state — reset at start of each scan.
```

and add after the `self._camera_dropped_recovery_logged` line:

```python
        # NaN-gap tracker — replaced with a fresh instance at each scan
        # start (startCapture); _on_pipeline_complete reads it to append
        # the data-gaps footer to the session notes.
        self._nan_gap_tracker = NanGapTracker()
```

- [ ] **Step 2: Reset per scan and pass to the sink**

In `startCapture`, find the per-scan watchdog reset (~line 1934):

```python
        # Camera dropout watchdog state — fresh per scan.
        self._camera_last_seen = {}
        self._camera_last_temp = {}
        self._camera_dropped = set()
        self._camera_dropped_recovery_logged = set()
        self._dropout_timer.start()
```

and add after it:

```python
        # NaN-gap tracker — fresh per scan (same lifecycle as the watchdog).
        self._nan_gap_tracker = NanGapTracker()
```

Then in the `ScanRequest(...)` construction (~line 2032), change:

```python
            sinks=[
                _LivePlotSink(connector=self, plot_t0=plot_t0, live_source=live_source),
```

to:

```python
            sinks=[
                _LivePlotSink(connector=self, plot_t0=plot_t0, live_source=live_source,
                              nan_gap_tracker=self._nan_gap_tracker),
```

- [ ] **Step 3: Append the gap line in `_on_pipeline_complete`**

In the `_on_pipeline_complete` closure, find (~line 1977):

```python
            duration_line = f"\n---\nScan {status} — duration: {duration_str}"
            self._scan_notes = (self._scan_notes.strip() + duration_line)
            self.scanNotesChanged.emit()
```

and change to:

```python
            duration_line = f"\n---\nScan {status} — duration: {duration_str}"
            self._scan_notes = (self._scan_notes.strip() + duration_line)
            # Data-gap footer: aggregate ranges where any camera went >1 s
            # without a finite BFI/BVI sample (sustained NaN-fill/dropout).
            # Empty string when the scan was clean. Fail-soft like the rest
            # of this handler — a tracker bug must never block notes
            # persistence.
            try:
                gap_line = gap_note_line(self._nan_gap_tracker)
                if gap_line:
                    logger.warning("Scan data gaps detected: %s", gap_line.strip())
                    self._scan_notes += gap_line
            except Exception:
                logger.exception("NaN-gap footer computation failed")
            self.scanNotesChanged.emit()
```

(Both edits land **before** `self.scanNotesChanged.emit()` and the
`_persist_scan_notes(...)` call further down, so the footer is shown in the
Notes modal and saved to `sessions.session_notes` in one shot.)

- [ ] **Step 4: Verify — import check, full unit suite, lint**

```powershell
python -c "import motion_connector; print('import ok')"
python -m pytest tests -m unit -v
python -m flake8 nan_gap_tracker.py --max-line-length=100
```

Expected: `import ok`; all unit-marked tests PASS; flake8 silent.
(If `python -c "import motion_connector"` fails on PyQt app-singleton
requirements, rely on the pytest run instead — `tests/test_live_plot_sink.py`
already imports the module, so a syntax error there fails the suite.)

- [ ] **Step 5: Commit**

```bash
git add motion_connector.py
git commit -m "feat: append NaN data-gap ranges to the scan notes footer"
```

---

### Task 5: Manual smoke check (fake-data mode) — optional but recommended

**Files:** none (verification only)

- [ ] **Step 1: Run the app in mock mode and complete a short scan**

Set `"cameraFakeData": true` in `config/app_config.json`, then:

```powershell
python main.py
```

Run a short scan (the Test scan, `test_scan_duration_sec` = 5 s, is enough).
Fake data is continuous, so the expected outcome is a **clean** footer:
the Notes modal shows `Scan completed — duration: ...` with **no**
`Data gaps` line, and the app log shows no `NaN-gap footer computation
failed` error.

- [ ] **Step 2: Check the log**

```powershell
Get-ChildItem C:\Users\ethan\Projects\scan_data\app-logs\ow-bloodflowapp-*.log |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1 |
  Get-Content | Select-String -Pattern "NaN-gap|Data gaps|raised|exception"
```

Expected: no matches (clean scan) — or only the `Scan data gaps detected`
warning if the fake-data generator happened to stall.

- [ ] **Step 3: Revert the config flag**

Set `"cameraFakeData"` back to `false` in `config/app_config.json` if you
changed it (do not commit a flipped flag).
