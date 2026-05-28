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

import logging
from typing import Optional

import numpy as np
from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot, pyqtProperty

logger = logging.getLogger("openmotion.bloodflow-app.data_sources")


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

    __slots__ = ("t", "v", "frame_id", "n", "_frame_id_to_index", "dropped_at",
                 "ring_trimmed", "_max_capacity")

    def __init__(
        self,
        initial_capacity: Optional[int] = None,
        track_frame_ids: bool = False,
        max_capacity: int = _MAX_CAPACITY,
    ) -> None:
        # Ring-trim threshold: when the buffer fills to max_capacity, the
        # oldest half is dropped. Default ~30 min @ 40 Hz; the live source
        # can pass a smaller value (e.g. for testing the DB lazy-load
        # without waiting 30 min). PastScanSource / the DB window keep the
        # large default so they never trim during a bulk load.
        self._max_capacity = max(2, int(max_capacity))
        if initial_capacity is None:
            initial_capacity = self._max_capacity
        self.t = np.empty(initial_capacity, dtype=np.float64)
        self.v = np.empty(initial_capacity, dtype=np.float32)
        self.frame_id = np.empty(initial_capacity, dtype=np.int64)
        self.n = 0
        self._frame_id_to_index: Optional[dict[int, int]] = (
            {} if track_frame_ids else None
        )
        self.dropped_at: Optional[float] = None
        # True once _ring_trim has dropped older samples — i.e. data
        # below t[0] now lives only in the DB, not memory. LiveScanSource
        # uses this to decide whether a pan below t[0] needs the DB tail
        # vs is simply before the scan start (no data, serve from memory).
        self.ring_trimmed: bool = False

    def _grow(self) -> None:
        """Double the capacity of all three parallel arrays atomically,
        capped at max_capacity. All three must always stay the same
        length — this method is the single chokepoint for that invariant."""
        new_cap = min(self.t.shape[0] * 2, self._max_capacity)
        self.t = np.resize(self.t, new_cap)
        self.v = np.resize(self.v, new_cap)
        self.frame_id = np.resize(self.frame_id, new_cap)

    def _ring_trim(self) -> None:
        """At-cap: drop the oldest half of the buffer in-place so new
        appends keep landing at index n without unbounded growth.
        Called when capacity has already reached max_capacity."""
        half = self._max_capacity // 2
        # Keep the most recent `half` samples (parity-safe — works for
        # odd max_capacity too, e.g. a test-configured small cache).
        keep_from = self.n - half
        self.t[:half] = self.t[keep_from:self.n]
        self.v[:half] = self.v[keep_from:self.n]
        self.frame_id[:half] = self.frame_id[keep_from:self.n]
        self.n = half
        self.ring_trimmed = True
        # Frame-id lookup positions shift after the trim; safest to
        # drop the mapping and rebuild lazily on subsequent appends.
        if self._frame_id_to_index is not None:
            self._frame_id_to_index.clear()

    def append(self, t: float, v: float, frame_id: int) -> None:
        """Append one sample. Grows capacity (doubling) on overflow up
        to max_capacity; at the cap, ring-trims the oldest half."""
        if self.n >= self.t.shape[0]:
            if self.t.shape[0] >= self._max_capacity:
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

        # Stride-aligned causal smoothing. Each output sits at an
        # ABSOLUTE sample index that is a multiple of `stride` — so the
        # same absolute sample always maps to the same output regardless
        # of which paint we're on. And the smoothing is CAUSAL (window
        # covers [N-w+1, N], only samples at or before N) — so once a
        # sample is appended, the output at that absolute index has its
        # final value and never changes.
        #
        # Stride is derived from the TIME WINDOW (× nominal 40 Hz), not
        # from n_window. Keying on n_window meant stride crept up as
        # samples accumulated within a fixed zoom (e.g. 24 → 25 when
        # n_window crossed 4800 at the same zoom level), and every
        # stride change relocated every output to a new absolute index
        # with a new causal window — producing a visible "morph" of
        # the trace shape each time it ticked. Time-keyed stride is
        # invariant under data growth, so the trace stays put.
        #
        # window = stride (not stride*3) keeps the low-pass at the
        # Nyquist-minimum — just enough to suppress aliasing across the
        # stride-subsampled output, without smearing real detail.
        # Earlier 3× kernel hid features even at the tightest 5 s zoom.
        nominal_hz = 40.0  # SDK histogram cadence
        expected_samples = max(1.0, (t_hi - t_lo) * nominal_hz)
        stride = max(1, int(-(-expected_samples // max_points)))

        # Zoomed-in tight enough that every sample fits — no decimation
        # needed, return raw samples. (The earlier `max(2, ...)` floor
        # always forced a 2-sample average even when not needed, which
        # was the source of "looks filtered" at 5 s zoom.)
        if stride == 1:
            t_out = self.t[i_lo:i_hi]
            v_out = self.v[i_lo:i_hi]
            return t_out, v_out

        window = stride

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
                 track_frame_ids: bool = False,
                 buffer_max_capacity: int = _MAX_CAPACITY) -> None:
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
        # Ring-trim threshold for buffers this source creates. Live
        # sources can shrink it (config) to exercise the DB tail without
        # a 30 min scan. Past/DB-window sources keep the large default.
        self._buffer_max_capacity = int(buffer_max_capacity)

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
            buf = _CameraBuffer(track_frame_ids=self._track_frame_ids,
                                max_capacity=self._buffer_max_capacity)
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
    start; lives as long as the connector holds a reference.

    Bounded-memory + DB tail: the in-memory _CameraBuffers ring-trim at
    _MAX_CAPACITY (~30 min @ 40 Hz), dropping the oldest half. Older
    samples remain in the scan DB (the SDK's ScanDBSink "live" channel
    writes per-cam BFI/BVI as the scan runs). When the viewer pans
    BEFORE the in-memory window, points_for_window / value_at lazily
    load the requested range from the DB into a transient window so the
    full scan history stays navigable without unbounded memory growth.

    Pass scan_db_path to enable the DB tail; None (the default, e.g. in
    unit tests) keeps the legacy in-memory-only behavior."""

    def __init__(self, plot_t0: float, parent: Optional[QObject] = None,
                 track_frame_ids: bool = False,
                 scan_db_path: Optional[str] = None,
                 cache_max_samples: int = _MAX_CAPACITY) -> None:
        super().__init__(plot_t0=plot_t0, parent=parent,
                         track_frame_ids=track_frame_ids,
                         buffer_max_capacity=cache_max_samples)
        self._live = True
        # DB tail state — all lazily initialized on first pan-into-past.
        self._scan_db_path = scan_db_path
        self._db = None                       # omotion.ScanDatabase read handle
        self._db_session_id: Optional[int] = None
        self._db_unavailable = False          # set True after a failed resolve
        self._db_window_buffers: dict = {}     # transient (side,cam,metric)->buffer
        self._db_window_lo = float("inf")     # loaded range, exclusive sentinel
        self._db_window_hi = float("-inf")

    # ── DB tail (lazy) ──────────────────────────────────────────────────────

    def _db_ready(self) -> bool:
        """Resolve the read-only DB handle + this live scan's session_id
        on first use. The live scan is always the newest session row, so
        we resolve by MAX(id). Only ever called after the in-memory
        buffer has ring-trimmed (>30 min in), by which point the session
        row exists. Returns False (permanently, after one failure) if the
        DB is unavailable — the source then behaves as in-memory-only."""
        if self._db_unavailable:
            return False
        if self._db is not None and self._db_session_id is not None:
            return True
        if not self._scan_db_path:
            self._db_unavailable = True
            return False
        try:
            from omotion.ScanDatabase import ScanDatabase
            self._db = ScanDatabase(db_path=self._scan_db_path)
            row = next(
                self._db._connection().execute(
                    "SELECT id FROM sessions ORDER BY id DESC LIMIT 1"
                ),
                None,
            )
            if row is None:
                self._db_unavailable = True
                return False
            self._db_session_id = int(row[0])
            logger.info("LiveScanSource: DB tail engaged (session_id=%d)", self._db_session_id)
            return True
        except Exception:
            logger.exception("LiveScanSource: DB tail unavailable")
            self._db_unavailable = True
            return False

    def _ensure_db_window(self, t_lo: float, t_hi: float) -> None:
        """Materialize session_data rows for [t_lo, t_hi] (padded) into
        the transient DB-window buffers, if not already covered. Padding
        by one window each side means small pans reuse the cached load
        instead of re-querying every paint."""
        if t_lo >= self._db_window_lo and t_hi <= self._db_window_hi:
            return
        span = max(1.0, t_hi - t_lo)
        want_lo = max(0.0, t_lo - span)
        want_hi = t_hi + span
        try:
            self._db_window_buffers, _ = _bucketize_session_rows(
                self._db, self._db_session_id, t_lo=want_lo, t_hi=want_hi
            )
            self._db_window_lo = want_lo
            self._db_window_hi = want_hi
        except Exception:
            logger.exception("LiveScanSource: DB window load failed")
            # Leave the previous window in place; a later paint retries.

    def _needs_db_tail(self, side: str, cam_id: int, metric: str, t_lo: float) -> bool:
        """True iff the requested t_lo is below the oldest IN-MEMORY
        sample AND that buffer has actually ring-trimmed (so the older
        data exists only in the DB). Crucially, when the buffer has NOT
        trimmed, t_lo below t[0] just means the window extends before the
        scan started — there's no older data, so serve from memory. This
        is the hot-path guard: during normal live scanning (pre-trim) it
        short-circuits to False WITHOUT touching the DB, so we never open
        a connection or query per-paint until a scan actually exceeds the
        ~30 min in-memory window."""
        buf = self.buffers.get((side, int(cam_id), metric))
        if buf is None or buf.n == 0 or not buf.ring_trimmed:
            return False
        return t_lo < float(buf.t[0])

    @pyqtSlot(str, int, str, float, float, int, result="QVariantList")
    def points_for_window(self, side, cam_id, metric, t_lo, t_hi, max_points):
        # Fast path: whole window in memory, or buffer not yet trimmed.
        # Only reach for the DB once data has actually been ring-trimmed.
        if not self._needs_db_tail(side, int(cam_id), metric, t_lo) or not self._db_ready():
            return super().points_for_window(side, cam_id, metric, t_lo, t_hi, int(max_points))
        # Pan-into-past beyond the in-memory window: serve from the DB
        # tail. The DB has the full history, so a DB-only read of
        # [t_lo, t_hi] is correct — only the un-flushed live edge
        # (~100 ms) is absent, never inside a panned-back window.
        self._ensure_db_window(t_lo, t_hi)
        buf = self._db_window_buffers.get((side, int(cam_id), metric))
        if buf is None:
            return []
        t_arr, v_arr = buf.window_decimated(t_lo, t_hi, int(max_points))
        return [[float(t), float(v)] for t, v in zip(t_arr, v_arr)]

    @pyqtSlot(str, int, str, float, result=float)
    def value_at(self, side, cam_id, metric, t):
        if not self._needs_db_tail(side, int(cam_id), metric, t) or not self._db_ready():
            return super().value_at(side, cam_id, metric, t)
        self._ensure_db_window(t, t)
        buf = self._db_window_buffers.get((side, int(cam_id), metric))
        if buf is None or buf.n == 0:
            return float("nan")
        n = buf.n
        t_slice = buf.t[:n]
        import numpy as _np
        idx = int(_np.searchsorted(t_slice, t, side="left"))
        if idx >= n:
            idx = n - 1
        elif idx > 0 and abs(t_slice[idx - 1] - t) < abs(t_slice[idx] - t):
            idx -= 1
        v = float(buf.v[idx])
        return v if _np.isfinite(v) else float("nan")

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


def _bucketize_session_rows(
    scan_db,
    session_id: int,
    t_lo: Optional[float] = None,
    t_hi: Optional[float] = None,
) -> tuple[dict, bool]:
    """Query session_data rows for a session (optionally a time slice)
    and bucket them into a fresh {(side, cam_id, metric): _CameraBuffer}
    dict. Returns (buffers, has_per_cam_bfi). Shared by PastScanSource's
    full load and LiveScanSource's lazy DB-tail window.

    cam_id >= 0 rows are per-cam (from the SDK "live" channel sink);
    cam_id == -1 rows are the side-aggregated corrected-frame
    placeholders. has_per_cam_bfi reflects whether any real per-cam
    BFI/BVI landed — the caller uses it to decide on CSV fallback."""
    buffers: dict = {}
    has_per_cam_bfi = False
    for row in scan_db.iter_session_data(session_id, t_lo=t_lo, t_hi=t_hi):
        side = _SIDE_INT_TO_STR.get(int(row["side"]))
        if side is None:
            continue
        cam_id = int(row["cam_id"])
        frame_id = int(row["frame_id"])
        t = float(row["timestamp_s"])
        for metric in ("bfi", "bvi", "mean", "contrast"):
            value = row.get(metric)
            if value is None:
                continue
            if cam_id >= 0 and metric in ("bfi", "bvi"):
                has_per_cam_bfi = True
            key = (side, cam_id, metric)
            buf = buffers.get(key)
            if buf is None:
                buf = _CameraBuffer()
                buffers[key] = buf
            buf.append(t=t, v=float(value), frame_id=frame_id)
    return buffers, has_per_cam_bfi


class PastScanSource(ScanDataSource):
    """ScanDataSource constructed from an SDK ScanDatabase session_id.
    Bucketizes session_data rows into per-(side, cam, metric) buffers
    using the same layout as LiveScanSource."""

    def __init__(
        self,
        scan_db,                # omotion.ScanDatabase — duck-typed for tests
        session_id: int,
        corrected_csv_path: Optional[str] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(plot_t0=0.0, parent=parent)
        self._live = False
        self.session_id = int(session_id)

        # session_data carries either:
        #   - Per-cam BFI/BVI rows from the SDK's "live" channel sink
        #     (cam_id 0..7, side 0/1, bfi+bvi+mean+contrast) — Phase 1
        #     and newer scans. The loop below buckets these directly
        #     into the per-cam (side, cam_id, metric) buffers.
        #   - Side-aggregated corrected-frame placeholder rows
        #     (cam_id=-1, side=0, mean+contrast only) — bucketed into
        #     (left, -1, mean) and (left, -1, contrast).
        # Older scans recorded before the live-channel write landed
        # have ONLY the placeholder rows; CSV fallback below picks
        # those up for per-cam display.
        self.buffers, has_per_cam_bfi = _bucketize_session_rows(scan_db, self.session_id)

        # CSV fallback — load only when the DB didn't already carry per-cam
        # BFI/BVI. Pre-Phase-1 scans land here. CsvSink writes this CSV
        # unconditionally for every scan (not gated by writeRawCsv).
        if not has_per_cam_bfi and corrected_csv_path:
            self._load_corrected_csv(corrected_csv_path)

    def _load_corrected_csv(self, csv_path: str) -> None:
        """Load per-(side, cam, metric) samples from the corrected CSV
        written by SDK's CsvSink. Two formats:

        - Normal (dev) mode — 82 cols: frame_id, timestamp_s,
          bfi_l1..bfi_r8, bvi_l1..bvi_r8, mean_l1..mean_r8,
          contrast_l1..contrast_r8, temp_l1..temp_r8. Each metric
          column populates (side, cam_id=0..7, metric).
        - Reduced mode — 6 cols: frame_id, timestamp_s, bfi_left,
          bfi_right, bvi_left, bvi_right. Each side column populates
          (side, cam_id=-1, metric).

        This CSV is written unconditionally for every scan (SDK's
        CsvSink isn't gated by writeRawCsv). Empty cells are skipped.
        Best-effort load — a missing or malformed file just means the
        viewer shows whatever else PastScanSource managed to read.
        """
        import csv as _csv

        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = _csv.DictReader(f)
                fieldnames = reader.fieldnames or []
                # Detect format by presence of the per-cam column for
                # cam 1 in either format.
                is_per_cam = any(c == "bfi_l1" for c in fieldnames)
                is_reduced = any(c == "bfi_left" for c in fieldnames)
                if not (is_per_cam or is_reduced):
                    return
                for row in reader:
                    try:
                        frame_id = int(row.get("frame_id", -1))
                        t = float(row["timestamp_s"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    if is_per_cam:
                        for metric in ("bfi", "bvi", "mean", "contrast"):
                            for side, side_prefix in (("left", "l"), ("right", "r")):
                                for cam_idx in range(8):
                                    # Columns are 1-indexed (l1..l8) so add 1
                                    # for the column name but store 0-indexed.
                                    col = f"{metric}_{side_prefix}{cam_idx + 1}"
                                    raw = row.get(col, "")
                                    if raw is None or raw == "":
                                        continue
                                    try:
                                        v = float(raw)
                                    except ValueError:
                                        continue
                                    buf = self.get_or_create_buffer(side, cam_idx, metric)
                                    buf.append(t=t, v=v, frame_id=frame_id)
                    elif is_reduced:
                        for metric in ("bfi", "bvi"):
                            for side in ("left", "right"):
                                col = f"{metric}_{side}"
                                raw = row.get(col, "")
                                if raw is None or raw == "":
                                    continue
                                try:
                                    v = float(raw)
                                except ValueError:
                                    continue
                                buf = self.get_or_create_buffer(side, -1, metric)
                                buf.append(t=t, v=v, frame_id=frame_id)
        except OSError:
            pass
