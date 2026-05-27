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
from PyQt6.QtCore import QObject, QTimer, pyqtSignal


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

    def _grow(self) -> None:
        """Double the capacity of all three parallel arrays atomically.
        All three must always stay the same length — this method is the
        single chokepoint that enforces that invariant."""
        new_cap = self.t.shape[0] * 2
        self.t = np.resize(self.t, new_cap)
        self.v = np.resize(self.v, new_cap)
        self.frame_id = np.resize(self.frame_id, new_cap)

    def append(self, t: float, v: float, frame_id: int) -> None:
        """Append one sample. Grows capacity (doubling) on overflow."""
        if self.n >= self.t.shape[0]:
            self._grow()
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
        no such frame_id was appended (race: final arrived before live).

        Value is stored as float32; sub-float32 precision is lost on write."""
        if frame_id == -1:
            return
        idx = self._frame_id_to_index.get(int(frame_id))
        if idx is None:
            return
        self.v[idx] = value

    def window_indices(self, t_lo: float, t_hi: float) -> tuple[int, int]:
        """Return (i_lo, i_hi) such that t[i_lo:i_hi] covers [t_lo, t_hi].
        i_hi is right-exclusive. Binary search; O(log n).

        Precondition: t[0:n] is monotonically non-decreasing. The live pipeline
        appends in timestamp order at 40 Hz; PastScanSource reads rows ordered
        by timestamp_s ASC from the SDK query. Out-of-order append silently
        returns wrong indices — np.searchsorted does not validate."""
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


_FLUSH_INTERVAL_MS = 100  # spec §"Throttled UI notify"


class ScanDataSource(QObject):
    """Base for live and past scan sources. Owns per-(side, cam, metric)
    _CameraBuffer instances and a throttled samplesAppended signal.

    Subclasses populate buffers; the base handles bookkeeping (liveEdge
    cache, flush timer, signal coalescing).
    """

    # (side, cam_id, metric, added_count) — coalesced across the flush window.
    samplesAppended = pyqtSignal(str, int, str, int)

    def __init__(self, plot_t0: float, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.plot_t0 = float(plot_t0)
        self.live: bool = False  # LiveScanSource overrides to True
        self.buffers: dict[tuple[str, int, str], _CameraBuffer] = {}

        # Pending dirty bookkeeping: (side, cam, metric) -> accumulated count.
        self._pending: dict[tuple[str, int, str], int] = {}

        # Throttle timer — fires every 100 ms while the source is alive.
        # Tests can call _flush() directly and ignore the timer entirely.
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(_FLUSH_INTERVAL_MS)
        self._flush_timer.timeout.connect(self._flush)
        self._flush_timer.start()

    # ── public ────────────────────────────────────────────────────────────

    @property
    def liveEdge(self) -> float:
        """The maximum timestamp across all buffers, or 0.0 if empty."""
        edge = 0.0
        for b in self.buffers.values():
            if b.n > 0:
                last_t = float(b.t[b.n - 1])
                if last_t > edge:
                    edge = last_t
        return edge

    def get_or_create_buffer(self, side: str, cam_id: int, metric: str) -> _CameraBuffer:
        key = (side, int(cam_id), metric)
        buf = self.buffers.get(key)
        if buf is None:
            buf = _CameraBuffer()
            self.buffers[key] = buf
        return buf

    def note_dirty(self, side: str, cam_id: int, metric: str, added: int) -> None:
        """Record that `added` new samples landed in (side, cam, metric).
        Coalesces with any previous note since the last flush."""
        if added <= 0:
            return
        key = (side, int(cam_id), metric)
        self._pending[key] = self._pending.get(key, 0) + added

    # ── internal ──────────────────────────────────────────────────────────

    def _flush(self) -> None:
        """Emit one samplesAppended per dirty buffer and clear pending."""
        if not self._pending:
            return
        pending = self._pending
        self._pending = {}
        for (side, cam_id, metric), count in pending.items():
            self.samplesAppended.emit(side, cam_id, metric, count)


class LiveScanSource(ScanDataSource):
    """ScanDataSource fed by the in-flight pipeline. Constructed at scan
    start; lives as long as the connector holds a reference."""

    def __init__(self, plot_t0: float, parent: Optional[QObject] = None) -> None:
        super().__init__(plot_t0=plot_t0, parent=parent)
        self.live = True

    def append_uncorrected(
        self,
        side: str,
        cam_id: int,
        frame_id: int,
        t: float,
        bfi: float,
        bvi: float,
        mean: Optional[float] = None,
        contrast: Optional[float] = None,
    ) -> None:
        """Append one frame's worth of uncorrected metrics.

        bfi and bvi are always appended (NaN included — the source stores
        what arrives). mean/contrast are appended only when non-None;
        the existing _LivePlotSink passes None for samples where the
        SDK reported a non-finite mean_dc_rt / contrast_sn_rt."""
        self._append_one(side, cam_id, "bfi", frame_id, t, bfi)
        self._append_one(side, cam_id, "bvi", frame_id, t, bvi)
        if mean is not None:
            self._append_one(side, cam_id, "mean", frame_id, t, mean)
        if contrast is not None:
            self._append_one(side, cam_id, "contrast", frame_id, t, contrast)

    def apply_corrected_batch(self, batch: list) -> None:
        """Overwrite in place at matching frame_ids across all 4 metrics.

        Payload shape matches scanCorrectedBatch.emit: list[dict] with
        keys side, camId, frameId, bfi, bvi, mean, contrast (ts is
        ignored — t is set at live-append time, not at correction time)."""
        for sample in batch:
            side = str(sample["side"])
            cam_id = int(sample["camId"])
            frame_id = int(sample["frameId"])
            for metric_key, payload_key in (
                ("bfi", "bfi"),
                ("bvi", "bvi"),
                ("mean", "mean"),
                ("contrast", "contrast"),
            ):
                buf = self.buffers.get((side, cam_id, metric_key))
                if buf is None:
                    continue
                buf.apply_corrected(frame_id=frame_id, value=float(sample[payload_key]))

    def mark_dropped(self, side: str, cam_id: int, t: float) -> None:
        """Record a dropout timestamp on every existing metric buffer for
        this (side, cam). Idempotent per _CameraBuffer.mark_dropped."""
        for metric in ("bfi", "bvi", "mean", "contrast"):
            buf = self.buffers.get((side, int(cam_id), metric))
            if buf is not None:
                buf.mark_dropped(t)

    # ── internal ──────────────────────────────────────────────────────────

    def _append_one(
        self,
        side: str,
        cam_id: int,
        metric: str,
        frame_id: int,
        t: float,
        v: float,
    ) -> None:
        buf = self.get_or_create_buffer(side, cam_id, metric)
        buf.append(t=t, v=v, frame_id=frame_id)
        self.note_dirty(side, cam_id, metric, added=1)
