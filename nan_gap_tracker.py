"""Sustained-gap detection over frame-arrival timestamps.

During a scan, _LivePlotSink (motion_connector.py) calls record() for every
frame that ARRIVES for a camera, keyed by (side, cam_id) — cam_id is -1 for
the clinical-mode per-side average. Frames flow at ~40 Hz, so during healthy
delivery each key's timestamp advances every ~25 ms. A stretch where a key
received no frame for longer than MIN_GAP_S therefore means delivery
genuinely stopped (camera dropout, USB stall, pipeline stall).

Arrival, not plottability: a covered / off-target camera streams unlit
frames whose BFI/BVI are NaN — that is expected operation, not a data gap,
so it must not appear in the notes footer. (Before 2026-07 the tracker was
fed only finite-BFI samples, which made covered sensors read as
catastrophic whole-scan gaps.)

At scan completion the connector unions the per-key gaps into aggregate
ranges (a range = "at least one camera received no frames here") and
appends them to the session notes via gap_note_line().

Pure Python, no Qt — unit-testable without hardware
(tests/test_nan_gap_tracker.py). Extracted per the motion_config.py
precedent rather than growing motion_connector.py further.

Spec: docs/superpowers/specs/2026-06-11-nan-gap-notes-footer-design.md
(gap semantics updated to arrival-based, 2026-07)
"""

from __future__ import annotations

import math
from typing import Hashable, Optional

# Minimum silence (seconds) between consecutive recorded samples of one key
# for the stretch to count as a gap. ~40 missed frames at 40 Hz.
MIN_GAP_S = 1.0


class NanGapTracker:
    """Tracks per-key recorded timestamps and the gaps between them.

    The tracker is agnostic about what a recorded sample *means* — the
    caller decides what counts as "data present" (since 2026-07: frame
    arrival). Timestamps are the pipeline's plot timestamps (sensor
    firmware clock, seconds). Leading gaps — before a key's first recorded
    sample — are never recorded: that is warmup, expected. Trailing gaps —
    a key falling silent before the scan's last data — are closed at
    merged_gaps() time against the global max timestamp seen (or an
    explicit end_t).
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
        if last is None or t > last:
            self._last[key] = t

    def merged_gaps(self, end_t: Optional[float] = None) -> list[tuple[float, float]]:
        """Union of all per-key gap intervals, sorted and merged.

        end_t closes trailing gaps: any key whose last finite sample is more
        than min_gap_s before end_t contributes (last, end_t). Defaults to
        the global max timestamp seen, so a camera that died mid-scan shows
        a gap running to the end of the data. An end_t earlier than recorded
        gaps does not truncate them.
        """
        if end_t is None:
            end_t = self._t_max
        gaps = list(self._gaps)
        if end_t is not None:
            # Defensive copy. The class is not thread-safe; in the app,
            # record() and merged_gaps() both run on the pipeline runner
            # thread (on_complete fires after all consumes).
            for last in list(self._last.values()):
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
