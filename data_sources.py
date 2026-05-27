"""Per-scan data sources for the bloodflow real-time plot viewer.

See docs/superpowers/specs/2026-05-22-realtime-plot-viewer-design.md for
the full design. Phase 1 introduces:

  - _CameraBuffer   : per-(side, cam, metric) growable numpy buffer.
  - ScanDataSource  : QObject base. Owns per-key buffers + the throttled
                      samplesAppended signal.
  - LiveScanSource  : appends from the in-flight pipeline; supports
                      corrected-batch in-place overwrites.
  - PastScanSource  : constructed from an SDK ScanDatabase session_id;
                      bucketizes session_data rows into the same layout.

Nothing in QML consumes these yet — Phase 1 is purely additive.
"""

from __future__ import annotations

from typing import Optional

import numpy as np


_INITIAL_CAPACITY = 4096  # ≈ 100 s @ 40 Hz; doubles on overflow.


class _CameraBuffer:
    """Append-only growable buffer for one (side, cam_id, metric) stream.

    Holds three parallel arrays (t, v, frame_id) plus a frame_id → index
    lookup so corrected-batch overwrites can rewrite in place. NaN values
    are stored; the renderer is responsible for skipping them on draw.
    """

    __slots__ = ("t", "v", "frame_id", "n", "_frame_id_to_index", "dropped_at")

    def __init__(self, initial_capacity: int = _INITIAL_CAPACITY) -> None:
        self.t = np.empty(initial_capacity, dtype=np.float64)
        self.v = np.empty(initial_capacity, dtype=np.float32)
        self.frame_id = np.empty(initial_capacity, dtype=np.int64)
        self.n = 0
        self._frame_id_to_index: dict[int, int] = {}
        self.dropped_at: Optional[float] = None

    def append(self, t: float, v: float, frame_id: int) -> None:
        """Append one sample. Grows capacity (doubling) on overflow."""
        if self.n >= self.t.shape[0]:
            new_cap = self.t.shape[0] * 2
            self.t = np.resize(self.t, new_cap)
            self.v = np.resize(self.v, new_cap)
            self.frame_id = np.resize(self.frame_id, new_cap)
        idx = self.n
        self.t[idx] = t
        self.v[idx] = v
        self.frame_id[idx] = frame_id
        # frame_id == -1 is the SDK's "unknown" sentinel; don't index it —
        # apply_corrected with frame_id=-1 must NOT silently rewrite the
        # most-recent unknown sample.
        if frame_id != -1:
            self._frame_id_to_index[int(frame_id)] = idx
        self.n += 1

    def apply_corrected(self, frame_id: int, value: float) -> None:
        """Overwrite v at the row whose frame_id matches. Silent no-op if
        no such frame_id was appended (race: final arrived before live)."""
        if frame_id == -1:
            return
        idx = self._frame_id_to_index.get(int(frame_id))
        if idx is None:
            return
        self.v[idx] = value

    def window_indices(self, t_lo: float, t_hi: float) -> tuple[int, int]:
        """Return (i_lo, i_hi) such that t[i_lo:i_hi] covers [t_lo, t_hi].
        i_hi is right-exclusive. Binary search; O(log n)."""
        if self.n == 0:
            return 0, 0
        t_slice = self.t[: self.n]
        i_lo = int(np.searchsorted(t_slice, t_lo, side="left"))
        i_hi = int(np.searchsorted(t_slice, t_hi, side="right"))
        return i_lo, i_hi

    def mark_dropped(self, t: float) -> None:
        """Record the first dropout timestamp for this stream. Idempotent —
        later calls don't overwrite the earlier dropout."""
        if self.dropped_at is None:
            self.dropped_at = float(t)
