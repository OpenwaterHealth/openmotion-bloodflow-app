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
