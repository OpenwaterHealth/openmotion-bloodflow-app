# NaN-gap note in the Session Notes footer

**Date:** 2026-06-11
**Status:** Approved (design)

## Problem

Over the course of a scan, stretches of data can be NaN-filled by the data
pipeline (camera dropout, stalls, etc.). The operator currently has no record
of *when* that happened. The request: at the bottom of the Session Notes modal,
note the timestamp ranges where data may have been lost to NaN-filling.

## Goal

When a scan completes, if any camera produced a *sustained* gap in finite
BFI/BVI data (longer than the normal frame cadence), append a single footer
line to the scan notes listing the aggregate time ranges (union across all
cameras). When there are no gaps, append nothing.

Example footer (added after the existing duration line):

```
---
Scan completed — duration: 00:05:12
Data gaps (>1.0s): 12.4–15.8s, 47.0–49.2s
```

## Key constraint: NaN is routine, not always an "issue"

The live sink at `motion_connector.py:231` skips non-finite BFI/BVI on
essentially every dark frame, plus early warmup/light frames before the first
dark observation. These are expected ~1 Hz NaNs, **not** lost data. Light
frames flow continuously at ~40 Hz, so the timestamp of the last *finite*
sample advances roughly every 25 ms during healthy capture.

Therefore the meaningful signal is a **sustained gap**: a stretch where a
camera produced no finite sample for noticeably longer than the normal cadence.
A single dark frame never creates a gap (the next light frame is ~25 ms later).
Only a genuine loss of finite data for >1 s registers.

## Decisions (from brainstorming)

- **Detection source:** timestamp ranges where finite data was lost in the data
  pipeline.
- **Granularity:** aggregate across any camera — the *union* of per-camera
  sustained gaps, merged into one list of ranges.
- **Gap threshold:** ~1 second (≈40 missed frames). Hardcoded module constant,
  not config-driven.
- **Approach:** incremental tracking in the live sink (Approach A below).

## Approach A — incremental tracking in the live sink (chosen)

A small pure-Python tracker records the timestamp of every *finite* BFI/BVI
sample per `(side, cam_id)` key as it flows through `_LivePlotSink`. When a
finite sample arrives more than the threshold after that key's previous finite
sample, it records a gap `(prev_t, cur_t)` for that key. At scan completion the
per-key gaps are unioned into aggregate ranges and appended to the notes
footer.

Properties:
- O(1) memory per key, no completion-time read-back.
- Mode-independent: full mode feeds per-camera keys (`cam_id` 0–7); reduced mode
  feeds the per-side-average keys (`cam_id = -1`). Either way gaps are detected.
- Sits exactly at the pipeline's NaN-skip point.

Trade-off: reflects the *realtime* stream the operator was capturing, not the
corrected DB record. Acceptable — the note is about data integrity during
capture.

Approaches **B** (completion-time SQLite `LAG()` window query against
`session_data`) and **C** (both) were considered and rejected: B adds an SDK
method and re-reads potentially huge tables for long scans; C is overkill.

## Components

### `nan_gap_tracker.py` (new, pure Python, no Qt)

Standalone and unit-testable without hardware. Keeps `motion_connector.py`
(already 4031 lines) from growing further.

```
MIN_GAP_S = 1.0   # module constant

class NanGapTracker:
    def __init__(self, min_gap_s: float = MIN_GAP_S): ...
    def record(self, key: tuple, t: float) -> None:
        # First finite sample for a key sets t0/last; subsequent samples
        # whose (t - last) > min_gap_s append (last, t) to that key's gaps.
        # Tracks the global max timestamp seen (scan data end).
    def merged_gaps(self, end_t: float | None = None) -> list[tuple[float, float]]:
        # Optionally close trailing gaps: for each key whose last finite t is
        # > min_gap_s before end_t (default = global max seen), add (last, end_t).
        # Union all per-key gap intervals into sorted, merged ranges.
    @property
    def t0(self) -> float | None:
        # First finite timestamp seen across all keys; used to make ranges
        # scan-relative.

def format_gaps(ranges, t0) -> str:
    # "12.4–15.8s, 47.0–49.2s"  (each endpoint = value - t0, 1 decimal)
```

