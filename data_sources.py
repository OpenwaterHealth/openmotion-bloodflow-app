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
from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot, pyqtProperty


_MAX_CAPACITY = 72000       # ≈ 30 min @ 40 Hz; ring-trim above this.
_INITIAL_CAPACITY = _MAX_CAPACITY
# Pre-allocate at the cap. Mid-scan grow events (np.resize → alloc +
# copy) at the 7-min and 14-min doublings stressed Python's allocator
# hard enough to stall the SDK parser thread — the data_queue would
# overflow, USB chunks got dropped, and the parser never recovered
# (eventually surfaced as CRC mismatches on misaligned reads). At
# ~1.44 MB per buffer × ~32 active buffers ≈ 46 MB the static
# allocation cost is fine and consistent across the scan.


class _CameraBuffer:
    """Append-only growable buffer for one (side, cam_id, metric) stream.

    Holds three parallel arrays (t, v, frame_id). NaN values are stored;
    the renderer is responsible for skipping them on draw.

    Optional `track_frame_ids=True` enables a frame_id → array-index
    lookup for in-place corrected-batch overwrites. Off by default
    (the new viewer no longer applies corrections, and tracking the
    dict added meaningful memory + GC pressure over long scans).

    Above _MAX_CAPACITY samples, the oldest half is dropped on the
    next overflow (drop-oldest ring behavior). Bounds memory for
    multi-hour scans at the cost of pan-back history beyond ~30 min.
    """

    __slots__ = ("t", "v", "frame_id", "n", "_frame_id_to_index", "dropped_at")

    def __init__(
        self,
        initial_capacity: int = _INITIAL_CAPACITY,
        track_frame_ids: bool = False,
    ) -> None:
        self.t = np.empty(initial_capacity, dtype=np.float64)
        self.v = np.empty(initial_capacity, dtype=np.float32)
        self.frame_id = np.empty(initial_capacity, dtype=np.int64)
        self.n = 0
        self._frame_id_to_index: Optional[dict[int, int]] = (
            {} if track_frame_ids else None
        )
        self.dropped_at: Optional[float] = None

    def _grow(self) -> None:
        """Double the capacity of all three parallel arrays atomically.
        All three must always stay the same length — this method is the
        single chokepoint that enforces that invariant."""
        new_cap = self.t.shape[0] * 2
        self.t = np.resize(self.t, new_cap)
        self.v = np.resize(self.v, new_cap)
        self.frame_id = np.resize(self.frame_id, new_cap)

    def _ring_trim(self) -> None:
        """At-cap: drop the oldest half of the buffer in-place so new
        appends keep landing at index n without unbounded growth.
        Called when capacity has already reached _MAX_CAPACITY."""
        half = _MAX_CAPACITY // 2
        self.t[:half] = self.t[half:]
        self.v[:half] = self.v[half:]
        self.frame_id[:half] = self.frame_id[half:]
        self.n = half
        # Frame-id lookup positions shift after the trim; safest to
        # drop the mapping and rebuild lazily on subsequent appends.
        if self._frame_id_to_index is not None:
            self._frame_id_to_index.clear()

    def append(self, t: float, v: float, frame_id: int) -> None:
        """Append one sample. Grows capacity (doubling) on overflow up
        to _MAX_CAPACITY; above that, ring-trims the oldest half."""
        if self.n >= self.t.shape[0]:
            if self.t.shape[0] >= _MAX_CAPACITY:
                self._ring_trim()
            else:
                self._grow()
        idx = self.n
        self.t[idx] = t
        self.v[idx] = v
        self.frame_id[idx] = frame_id
        # frame_id == -1 is the SDK's "unknown" sentinel; don't index it —
        # apply_corrected with frame_id=-1 must NOT silently rewrite the
        # most-recent unknown sample.
        if self._frame_id_to_index is not None and frame_id != -1:
            self._frame_id_to_index[int(frame_id)] = idx
        self.n += 1

    def apply_corrected(self, frame_id: int, value: float) -> None:
        """Overwrite v at the row whose frame_id matches. Silent no-op
        when frame-id tracking is disabled (the default), when
        frame_id is the -1 sentinel, or when no matching frame_id was
        appended (race: final arrived before live, or the row was
        dropped by a ring-trim).

        Value is stored as float32; sub-float32 precision is lost on write."""
        if self._frame_id_to_index is None or frame_id == -1:
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

    def window_decimated(
        self,
        t_lo: float,
        t_hi: float,
        max_points: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return (t, v) arrays covering [t_lo, t_hi] at no more than
        max_points samples. When decimation is required, samples are
        mean-binned (each output value = mean of `stride` consecutive
        input samples) rather than stride-subsampled.

        Mean-binning matters: simple stride subsampling at e.g.
        stride=2 causes the rendered trace to alternate between odd-
        and even-indexed samples as the window scrolls one sample at a
        time, producing a visible "jumping between two datasets"
        aliasing. Mean-binning averages each pair/triple so the
        per-bin value changes only fractionally as the window slides.

        NaN samples within a bin are skipped via np.nanmean; a bin
        whose samples are all non-finite returns NaN (the renderer's
        existing isFinite guard skips it).

        Empty arrays returned when the window contains no samples or
        the buffer is empty."""
        i_lo, i_hi = self.window_indices(t_lo, t_hi)
        n_window = i_hi - i_lo
        if n_window <= 0:
            return self.t[0:0], self.v[0:0]

        # Stride-aligned causal smoothing — always applied, even when
        # n_window <= max_points. Previously we returned raw samples in
        # that case and only switched to smoothing once the window
        # filled, which produced a visible "downsampled" appearance
        # flip + a perf ramp during the first ~5 s of the scan. With a
        # minimum stride of 2 the kernel is always wide enough to
        # suppress aliasing without much loss of detail.
        #
        # Each output sits at an ABSOLUTE sample index that is a multiple
        # of `stride` — so the same absolute sample always maps to the
        # same output regardless of which paint we're on. And the
        # smoothing is CAUSAL (window covers [N-w+1, N], only samples
        # at or before N) — so once a sample is appended, the output at
        # that absolute index has its final value and never changes.
        #
        # Stride is derived from the TIME WINDOW (× nominal 40 Hz), not
        # from n_window. Keying on n_window meant stride crept up as
        # samples accumulated within a fixed zoom (e.g. 24 → 25 when
        # n_window crossed 4800 at the same zoom level), and every
        # stride change relocated every output to a new absolute index
        # with a new causal window — producing a visible "morph" of
        # the trace shape each time it ticked. Time-keyed stride is
        # invariant under data growth, so the trace stays put.
        nominal_hz = 40.0  # SDK histogram cadence
        expected_samples = max(1.0, (t_hi - t_lo) * nominal_hz)
        stride = max(2, int(-(-expected_samples // max_points)))
        window = stride * 3

        # Stride-aligned absolute indices that fall within [i_lo, i_hi).
        first_k = (i_lo + stride - 1) // stride
        last_k = (i_hi - 1) // stride
        if last_k < first_k:
            return self.t[0:0], self.v[0:0]
        abs_idxs = np.arange(first_k, last_k + 1) * stride

        # Cumsum range covers the causal lookback for the leftmost output
        # all the way through the rightmost output (inclusive).
        cum_lo = max(0, int(abs_idxs[0]) - window + 1)
        cum_hi = int(abs_idxs[-1]) + 1
        v_block = self.v[cum_lo:cum_hi]
        f_block = np.isfinite(v_block)
        v_clean = np.where(f_block, v_block, 0.0).astype(np.float64)
        v_cum = np.empty(v_block.size + 1, dtype=np.float64)
        v_cum[0] = 0.0
        np.cumsum(v_clean, out=v_cum[1:])
        n_cum = np.empty(v_block.size + 1, dtype=np.int64)
        n_cum[0] = 0
        np.cumsum(f_block, out=n_cum[1:], dtype=np.int64)

        # Per-output causal window = [abs_idx - window + 1, abs_idx],
        # translated into v_block-local indices.
        local = abs_idxs - cum_lo
        lo_b = np.maximum(0, local - window + 1)
        hi_b = local + 1
        counts = n_cum[hi_b] - n_cum[lo_b]
        sums = v_cum[hi_b] - v_cum[lo_b]
        with np.errstate(invalid="ignore", divide="ignore"):
            v_out = np.where(counts > 0, sums / np.maximum(counts, 1), np.nan)
        t_out = self.t[abs_idxs]
        return t_out, v_out.astype(np.float32)

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

    def __init__(self, plot_t0: float, parent: Optional[QObject] = None,
                 track_frame_ids: bool = False) -> None:
        super().__init__(parent)
        self.plot_t0 = float(plot_t0)
        # Subclasses set _live in __init__ — backing store for the
        # `live` pyqtProperty below. Must be a pyqtProperty (not a
        # plain attribute) for QML to read it; plain Python attributes
        # are invisible to QML and read as `undefined`.
        self._live: bool = False
        self.buffers: dict[tuple[str, int, str], _CameraBuffer] = {}
        # Default False — production no longer uses corrected-batch
        # overwrites; saves a per-buffer frame_id dict that scales with
        # scan duration. Tests opt in via the constructor flag.
        self._track_frame_ids = track_frame_ids

        # Pending dirty bookkeeping: (side, cam, metric) -> accumulated count.
        self._pending: dict[tuple[str, int, str], int] = {}

        # Throttle timer — fires every 100 ms while the source is alive.
        # Tests can call _flush() directly and ignore the timer entirely.
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(_FLUSH_INTERVAL_MS)
        self._flush_timer.timeout.connect(self._flush)
        self._flush_timer.start()

    # ── public ────────────────────────────────────────────────────────────

    @pyqtProperty(bool, constant=True)
    def live(self) -> bool:
        """True for LiveScanSource, False for PastScanSource. Must be a
        pyqtProperty (not a plain attribute) for QML to read it — plain
        Python attributes resolve to ``undefined`` in QML, which silently
        broke the toolbar's source-type label and the "Back to live"
        button's variant selection (both branched on `scanSource.live`)."""
        return self._live

    @pyqtProperty(float)
    def liveEdge(self) -> float:
        """The maximum timestamp across all buffers, or 0.0 if empty.

        Exposed as ``pyqtProperty`` (not plain ``@property``) so QML can
        read it from JavaScript inside ``PlotCell.onPaint``. Plain Python
        properties don't appear in PyQt6's meta-object and resolve to
        ``undefined`` in QML, which would NaN-poison the paint math.

        No notify signal: PlotCell repaints on ``samplesAppended`` (which
        carries the dirty-buffer signal) and reads liveEdge fresh inside
        onPaint.
        """
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
            buf = _CameraBuffer(track_frame_ids=self._track_frame_ids)
            self.buffers[key] = buf
        return buf

    @pyqtSlot(str, int, str, float, float, int, result="QVariantList")
    def points_for_window(
        self,
        side: str,
        cam_id: int,
        metric: str,
        t_lo: float,
        t_hi: float,
        max_points: int,
    ) -> list:
        """QML-facing: return a list of [t, v] pairs for the requested
        (side, cam_id, metric) buffer in the window [t_lo, t_hi], strided
        to at most max_points. Returns [] when the buffer doesn't exist
        or the window is empty.

        Each pair is [float, float]. The list is a fresh allocation per
        call — Phase 2b can optimize with typed-array passthrough if paint
        cost becomes a bottleneck."""
        buf = self.buffers.get((side, int(cam_id), metric))
        if buf is None:
            return []
        t_arr, v_arr = buf.window_decimated(t_lo, t_hi, int(max_points))
        return [[float(t), float(v)] for t, v in zip(t_arr, v_arr)]

    @pyqtSlot(str, int, str, float, result=float)
    def value_at(self, side: str, cam_id: int, metric: str, t: float) -> float:
        """Return the buffer's value for the sample nearest to time `t`.
        NaN if the buffer doesn't exist, is empty, or the nearest sample
        is itself non-finite. Used by the viewer's hover tooltip.

        Concurrency: snapshots buf.n locally because the SDK pipeline
        thread may call _CameraBuffer.append (incrementing n) between
        our reads. Without the snapshot, a torn read of n caused
        IndexErrors when the searchsorted-returned idx happened to
        equal the captured slice size but failed the (stale-n) clamp."""
        buf = self.buffers.get((side, int(cam_id), metric))
        if buf is None:
            return float("nan")
        n = buf.n
        if n == 0:
            return float("nan")
        t_slice = buf.t[:n]
        idx = int(np.searchsorted(t_slice, t, side="left"))
        if idx >= n:
            idx = n - 1
        elif idx > 0 and abs(t_slice[idx - 1] - t) < abs(t_slice[idx] - t):
            idx -= 1
        v = float(buf.v[idx])
        return v if np.isfinite(v) else float("nan")

    @pyqtSlot(str, int, result=float)
    def dropped_at_for(self, side: str, cam_id: int) -> float:
        """Return the dropout timestamp for (side, cam_id), or NaN if the
        camera hasn't been flagged as dropped this scan. PlotCell uses
        this to draw a vertical bar at the dropout time. The bar is
        per-(side, cam) so any one of its metric buffers' dropped_at
        suffices."""
        for metric in ("bfi", "bvi", "mean", "contrast"):
            buf = self.buffers.get((side, int(cam_id), metric))
            if buf is not None and buf.dropped_at is not None:
                return float(buf.dropped_at)
        return float("nan")

    @pyqtSlot(str, result="QVariantMap")
    @pyqtSlot(str, float, float, float, result="QVariantMap")
    def compute_bounds_for_metric(
        self,
        metric: str,
        percentile_lo: float = 2.0,
        percentile_hi: float = 98.0,
        pad_frac: float = 0.25,
    ) -> dict:
        """Aggregate finite values across every (side, cam_id) buffer for
        `metric`, return padded percentile bounds as {"yMin": ..., "yMax": ...}.

        Neutral fallback {"yMin": 0.0, "yMax": 1.0} when fewer than 4 valid
        samples are available (too noisy to autoscale meaningfully).

        Matches legacy EmbeddedRealtimePlot._boundsFromArray semantics."""
        # Stay in numpy throughout — building a Python list from
        # `.tolist()` was the dominant cost of this slot at 1 Hz over
        # 8 cams × 2 metrics × growing buffers.
        chunks: list[np.ndarray] = []
        for (_side, _cam_id, m), buf in self.buffers.items():
            if m != metric or buf.n == 0:
                continue
            slice_v = buf.v[: buf.n]
            finite_mask = np.isfinite(slice_v)
            if finite_mask.any():
                chunks.append(slice_v[finite_mask])

        if not chunks:
            return {"yMin": 0.0, "yMax": 1.0}
        combined = np.concatenate(chunks)
        if combined.size < 4:
            return {"yMin": 0.0, "yMax": 1.0}

        lo = float(np.percentile(combined, percentile_lo))
        hi = float(np.percentile(combined, percentile_hi))

        if lo == hi:
            lo -= 0.5
            hi += 0.5

        pad = (hi - lo) * pad_frac
        return {"yMin": lo - pad, "yMax": hi + pad}

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

    def __init__(self, plot_t0: float, parent: Optional[QObject] = None,
                 track_frame_ids: bool = False) -> None:
        super().__init__(plot_t0=plot_t0, parent=parent,
                         track_frame_ids=track_frame_ids)
        self._live = True

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


# session_data.side is stored as INTEGER (0 = left, 1 = right) but the
# rest of the app uses string side names. Normalize on the way in.
_SIDE_INT_TO_STR = {0: "left", 1: "right"}


class PastScanSource(ScanDataSource):
    """ScanDataSource constructed from an SDK ScanDatabase session_id.
    Bucketizes session_data rows into per-(side, cam, metric) buffers
    using the same layout as LiveScanSource."""

    def __init__(
        self,
        scan_db,                # omotion.ScanDatabase — duck-typed for tests
        session_id: int,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(plot_t0=0.0, parent=parent)
        self._live = False
        self.session_id = int(session_id)

        for row in scan_db.iter_session_data(self.session_id):
            side_int = int(row["side"])
            side = _SIDE_INT_TO_STR.get(side_int)
            if side is None:
                # Unknown side encoding — silently skip rather than crash on
                # legacy / corrupted data; the load is best-effort.
                continue
            cam_id = int(row["cam_id"])
            frame_id = int(row["frame_id"])
            t = float(row["timestamp_s"])
            for metric in ("bfi", "bvi", "mean", "contrast"):
                value = row.get(metric)
                if value is None:
                    continue
                buf = self.get_or_create_buffer(side, cam_id, metric)
                buf.append(t=t, v=float(value), frame_id=frame_id)
