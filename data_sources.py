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
import math
import threading
from typing import Optional

import numpy as np
from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot, pyqtProperty

logger = logging.getLogger("openmotion.bloodflow-app.data_sources")


_MAX_CAPACITY = 72000       # ≈ 30 min @ 40 Hz; ring-trim above this.
_INITIAL_CAPACITY = _MAX_CAPACITY

# ── BVI display low-pass (issue #228) ───────────────────────────────────
# The live display applies a 1-pole IIR low-pass to the BVI stream at
# LiveScanSource ingest, governed ONLY by the bviLowPassCutoffHz config
# key (the Settings switch and the bviLowPassEnabled bool are gone).
# Display-side only: the scan DB record (SDK ScanDBSink), CSVs, the
# DB-tail history windows, and replay all stay raw.
BVI_LPF_DEFAULT_CUTOFF_HZ = 20.0
_BVI_LPF_SAMPLE_HZ = 40.0   # nominal live sample rate (dt = 1/40 s)


def resolve_bvi_lpf_cutoff(value) -> float:
    """Resolve the ``bviLowPassCutoffHz`` config value to an effective
    cutoff in Hz. Contract (#228): the number is the only control —
    missing or invalid → the 20 Hz default; ``<= 0`` → 0.0, the
    documented escape hatch that turns the filter off. Bools are
    rejected as invalid (a leftover ``true`` must not become 1 Hz)."""
    if isinstance(value, bool):
        return BVI_LPF_DEFAULT_CUTOFF_HZ
    try:
        cutoff = float(value)
    except (TypeError, ValueError):
        return BVI_LPF_DEFAULT_CUTOFF_HZ
    if math.isnan(cutoff):
        return BVI_LPF_DEFAULT_CUTOFF_HZ
    return cutoff if cutoff > 0.0 else 0.0