Notes:
- Leading gaps (before a key's first finite sample) are never recorded — that is
  warmup, expected.
- Trailing gaps (a camera stops before scan end) are closed at completion using
  the global max timestamp seen as the end reference.
- "Finite" means both BFI and BVI finite, matching the live sink's existing gate
  at `motion_connector.py:231` and the per-side-average path.

### `_LivePlotSink` (`motion_connector.py`)

- Holds a reference to the connector's tracker.
- Per-camera path: after the existing `math.isfinite(bfi) and math.isfinite(bvi)`
  check, call `tracker.record((side, cam_id), plot_ts)`.
- Per-side-average path (`_consume_side_avg`): record `((side, -1), t)` only when
  the sample's bfi and bvi are finite.

### Connector wiring (`motion_connector.py`)

- Instantiate/reset `self._nan_gap_tracker = NanGapTracker()` at scan start,
  alongside the camera-dropout watchdog reset (~line 1934).
- In `_on_pipeline_complete`, after the existing duration line is appended:
  ```
  tracker = self._nan_gap_tracker
  ranges = tracker.merged_gaps()
  if ranges:
      line = "\nData gaps (>1.0s): " + format_gaps(ranges, tracker.t0)
      self._scan_notes = self._scan_notes + line
  ```
  This runs before `scanNotesChanged.emit()` and `_persist_scan_notes(...)`, so
  the footer is both shown in the modal and saved to the DB session.

## Data flow

```
SDK pipeline ──> _LivePlotSink.consume()
                   │ (finite BFI/BVI only)
                   └─> NanGapTracker.record((side, cam_id), plot_ts)
                                            │
scan completes ──> _on_pipeline_complete()  │
                   ├─ append duration line  │
                   ├─ ranges = tracker.merged_gaps()  ◄┘
                   ├─ if ranges: append "Data gaps ..." line
                   ├─ scanNotesChanged.emit()  ──> NotesModal renders MotionInterface.scanNotes
                   └─ _persist_scan_notes()    ──> sessions.session_notes
```

`NotesModal.qml` needs **no changes** — it already renders `MotionInterface.scanNotes`.

## Error handling

- `record()` ignores non-finite `t` defensively (shouldn't happen — caller gates
  on finiteness).
- Gap computation in `_on_pipeline_complete` is wrapped so a tracker error never
  aborts notes persistence (the completion handler is fail-soft by design).
- Empty tracker (no samples, e.g. immediate cancel) → `merged_gaps()` returns
  `[]` → no footer line.

## Testing

Pure unit tests for `NanGapTracker` (no hardware):
- Healthy 40 Hz stream → no gaps.
- Single dark-frame NaN (one ~25 ms skip) → no gap.
- One camera silent for 3 s mid-scan → one range covering it.
- Two cameras with overlapping gaps → merged into a single range.
- Two cameras with disjoint gaps → two separate ranges.
- Trailing gap: a camera stops 2 s before the global max → range closed at end.
- Leading warmup (no samples until t=2 s) → no leading gap.
- `format_gaps` produces scan-relative `a–b s` strings with 1-decimal precision.

## Known limitations

- Ranges are scan-relative seconds (first finite sample = 0 s), matching the
  plot. A mid-scan sensor reboot resets the firmware clock
  (`motion_connector.py:1899`), which can skew absolute offsets — the same
  caveat the live plot already carries.
- Gap granularity is per-camera in BOTH modes: the SDK's `Tee("live", ...)` is
  unconditional, so the per-camera channel feeds the tracker even in reduced
  mode (alongside the `cam_id=-1` side-average keys). A single flaky camera
  therefore produces a `Data gaps` line in clinical (reduced) mode even though
  the displayed side average never blinked — consistent with the "any camera"
  union decision. (Corrected during implementation review; an earlier draft of
  this section wrongly claimed reduced mode only registered whole-side loss.)

## Out of scope

- No new config knob (threshold is a hardcoded constant per the decision).
- No change to `NotesModal.qml`.
- No SDK changes.
- No backfill of the note onto past scans.