def bvi_lpf_alpha(cutoff_hz: float) -> float:
    """Smoothing factor for the 1-pole IIR at the nominal 40 Hz live
    sample rate: ``alpha = dt / (RC + dt)``, ``RC = 1/(2*pi*fc)``,
    ``dt = 1/40 s`` — the same discretization the legacy QML filter
    used (37f7dc9). ``cutoff_hz <= 0`` → 1.0 (pass-through). At the
    20 Hz default this is mild smoothing (alpha ≈ 0.759), since 20 Hz
    sits at Nyquist for the ~40 fps BVI stream."""
    if cutoff_hz <= 0.0:
        return 1.0
    dt = 1.0 / _BVI_LPF_SAMPLE_HZ
    rc = 1.0 / (2.0 * math.pi * float(cutoff_hz))
    return dt / (rc + dt)
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

    Above _MAX_CAPACITY samples, the oldest half is dropped on the
    next overflow (drop-oldest ring behavior). Bounds memory for
    multi-hour scans at the cost of pan-back history beyond ~30 min.
    """

    __slots__ = ("t", "v", "frame_id", "n", "dropped_at",
                 "ring_trimmed", "_max_capacity", "_lock")

    def __init__(
        self,
        initial_capacity: Optional[int] = None,
        max_capacity: Optional[int] = _MAX_CAPACITY,
    ) -> None:
        # Ring-trim threshold: when the buffer fills to max_capacity, the
        # oldest half is dropped. The LIVE source passes a finite value
        # (liveCacheMaxSeconds × 40 Hz) to bound memory. PastScanSource
        # and the DB-tail window pass max_capacity=None (UNBOUNDED) — they
        # bulk-load a finite, already-known result set and must never drop
        # their own rows, so they grow-only from a small initial size.
        self._max_capacity = None if max_capacity is None else max(2, int(max_capacity))
        if initial_capacity is None:
            initial_capacity = self._max_capacity if self._max_capacity is not None else 4096
        self.t = np.empty(initial_capacity, dtype=np.float64)
        self.v = np.empty(initial_capacity, dtype=np.float32)
        self.frame_id = np.empty(initial_capacity, dtype=np.int64)
        self.n = 0
        # Guards n + the arrays against a torn read: the SDK pipeline
        # thread appends (and _ring_trim shifts data in place, rewriting
        # n) while the GUI thread reads via window_decimated/value_near.
        # RLock because window_decimated calls window_indices. Held only
        # for short numpy ops, so contention at 40 Hz × ~32 buffers is
        # negligible.
        self._lock = threading.RLock()
        self.dropped_at: Optional[float] = None
        # True once _ring_trim has dropped older samples — i.e. data
        # below t[0] now lives only in the DB, not memory. LiveScanSource
        # uses this to decide whether a pan below t[0] needs the DB tail
        # vs is simply before the scan start (no data, serve from memory).
        self.ring_trimmed: bool = False

    def _grow(self) -> None:
        """Double the capacity of all three parallel arrays atomically,
        capped at max_capacity (uncapped when max_capacity is None — the
        unbounded historical/DB-window buffers). All three must always stay
        the same length — this method is the single chokepoint for that
        invariant."""
        doubled = self.t.shape[0] * 2
        new_cap = doubled if self._max_capacity is None else min(doubled, self._max_capacity)
        self.t = np.resize(self.t, new_cap)
        self.v = np.resize(self.v, new_cap)
        self.frame_id = np.resize(self.frame_id, new_cap)

    def _ring_trim(self) -> None:
        """At-cap: drop the oldest half of the buffer in-place so new
        appends keep landing at index n without unbounded growth.
        Called when capacity has already reached max_capacity.
        Caller (append) holds _lock."""
        half = self._max_capacity // 2
        # Keep the most recent `half` samples (parity-safe — works for
        # odd max_capacity too, e.g. a test-configured small cache).
        keep_from = self.n - half
        self.t[:half] = self.t[keep_from:self.n]
        self.v[:half] = self.v[keep_from:self.n]
        self.frame_id[:half] = self.frame_id[keep_from:self.n]
        self.n = half
        self.ring_trimmed = True

    def append(self, t: float, v: float, frame_id: int) -> None:
        """Append one sample. Grows capacity (doubling) on overflow up
        to max_capacity; at the cap, ring-trims the oldest half."""
        with self._lock:
            if self.n >= self.t.shape[0]:
                # Unbounded (max_capacity=None): always grow, never trim —
                # these buffers bulk-load a finite, already-known result set
                # (a past scan or a DB-tail window) and must never drop their
                # own loaded rows.
                if self._max_capacity is not None and self.t.shape[0] >= self._max_capacity:
                    self._ring_trim()
                else:
                    self._grow()
            idx = self.n
            self.t[idx] = t
            self.v[idx] = v
            self.frame_id[idx] = frame_id
            self.n += 1

    def window_indices(self, t_lo: float, t_hi: float) -> tuple[int, int]:
        """Return (i_lo, i_hi) such that t[i_lo:i_hi] covers [t_lo, t_hi].
        i_hi is right-exclusive. Binary search; O(log n).

        Precondition: t[0:n] is monotonically non-decreasing. The live pipeline
        appends in timestamp order at 40 Hz; PastScanSource reads rows ordered
        by timestamp_s ASC from the SDK query. Out-of-order append silently
        returns wrong indices — np.searchsorted does not validate."""
        with self._lock:
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

        Thread-safe: the whole read runs under _lock and the returned
        arrays are fresh allocations, so a concurrent append/_ring_trim
        on the pipeline thread can't shift data under the caller.

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
        with self._lock:
            return self._window_decimated_locked(t_lo, t_hi, max_points)

    def _window_decimated_locked(
        self,
        t_lo: float,
        t_hi: float,
        max_points: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        """window_decimated body. Caller holds _lock (RLock — the
        window_indices call below re-enters it)."""
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
            # Copies, not views — a view would escape the lock and read
            # memory a concurrent _ring_trim is shifting.
            t_out = self.t[i_lo:i_hi].copy()
            v_out = self.v[i_lo:i_hi].copy()
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

    def value_near(self, t: float) -> float:
        """Value of the sample nearest to time `t`; NaN when the buffer
        is empty or the nearest sample is itself non-finite.

        Lock-protected: the GUI hover tooltip reads while the pipeline
        thread appends/ring-trims. A torn read of `n` previously caused
        IndexErrors when a searchsorted idx passed a stale-n clamp."""
        with self._lock:
            n = self.n
            if n == 0:
                return float("nan")
            t_slice = self.t[:n]
            idx = int(np.searchsorted(t_slice, t, side="left"))
            if idx >= n:
                idx = n - 1
            elif idx > 0 and abs(t_slice[idx - 1] - t) < abs(t_slice[idx] - t):
                idx -= 1
            v = float(self.v[idx])
            return v if np.isfinite(v) else float("nan")

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
                 buffer_max_capacity: Optional[int] = _MAX_CAPACITY) -> None:
        super().__init__(parent)
        self.plot_t0 = float(plot_t0)
        # Subclasses set _live in __init__ — backing store for the
        # `live` pyqtProperty below. Must be a pyqtProperty (not a
        # plain attribute) for QML to read it; plain Python attributes
        # are invisible to QML and read as `undefined`.
        self._live: bool = False
        # Camera configuration this source represents, as left/right 8-bit
        # masks (bit c set ⇒ camera c recorded). -1 means "unknown" — the
        # default for live sources, which leave the plot grid following the
        # operator's live Scan Settings selection. PastScanSource overrides
        # these with the config the replayed scan actually recorded (issue
        # #175) so its grid doesn't inherit the current selection.
        self._left_mask: int = -1
        self._right_mask: int = -1
        # Recorded display mode, tri-state: -1 unknown / 0 per-camera / 1
        # clinical. -1 for live sources (viewer follows the live config);
        # PastScanSource derives the real value from its buffer layout.
        self._clinical_mode: int = -1
        # Human-readable identity for the viewer's "Viewing" badge (issue
        # #245): the operator's user label and the scan's date/time. Empty
        # on live sources — the badge is past-scan only. PastScanSource sets
        # these from the same session row the History list shows.
        self._user_label: str = ""
        self._date_time: str = ""
        self.buffers: dict[tuple[str, int, str], _CameraBuffer] = {}
        # Ring-trim threshold for buffers this source creates. Live sources
        # pass a finite value (config) to bound memory; past sources pass None
        # (UNBOUNDED) so a bulk load of a long scan never ring-trims away its
        # own oldest rows mid-load.
        self._buffer_max_capacity = (
            None if buffer_max_capacity is None else int(buffer_max_capacity)
        )

        # Pending dirty bookkeeping: (side, cam, metric) -> accumulated count.
        self._pending: dict[tuple[str, int, str], int] = {}

        # Set by release() when the connector retires this source; guards
        # against double-release and marks the flush timer as stopped.
        self._released = False

        # Throttle timer — fires every 100 ms while the source is alive.
        # Tests can call _flush() directly and ignore the timer entirely.
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(_FLUSH_INTERVAL_MS)
        self._flush_timer.timeout.connect(self._flush)
        self._flush_timer.start()

    # ── public ────────────────────────────────────────────────────────────

    def release(self) -> None:
        """Stop the flush timer and drop external resources. Idempotent.

        Called by the connector right before deleteLater() when this source
        is no longer reachable from QML (superseded by a newer scan or
        another History view). Buffers stay readable — a late paint during
        the deleteLater window must not crash — but no further
        samplesAppended fires."""
        if self._released:
            return
        self._released = True
        self._flush_timer.stop()
        self._pending.clear()

    @pyqtProperty(bool, constant=True)
    def live(self) -> bool:
        """True for LiveScanSource, False for PastScanSource. Must be a
        pyqtProperty (not a plain attribute) for QML to read it — plain
        Python attributes resolve to ``undefined`` in QML, which silently
        broke the toolbar's source-type label and the "Back to live"
        button's variant selection (both branched on `scanSource.live`)."""
        return self._live

    @pyqtProperty(int, constant=True)
    def leftMask(self) -> int:
        """Left-sensor camera mask this source represents, or -1 if unknown.

        Exposed as ``pyqtProperty`` (not a plain attribute) so QML can read
        it — PlotViewer prefers this over the live selection when it's a
        real mask (>= 0), so a replayed scan lays its grid out from its own
        configuration (issue #175). ``constant=True``: set once at
        construction, never changes for the life of the source."""
        return self._left_mask

    @pyqtProperty(int, constant=True)
    def rightMask(self) -> int:
        """Right-sensor camera mask this source represents, or -1 if unknown.
        See ``leftMask``."""
        return self._right_mask

    @pyqtProperty(int, constant=True)
    def clinicalMode(self) -> int:
        """Recorded display mode this source represents: -1 unknown, 0
        per-camera, 1 clinical. Exposed for QML (PlotViewer.effectiveClinical)
        so a replayed scan renders in its own mode rather than the live config.
        See ``leftMask`` for why this must be a pyqtProperty."""
        return self._clinical_mode

    @pyqtProperty(str, constant=True)
    def userLabel(self) -> str:
        """Operator-entered label for the viewer's "Viewing" badge (issue
        #245), or "" when none (live sources, unlabeled scans). Exposed as a
        ``pyqtProperty`` so QML can read it; ``constant=True`` since it's set
        once at construction. See ``leftMask`` for the plain-attribute caveat."""
        return self._user_label

    @pyqtProperty(str, constant=True)
    def dateTime(self) -> str:
        """The scan's date/time for the viewer badge (issue #245), formatted
        as ``YYYY-MM-DD HH:MM:SS`` like the History list; "" when unknown. The
        viewer trims it to minutes for display. See ``userLabel``."""
        return self._date_time

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
            buf = _CameraBuffer(max_capacity=self._buffer_max_capacity)
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

        Concurrency handled inside _CameraBuffer.value_near — the lookup
        runs under the buffer lock so a pipeline-thread append/ring-trim
        can't shift data mid-read (a torn read of n used to cause
        IndexErrors here)."""
        buf = self.buffers.get((side, int(cam_id), metric))
        if buf is None:
            return float("nan")
        return buf.value_near(t)

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

    def last_sample_t(self, side: str, cam_id: int) -> float:
        """Newest sample timestamp across this (side, cam)'s metric
        buffers, or NaN when the camera has no samples yet.

        This is where the camera's trace visibly ends — the dropout
        watchdog anchors the plot's dropout marker here so the marker
        lives on the SAME time axis as the samples (SDK-normalized,
        t=0 at the scan's first frame). See issue #284: a marker
        computed on any other clock lands off the sample axis and
        renders clipped or at the wrong x."""
        latest = float("nan")
        for metric in ("bfi", "bvi", "mean", "contrast"):
            buf = self.buffers.get((side, int(cam_id), metric))
            if buf is None:
                continue
            # Under the buffer lock: the pipeline thread appends (and
            # ring-trims, shifting data + rewriting n) concurrently.
            with buf._lock:
                if buf.n == 0:
                    continue
                t = float(buf.t[buf.n - 1])
            if math.isnan(latest) or t > latest:
                latest = t
        return latest

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

        Semantics carried over from the legacy plot's _boundsFromArray
        (2%/98% percentile bounds + 25% padding)."""
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
    The load runs on a worker thread (issue #256) — until it lands,
    reads serve the in-memory portion plus whatever the previous cached
    window covers, then historyWindowLoaded triggers a repaint.

    Pass scan_db_path to enable the DB tail; None (the default, e.g. in
    unit tests) keeps the legacy in-memory-only behavior."""

    # Emitted (from the loader thread — PyQt queues it to the GUI thread)
    # when an async DB-window load lands. PlotViewer marks itself dirty on
    # this so the newly-available history paints even when no live samples
    # are arriving to tick the paint throttle.
    historyWindowLoaded = pyqtSignal()

    def __init__(self, plot_t0: float, parent: Optional[QObject] = None,
                 scan_db_path: Optional[str] = None,
                 cache_max_samples: int = _MAX_CAPACITY,
                 bvi_lpf_cutoff_hz: float = 0.0) -> None:
        super().__init__(plot_t0=plot_t0, parent=parent,
                         buffer_max_capacity=cache_max_samples)
        self._live = True
        # BVI display low-pass (issue #228): 1-pole IIR applied at ingest,
        # per (side, cam_id) stream (cam_id -1 = clinical side average).
        # The constructor default 0.0 = OFF, preserving raw-storage
        # semantics for direct constructions (tests); the connector — the
        # sole production construction site — passes the config-resolved
        # cutoff (resolve_bvi_lpf_cutoff: missing/invalid → 20 Hz). State
        # is per-scan by construction: a fresh source per scan.
        self._bvi_lpf_alpha = bvi_lpf_alpha(float(bvi_lpf_cutoff_hz))
        self._bvi_lpf_prev: dict[tuple[str, int], float] = {}
        # DB tail state — all lazily initialized on first pan-into-past.
        self._scan_db_path = scan_db_path
        self._db = None                       # omotion.ScanDatabase read handle
        self._db_session_id: Optional[int] = None
        self._db_unavailable = False          # set True only on HARD failure
        # Exact session label for THIS scan (f"{scan_id}_{subject_id}"), set
        # by the connector right after start_scan via set_scan_label. Lets
        # the DB tail bind to this scan's session row by label instead of
        # guessing the newest session (which could be another scan's).
        self._scan_label: Optional[str] = None
        self._db_window_buffers: dict = {}     # transient (side,cam,metric)->buffer
        self._db_window_lo = float("inf")     # loaded range, exclusive sentinel
        self._db_window_hi = float("-inf")
        # Async DB-window loader (issue #256): the query + bucketize run on
        # a worker thread; the GUI thread only checks coverage and schedules.
        # _db_load_lock guards the window bounds/buffers install and the
        # thread handle. One load in flight at a time — a stale request is
        # simply re-issued by a later paint if still uncovered.
        self._db_load_lock = threading.Lock()
        self._db_load_thread: Optional[threading.Thread] = None
        # Test hook: False runs loads inline so assertions are deterministic.
        self._db_load_async = True

    def release(self) -> None:
        """Base release plus DB-tail teardown: close the read handle and
        drop the transient window buffers so a retired live source can't
        hold the scan DB open for the app's lifetime."""
        if self._released:
            return
        super().release()
        db = self._db
        self._db = None
        self._db_unavailable = True  # never reopen after release
        self._db_window_buffers = {}
        if db is not None:
            try:
                db.close()
            except Exception:
                logger.warning("scan DB close failed on release",
                               exc_info=True)

    def set_scan_label(self, label: Optional[str]) -> None:
        """Bind this live source to a specific scan-DB session by label
        (``f"{scan_id}_{subject_id}"``). Called once, right after the scan
        starts; the DB tail resolves the session_id from it on first use."""
        if label:
            self._scan_label = str(label)

    # ── DB tail (lazy) ──────────────────────────────────────────────────────

    def _db_ready(self) -> bool:
        """Resolve the read-only DB handle + this live scan's session_id on
        first use. Only ever called after the in-memory buffer has ring-
        trimmed, by which point the session row exists.

        Binds to THIS scan's session by its exact label (set via
        set_scan_label) so a concurrent/older scan's session is never picked;
        falls back to the newest session only when no label was provided
        (older callers). Permanent disable (_db_unavailable) is reserved for
        HARD failures — no DB path, or an exception opening it. A merely
        not-yet-visible session row is a SOFT miss: returns False without
        disabling so a later paint retries (the row is created at scan start,
        so this should essentially never persist)."""
        if self._db_unavailable:
            return False
        if self._db is not None and self._db_session_id is not None:
            return True
        if not self._scan_db_path:
            self._db_unavailable = True  # hard: no path → never works
            return False
        try:
            if self._db is None:
                from omotion.ScanDatabase import ScanDatabase
                self._db = ScanDatabase(db_path=self._scan_db_path)
            sid = self._resolve_session_id()
            if sid is None:
                # Soft miss — keep _db open and retry on a later paint.
                return False
            self._db_session_id = sid
            logger.info(
                "LiveScanSource: DB tail engaged (session_id=%d, label=%r)",
                sid, self._scan_label,
            )
            return True
        except Exception:
            logger.exception("LiveScanSource: DB tail unavailable")
            self._db_unavailable = True  # hard failure
            return False

    def _resolve_session_id(self) -> Optional[int]:
        """Return this scan's session_id, or None if not yet resolvable.

        Prefers an exact label match (this scan's session row); falls back to
        the newest session when no label was bound. Raises only on a real DB
        error (handled by _db_ready)."""
        conn = self._db._connection()
        if self._scan_label:
            row = next(
                conn.execute(
                    "SELECT id FROM sessions WHERE session_label = ? "
                    "ORDER BY session_start DESC, id DESC LIMIT 1",
                    (self._scan_label,),
                ),
                None,
            )
            return int(row[0]) if row is not None else None
        # No label bound (older callers): newest session is this live scan.
        row = next(
            conn.execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1"),
            None,
        )
        return int(row[0]) if row is not None else None

    def _ensure_db_window(self, t_lo: float, t_hi: float) -> None:
        """Make sure the transient DB-window buffers cover [t_lo, t_hi]
        (padded), scheduling an ASYNC load when they don't. Never blocks
        the GUI thread on the SQLite query + bucketize — loading the
        window synchronously here froze the whole app for seconds per
        zoom/pan step on long All-camera scans (issue #256: 16 cams ×
        40 Hz ≈ 640 rows/s of history, so one padded 60 s window is
        ~10⁵ rows re-bucketized per interaction). Until the load lands,
        callers serve the in-memory portion plus whatever the previous
        cached window still covers; historyWindowLoaded then triggers a
        repaint with the fresh history. Padding by one window each side
        means small pans reuse the cached load instead of re-querying
        every paint.

        want_hi is capped at the newest available sample (liveEdge) so the
        cached window never claims to cover future/nonexistent rows — that
        false coverage used to make the staleness check short-circuit and
        serve a stale window while live data piled up unshown. Callers only
        pass t_hi up to the in-memory boundary anyway (the recent portion is
        served from memory), so this cap is a belt-and-suspenders guard.

        t_lo is clamped to >= 0 BEFORE the staleness check: scan timestamps
        start at 0, so no rows can ever exist below it, and the stored
        _db_window_lo is itself floored at 0 (want_lo). A followLive window
        wider than the elapsed scan — wheel-zoom out to e.g. 600 s, then
        "Back to live" mid-scan — requests t_lo = liveEdge - windowSeconds
        < 0 on EVERY paint; unclamped, that request can never be satisfied
        by the cache, so every points_for_window call (cells × metrics,
        ~30 Hz) re-queried and re-bucketized the whole DB window
        (issue #151)."""
        t_lo = max(0.0, float(t_lo))
        t_hi = max(t_lo, float(t_hi))
        with self._db_load_lock:
            if t_lo >= self._db_window_lo and t_hi <= self._db_window_hi:
                return
            loader_busy = (self._db_load_thread is not None
                           and self._db_load_thread.is_alive())
        if loader_busy:
            # One load in flight at a time. If this request is still
            # uncovered when the in-flight load lands, the next paint
            # re-issues it — the range converges without a queue.
            return
        span = max(1.0, t_hi - t_lo)
        want_lo = max(0.0, t_lo - span)
        edge = self.liveEdge
        want_hi = t_hi + span
        if edge > 0.0:
            want_hi = min(want_hi, edge)
        if not self._db_load_async:
            self._db_window_load(want_lo, want_hi)
            return
        thread = threading.Thread(
            target=self._db_window_load, args=(want_lo, want_hi),
            name="plot-db-window-load", daemon=True,
        )
        with self._db_load_lock:
            self._db_load_thread = thread
        thread.start()

    def _db_window_load(self, want_lo: float, want_hi: float) -> None:
        """Loader body — worker thread in the app, inline when
        _db_load_async is False (tests). Queries + bucketizes the padded
        range, installs the fresh window under the lock, and pings the
        viewer. Sharing the sqlite handle with the GUI thread is safe:
        it's opened check_same_thread=False, the GUI only touches it for
        the one-shot session-id resolve in _db_ready (before any load can
        be scheduled), and the single-inflight rule serializes loads."""
        try:
            buffers, _ = _bucketize_session_rows(
                self._db, self._db_session_id, t_lo=want_lo, t_hi=want_hi
            )
        except Exception:
            if self._released:
                # release() closed the DB under an in-flight load — the
                # result would have been discarded anyway.
                logger.debug("LiveScanSource: DB window load aborted by "
                             "release", exc_info=True)
            else:
                logger.exception("LiveScanSource: DB window load failed")
            # Leave the previous window in place; a later paint retries.
            return
        if self._released:
            return
        with self._db_load_lock:
            self._db_window_buffers = buffers
            self._db_window_lo = want_lo
            self._db_window_hi = want_hi
        self.historyWindowLoaded.emit()

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
    def points_for_window(
        self,
        side: str,
        cam_id: int,
        metric: str,
        t_lo: float,
        t_hi: float,
        max_points: int,
    ) -> list:
        # Fast path: whole window in memory, or buffer not yet trimmed.
        # Only reach for the DB once data has actually been ring-trimmed.
        if not self._needs_db_tail(side, int(cam_id), metric, t_lo) or not self._db_ready():
            return super().points_for_window(side, cam_id, metric, t_lo, t_hi, int(max_points))

        # Straddling window: t_lo is below the oldest in-memory sample but
        # t_hi may reach into (or past) the live edge. Split at the in-memory
        # boundary and stitch: serve the OLD portion [t_lo, split] from the
        # DB tail (static history) and the RECENT portion [split, t_hi] from
        # the in-memory buffer (fresh every paint). Serving the recent portion
        # from the DB instead would freeze the live trace — the DB window is
        # cached and only reloads when `split` advances, so new live samples
        # wouldn't appear until the next reload. The DB is queried only up to
        # `split`, never into the live/future region.
        buf = self.buffers.get((side, int(cam_id), metric))
        split = float(buf.t[0])  # _needs_db_tail guarantees buf valid & n>0
        db_hi = min(split, float(t_hi))
        mem_lo = max(split, float(t_lo))

        # Proportional point budget so trace density (stride) is consistent
        # across the seam rather than each portion getting the full budget.
        total_span = max(1e-9, float(t_hi) - float(t_lo))
        db_span = max(0.0, db_hi - float(t_lo))
        mem_span = max(0.0, float(t_hi) - mem_lo)
        mp = int(max_points)
        db_max = max(1, int(round(mp * db_span / total_span)))
        mem_max = max(1, int(round(mp * mem_span / total_span)))

        db_pts: list = []
        if db_span > 0.0:
            # Non-blocking: schedules an async load when the cached window
            # doesn't cover the range. Until it lands we render whatever
            # overlap the previous cached window still has (possibly
            # nothing) — historyWindowLoaded repaints with the fresh rows.
            self._ensure_db_window(float(t_lo), db_hi)
            dbuf = self._db_window_buffers.get((side, int(cam_id), metric))
            if dbuf is not None:
                t_arr, v_arr = dbuf.window_decimated(float(t_lo), db_hi, db_max)
                db_pts = [[float(t), float(v)] for t, v in zip(t_arr, v_arr)]

        mem_pts: list = []
        if mem_span > 0.0:
            mem_pts = super().points_for_window(
                side, cam_id, metric, mem_lo, float(t_hi), mem_max
            )
        return db_pts + mem_pts

    @pyqtSlot(str, int, str, float, result=float)
    def value_at(self, side: str, cam_id: int, metric: str, t: float) -> float:
        if not self._needs_db_tail(side, int(cam_id), metric, t) or not self._db_ready():
            return super().value_at(side, cam_id, metric, t)
        self._ensure_db_window(t, t)
        buf = self._db_window_buffers.get((side, int(cam_id), metric))
        if buf is None:
            return float("nan")
        return buf.value_near(t)

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
        temp: Optional[float] = None,
    ) -> None:
        """Append one frame's worth of uncorrected metrics.

        bfi and bvi are always appended (NaN included — the source stores
        what arrives). bvi alone passes through the display low-pass
        first when a cutoff was configured (see _bvi_lpf; issue #228).
        mean/contrast/temp are appended only when non-None;
        the existing _LivePlotSink passes None for samples where the
        SDK reported a non-finite mean_dc_rt / contrast_sn_rt, and None
        temp for dark frames (whose camera-temp reading is meaningless)."""
        self._append_one(side, cam_id, "bfi", frame_id, t, bfi)
        self._append_one(side, cam_id, "bvi", frame_id, t,
                         self._bvi_lpf(side, cam_id, bvi))
        if mean is not None:
            self._append_one(side, cam_id, "mean", frame_id, t, mean)
        if contrast is not None:
            self._append_one(side, cam_id, "contrast", frame_id, t, contrast)
        if temp is not None:
            self._append_one(side, cam_id, "temp", frame_id, t, temp)

    def mark_dropped(self, side: str, cam_id: int, t: float) -> None:
        """Record a dropout timestamp on every existing metric buffer for
        this (side, cam). Idempotent per _CameraBuffer.mark_dropped."""
        for metric in ("bfi", "bvi", "mean", "contrast"):
            buf = self.buffers.get((side, int(cam_id), metric))
            if buf is not None:
                buf.mark_dropped(t)

    # ── internal ──────────────────────────────────────────────────────────

    def _bvi_lpf(self, side: str, cam_id: int, bvi: float) -> float:
        """1-pole IIR low-pass on the DISPLAY BVI stream (issue #228).

        ``y[n] = y[n-1] + alpha * (x[n] - y[n-1])``; the first finite
        sample seeds the state (no ramp-in from zero). Non-finite input
        (NaN side averages while sensors are covered) is returned as-is —
        the renderer skips NaN — and leaves the state untouched, so the
        filter resumes from the last finite output. alpha >= 1 (filter
        off) short-circuits to raw."""
        alpha = self._bvi_lpf_alpha
        if alpha >= 1.0 or not math.isfinite(bvi):
            return bvi
        key = (side, int(cam_id))
        prev = self._bvi_lpf_prev.get(key)
        out = bvi if prev is None else prev + alpha * (bvi - prev)
        self._bvi_lpf_prev[key] = out
        return out

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
    dict. Returns (buffers, has_bfi). Shared by PastScanSource's full load
    and LiveScanSource's lazy DB-tail window.

    Rows are bucketed by their stored cam_id verbatim:
      - cam_id 0..7 — per-camera BFI/BVI/mean/contrast (normal-mode scans).
      - cam_id == -1 — the clinical-mode dark-corrected per-side average
        (bfi/bvi/mean/contrast), persisted by ScanDBSink from the cam_id=-1
        frames the SDK's SideAverageStage routes onto the "final" channel.
    has_bfi reflects whether ANY finite BFI/BVI landed (either layout) — the
    caller uses it to decide whether to fall back to the corrected CSV (only
    pre-pipeline scans, which carry no BFI/BVI in the DB)."""
    buffers: dict = {}
    has_bfi = False
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
            if metric in ("bfi", "bvi"):
                has_bfi = True
            key = (side, cam_id, metric)
            buf = buffers.get(key)
            if buf is None:
                # Unbounded: this is a bulk load of a finite query result
                # (a full past scan or a DB-tail window). A finite ring-trim
                # cap would drop the oldest loaded rows mid-load.
                buf = _CameraBuffer(max_capacity=None)
                buffers[key] = buf
            buf.append(t=t, v=float(value), frame_id=frame_id)
    return buffers, has_bfi


def _get_or_create_unbounded(
    buffers: dict, side: str, cam_id: int, metric: str
) -> _CameraBuffer:
    """Fetch-or-create an UNBOUNDED _CameraBuffer in a plain buffers dict.
    Used by the bulk loaders (past scan / CSV fallback), which load a
    finite, already-known result set and must never ring-trim it."""
    key = (side, int(cam_id), metric)
    buf = buffers.get(key)
    if buf is None:
        buf = _CameraBuffer(max_capacity=None)
        buffers[key] = buf
    return buf


def _load_corrected_csv_into(buffers: dict, csv_path: str) -> None:
    """Load per-(side, cam, metric) samples from the corrected CSV
    written by SDK's CsvSink into `buffers`. Two formats:

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
    viewer shows whatever else made it into `buffers`.
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
                        for side, prefix in (("left", "l"), ("right", "r")):
                            for cam_idx in range(8):
                                # Columns are 1-indexed (l1..l8) so add 1
                                # for the column name but store 0-indexed.
                                col = f"{metric}_{prefix}{cam_idx + 1}"
                                raw = row.get(col, "")
                                if raw is None or raw == "":
                                    continue
                                try:
                                    v = float(raw)
                                except ValueError:
                                    continue
                                buf = _get_or_create_unbounded(
                                    buffers, side, cam_idx, metric
                                )
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
                            buf = _get_or_create_unbounded(
                                buffers, side, -1, metric
                            )
                            buf.append(t=t, v=v, frame_id=frame_id)
    except OSError:
        # Best-effort by contract (see docstring), but never silent — a
        # missing/unreadable corrected CSV means the viewer shows less data.
        logger.warning("corrected-CSV fallback load failed: %s",
                       csv_path, exc_info=True)


def buffers_are_empty(buffers: Optional[dict]) -> bool:
    """True when a loaded past-scan buffer dict holds no samples at all.

    A scan interrupted before any dark interval closed yields a session row
    but zero session_data and no corrected CSV, so the loader returns an
    empty (or sample-less) dict. The viewer uses this to show a 'no data'
    error instead of silently opening an empty plot."""
    if not buffers:
        return True
    return all(getattr(b, "n", 0) == 0 for b in buffers.values())


def _derive_masks_from_buffers(buffers: dict) -> tuple[int, int]:
    """Reconstruct (left_mask, right_mask) from which per-camera streams a
    loaded scan carries — bit c set ⇒ camera c (cam_id 0..7) recorded data.

    Returns (-1, -1) when no per-camera streams exist (e.g. a clinical-mode
    scan whose only buffers are the cam_id=-1 side average) so the viewer
    falls back to its live/preview masks rather than rendering an empty
    normal-mode grid. A side that recorded nothing yields mask 0 as long as
    the other side has cameras — preserving an asymmetric/one-sided scan's
    true per-side configuration (issue #175)."""
    masks = {"left": 0, "right": 0}
    for key in buffers:
        side, cam_id = key[0], key[1]
        if side in masks and 0 <= cam_id < 8:
            masks[side] |= (1 << cam_id)
    if masks["left"] == 0 and masks["right"] == 0:
        return -1, -1
    return masks["left"], masks["right"]


def _derive_clinical_from_buffers(buffers) -> int:
    """Tri-state recorded display mode from a loaded scan's buffer cam_ids:
    -1 unknown, 0 per-camera, 1 clinical. Clinical-mode scans store only the
    cam_id=-1 side average; per-camera (engineering) scans store cam_id 0..7.
    Mirrors _derive_masks_from_buffers — the viewer prefers this over the live
    config so replay renders in the mode the scan was captured in."""
    if buffers_are_empty(buffers):
        return -1
    if any(0 <= key[1] < 8 for key in buffers):
        return 0
    if any(key[1] == -1 for key in buffers):
        return 1
    return -1


def _masks_from_flags(flags) -> Optional[tuple[int, int]]:
    """Authoritative (left_mask, right_mask) from a scan's stored
    ``session_meta.sdk_flags`` (written by the SDK's ScanDBSink at scan start),
    or None when the flags don't carry a usable per-side mask pair.

    This is the configuration the scan was actually RUN with — preferred over
    _derive_masks_from_buffers, which only reconstructs the cameras that
    happened to log data and so under-represents a config whose outer cameras
    recorded nothing (issue #175 reopen). Returns None — caller falls back to
    the derived heuristic — when ``flags`` isn't a dict, either mask key is
    missing/non-int, or BOTH masks are 0 (no real scan configures zero
    cameras; mirrors _derive_masks_from_buffers' both-zero "unknown")."""
    if not isinstance(flags, dict):
        return None
    lm = flags.get("left_camera_mask")
    rm = flags.get("right_camera_mask")
    # bool is an int subclass; a mask is never a bool, so reject it explicitly.
    if not isinstance(lm, int) or isinstance(lm, bool):
        return None
    if not isinstance(rm, int) or isinstance(rm, bool):
        return None
    lm &= 0xFF
    rm &= 0xFF
    if lm == 0 and rm == 0:
        return None
    return lm, rm


def _clinical_from_flags(flags) -> Optional[int]:
    """Recorded display mode (0 per-camera / 1 clinical) from a scan's stored
    ``session_meta.sdk_flags.reduced_mode``, or None when absent. Preferred
    over _derive_clinical_from_buffers for the same reason as _masks_from_flags
    — it reflects how the scan was captured, not what its data happens to
    contain (issue #175 reopen)."""
    if not isinstance(flags, dict):
        return None
    rv = flags.get("reduced_mode")
    if rv is None:
        return None
    return 1 if rv else 0


def load_past_scan_buffers(
    scan_db,
    session_id: int,
    corrected_csv_path: Optional[str] = None,
) -> tuple[dict, bool]:
    """Bulk-load a past scan's samples into a plain
    {(side, cam_id, metric): _CameraBuffer} dict. Returns (buffers, has_bfi).

    This is the HEAVY part of opening a past scan — a multi-hour scan is
    millions of session_data rows (issue #152 measured ~8.3M samples ≈ 20 s).
    It is deliberately Qt-free (pure Python + numpy + sqlite) so callers can
    run it on a worker thread and hand the finished dict to
    PastScanSource(preloaded_buffers=...) on the GUI thread.

    session_data carries, by cam_id:
      - cam_id 0..7  — per-camera BFI/BVI/mean/contrast (normal-mode scans).
      - cam_id == -1 — the clinical-mode dark-corrected per-side average
        (cam_id=-1 frames on the "final" channel, persisted by ScanDBSink).
        Clinical cells query cam_id=-1, so replay reads it straight from
        the DB — no derivation.
    Pre-pipeline scans carry no BFI/BVI in the DB; the corrected-CSV
    fallback covers those (CsvSink writes it for every scan)."""
    buffers, has_bfi = _bucketize_session_rows(scan_db, int(session_id))
    if not has_bfi and corrected_csv_path:
        _load_corrected_csv_into(buffers, corrected_csv_path)
    return buffers, has_bfi


class PastScanSource(ScanDataSource):
    """ScanDataSource constructed from an SDK ScanDatabase session_id.
    Bucketizes session_data rows into per-(side, cam, metric) buffers
    using the same layout as LiveScanSource.

    Pass `preloaded_buffers` (built off-thread via load_past_scan_buffers)
    to skip the synchronous DB/CSV walk entirely — constructing the QObject
    itself is cheap and stays on the GUI thread (issue #152)."""

    def __init__(
        self,
        scan_db,                # omotion.ScanDatabase — duck-typed for tests
        session_id: int,
        corrected_csv_path: Optional[str] = None,
        parent: Optional[QObject] = None,
        preloaded_buffers: Optional[dict] = None,
        recorded_flags: Optional[dict] = None,
        user_label: str = "",
        date_time: str = "",
    ) -> None:
        # Unbounded buffers: a past scan is a finite, already-known result set
        # loaded in bulk (DB rows and/or the corrected CSV). With a finite
        # ring-trim cap, loading a long scan would drop its own oldest rows
        # as it filled — corrupting the very history the viewer pans back to.
        super().__init__(plot_t0=0.0, parent=parent, buffer_max_capacity=None)
        self._live = False
        self.session_id = int(session_id)
        # Viewer "Viewing" badge identity (issue #245). Coerced to str so a
        # stray None from the meta lookup can't reach QML as `undefined`.
        self._user_label = str(user_label or "")
        self._date_time = str(date_time or "")

        if preloaded_buffers is not None:
            # Already bulk-loaded on a worker thread — adopt as-is.
            self.buffers = preloaded_buffers
        else:
            # Synchronous load (tests / small scans): see
            # load_past_scan_buffers for the layout and CSV-fallback rules.
            self.buffers, _ = load_past_scan_buffers(
                scan_db, self.session_id, corrected_csv_path
            )

        # Lay the plot grid out from THIS scan's recorded cameras, not the
        # operator's live selection (issue #175). Prefer the authoritative
        # config the scan was RUN with — stored in session_meta.sdk_flags by
        # the SDK's ScanDBSink and threaded in as recorded_flags — over the
        # data-derived heuristic below. Deriving from data alone collapses a
        # config whose outer cameras logged nothing: an All/All scan where only
        # the middle cameras produced rows would render a Middle grid, hiding
        # the configured-but-empty cameras (issue #175 reopen). Fall back to
        # the heuristic for older scans whose meta predates these flags.
        self._left_mask, self._right_mask = _derive_masks_from_buffers(
            self.buffers
        )
        self._clinical_mode = _derive_clinical_from_buffers(self.buffers)
        recorded_masks = _masks_from_flags(recorded_flags)
        if recorded_masks is not None:
            self._left_mask, self._right_mask = recorded_masks
        recorded_clinical = _clinical_from_flags(recorded_flags)
        if recorded_clinical is not None:
            self._clinical_mode = recorded_clinical
