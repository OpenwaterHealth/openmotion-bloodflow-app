"""Per-scan data sources for the bloodflow real-time plot viewer.

Design: docs/superpowers/specs/2026-05-22-realtime-plot-viewer-design.md

  - _CameraBuffer   : per-(side, cam, metric) growable numpy buffer.
  - ScanDataSource  : QObject base QML reads (the connector's
                      currentScanSource). Owns the buffers and the throttled
                      samplesAppended signal.
  - LiveScanSource  : fed by the in-flight pipeline; bounded memory with a
                      lazy scan-DB tail for panning past the ring.
  - PastScanSource  : a finished scan, bulk-loaded from the scan DB or a
                      corrected CSV into the same layout.
"""

from __future__ import annotations

import csv
import logging
import math
import threading
from typing import Optional

import numpy as np
from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot, pyqtProperty

logger = logging.getLogger("openmotion.bloodflow-app.data_sources")


_MAX_CAPACITY = 72000       # ≈ 30 min @ 40 Hz; ring-trim above this.
_NOMINAL_HZ = 40.0          # SDK histogram cadence / live sample rate
_FLUSH_INTERVAL_MS = 100    # spec §"Throttled UI notify"
_METRICS = ("bfi", "bvi", "mean", "contrast")
_SIDES = (("left", "l"), ("right", "r"))    # (side name, per-cam CSV column prefix)
# session_data.side is stored as INTEGER; the rest of the app uses names.
_SIDE_INT_TO_STR = {0: "left", 1: "right"}

# ── BVI display low-pass (issues #228, #552) ────────────────────────────
# The live display applies a 1-pole IIR low-pass to the BVI stream at
# LiveScanSource ingest. The cutoff is the compiled bviLowPassCutoffHz
# constant; the research-only Settings switch (bviLowPassEnabled, #552)
# gates it on/off. Display-side only: the scan DB record (SDK ScanDBSink),
# CSVs, the DB-tail history windows, and replay all stay raw.
BVI_LPF_DEFAULT_CUTOFF_HZ = 20.0


def resolve_bvi_lpf_cutoff(value) -> float:
    """Resolve the ``bviLowPassCutoffHz`` config value to a cutoff in Hz.
    Contract (#228): missing or invalid → the 20 Hz default; ``<= 0`` → 0.0,
    the documented escape hatch that turns the filter off. Bools are invalid
    (a leftover ``true`` must not become 1 Hz)."""
    try:
        cutoff = math.nan if isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        cutoff = math.nan
    if math.isnan(cutoff):
        return BVI_LPF_DEFAULT_CUTOFF_HZ
    return cutoff if cutoff > 0.0 else 0.0


def effective_bvi_lpf_cutoff(enabled, cutoff_value) -> float:
    """Cutoff the live source should run with (0.0 = off), given the Settings
    switch (``bviLowPassEnabled``, #552). Only an explicit ``False`` turns the
    filter off: a missing or garbage flag keeps the shipped default (on)."""
    return 0.0 if enabled is False else resolve_bvi_lpf_cutoff(cutoff_value)


def bvi_lpf_alpha(cutoff_hz: float) -> float:
    """Smoothing factor of the 1-pole IIR at the nominal 40 Hz sample rate:
    ``alpha = dt / (RC + dt)``, ``RC = 1/(2*pi*fc)``, the discretization the
    legacy QML filter used (37f7dc9). ``cutoff_hz <= 0`` → 1.0 (pass-through).
    The 20 Hz default is mild (alpha ≈ 0.759): 20 Hz is Nyquist at ~40 fps."""
    if cutoff_hz <= 0.0:
        return 1.0
    dt = 1.0 / _NOMINAL_HZ
    rc = 1.0 / (2.0 * math.pi * float(cutoff_hz))
    return dt / (rc + dt)


class _CameraBuffer:
    """Append-only buffer for one (side, cam_id, metric) stream: three
    parallel arrays (t, v, frame_id). NaN values are stored; the renderer
    skips them on draw.

    max_capacity is the ring-trim threshold: a full buffer drops its oldest
    half, bounding memory for multi-hour live scans at the cost of in-memory
    pan-back. None = UNBOUNDED (grow-only from a small initial size) for the
    bulk loaders (past scan, DB-tail window, CSV), which load a finite,
    already-known result set and must never drop their own rows.

    Bounded buffers pre-allocate at the cap: mid-scan np.resize grows (alloc
    + copy) stalled the SDK parser thread until its data_queue overflowed
    and USB chunks were dropped (surfacing as CRC mismatches). ~1.44 MB per
    buffer × ~32 active buffers ≈ 46 MB is a fine, constant cost."""

    __slots__ = ("t", "v", "frame_id", "n", "dropped_at",
                 "ring_trimmed", "_max_capacity", "_lock")

    def __init__(self, initial_capacity: Optional[int] = None,
                 max_capacity: Optional[int] = _MAX_CAPACITY) -> None:
        self._max_capacity = None if max_capacity is None else max(2, int(max_capacity))
        if initial_capacity is None:
            initial_capacity = self._max_capacity or 4096
        self.t = np.empty(initial_capacity, dtype=np.float64)
        self.v = np.empty(initial_capacity, dtype=np.float32)
        self.frame_id = np.empty(initial_capacity, dtype=np.int64)
        self.n = 0
        # Guards n + the arrays against a torn read: the SDK pipeline thread
        # appends (and _ring_trim shifts data in place, rewriting n) while the
        # GUI thread reads. RLock because window_decimated calls
        # window_indices. Held only for short numpy ops.
        self._lock = threading.RLock()
        self.dropped_at: Optional[float] = None
        # True once _ring_trim has dropped samples, i.e. data below t[0] now
        # lives only in the DB. A pan below t[0] of an untrimmed buffer is
        # simply before the scan start.
        self.ring_trimmed: bool = False

    def _grow(self) -> None:
        """Double all three arrays (capped at max_capacity when bounded).
        The single place that keeps them the same length."""
        new_cap = self.t.shape[0] * 2
        if self._max_capacity is not None:
            new_cap = min(new_cap, self._max_capacity)
        self.t = np.resize(self.t, new_cap)
        self.v = np.resize(self.v, new_cap)
        self.frame_id = np.resize(self.frame_id, new_cap)

    def _ring_trim(self) -> None:
        """Keep only the most recent max_capacity // 2 samples, in place
        (works for an odd max_capacity too). Caller holds _lock."""
        half = self._max_capacity // 2
        for arr in (self.t, self.v, self.frame_id):
            arr[:half] = arr[self.n - half:self.n]
        self.n = half
        self.ring_trimmed = True

    def append(self, t: float, v: float, frame_id: int) -> None:
        """Append one sample. On overflow: ring-trim when already at
        max_capacity, otherwise double the capacity."""
        with self._lock:
            capacity = self.t.shape[0]
            if self.n >= capacity:
                if self._max_capacity is not None and capacity >= self._max_capacity:
                    self._ring_trim()
                else:
                    self._grow()
            idx = self.n
            self.t[idx] = t
            self.v[idx] = v
            self.frame_id[idx] = frame_id
            self.n += 1

    def window_indices(self, t_lo: float, t_hi: float) -> tuple[int, int]:
        """Return (i_lo, i_hi), i_hi exclusive, such that t[i_lo:i_hi] covers
        [t_lo, t_hi]. Binary search.

        Precondition: t[0:n] is non-decreasing (the live pipeline appends in
        timestamp order; the DB query is ordered by timestamp_s). An
        out-of-order append silently yields wrong indices."""
        with self._lock:
            if self.n == 0:
                return 0, 0
            t_slice = self.t[: self.n]
            i_lo = int(np.searchsorted(t_slice, t_lo, side="left"))
            i_hi = int(np.searchsorted(t_slice, t_hi, side="right"))
            return i_lo, i_hi

    def window_decimated(self, t_lo: float, t_hi: float,
                         max_points: int) -> tuple[np.ndarray, np.ndarray]:
        """Return fresh (t, v) arrays covering [t_lo, t_hi] at no more than
        max_points samples; empty when the window holds no samples.

        Thread-safe: the whole read runs under _lock and the results are
        copies, so a concurrent append/_ring_trim can't shift data under the
        caller.

        Decimation is stride-aligned causal mean-binning. Each output sits at
        an ABSOLUTE sample index that is a multiple of `stride` and averages
        the finite samples of [index - stride + 1, index], so an output never
        changes once its sample has arrived. Plain stride subsampling made
        the trace alternate between odd and even samples as the window
        scrolled ("jumping between two datasets"). A bin with no finite
        sample is NaN (the renderer skips it).

        The stride comes from the TIME window (× nominal 40 Hz), not from the
        sample count: a count-keyed stride crept up as samples accumulated at
        a fixed zoom, and each change relocated every output (a visible
        "morph" of the trace). The averaging window equals the stride, the
        Nyquist minimum: an earlier 3× kernel hid features even at the
        tightest 5 s zoom."""
        with self._lock:
            i_lo, i_hi = self.window_indices(t_lo, t_hi)
            if i_hi <= i_lo:
                return self.t[0:0], self.v[0:0]

            expected_samples = max(1.0, (t_hi - t_lo) * _NOMINAL_HZ)
            stride = max(1, int(-(-expected_samples // max_points)))
            if stride == 1:
                # Every sample fits: return them raw (no forced averaging,
                # which read as "looks filtered" at 5 s zoom).
                return self.t[i_lo:i_hi].copy(), self.v[i_lo:i_hi].copy()

            # Stride-aligned absolute indices that fall within [i_lo, i_hi).
            first_k = (i_lo + stride - 1) // stride
            last_k = (i_hi - 1) // stride
            if last_k < first_k:
                return self.t[0:0], self.v[0:0]
            abs_idxs = np.arange(first_k, last_k + 1) * stride

            # Prefix sums over the causal lookback of the leftmost output
            # through the rightmost output (inclusive).
            cum_lo = max(0, int(abs_idxs[0]) - stride + 1)
            v_block = self.v[cum_lo:int(abs_idxs[-1]) + 1]
            f_block = np.isfinite(v_block)
            v_cum = np.empty(v_block.size + 1, dtype=np.float64)
            v_cum[0] = 0.0
            np.cumsum(np.where(f_block, v_block, 0.0).astype(np.float64), out=v_cum[1:])
            n_cum = np.empty(v_block.size + 1, dtype=np.int64)
            n_cum[0] = 0
            np.cumsum(f_block, out=n_cum[1:], dtype=np.int64)

            # Per-output causal window in v_block-local indices.
            hi_b = abs_idxs - cum_lo + 1
            lo_b = np.maximum(0, hi_b - stride)
            counts = n_cum[hi_b] - n_cum[lo_b]
            sums = v_cum[hi_b] - v_cum[lo_b]
            with np.errstate(invalid="ignore", divide="ignore"):
                v_out = np.where(counts > 0, sums / np.maximum(counts, 1), np.nan)
            return self.t[abs_idxs], v_out.astype(np.float32)

    def value_near(self, t: float) -> float:
        """Value of the sample nearest to time `t`; NaN when the buffer is
        empty or that sample is non-finite. Under the lock: a torn read of
        `n` (GUI hover vs pipeline append/ring-trim) caused IndexErrors."""
        with self._lock:
            n = self.n
            if n == 0:
                return math.nan
            t_slice = self.t[:n]
            idx = int(np.searchsorted(t_slice, t, side="left"))
            if idx >= n:
                idx = n - 1
            elif idx > 0 and abs(t_slice[idx - 1] - t) < abs(t_slice[idx] - t):
                idx -= 1
            v = float(self.v[idx])
            return v if np.isfinite(v) else math.nan

    def last_t(self) -> float:
        """Newest timestamp, or NaN when empty. Under the lock (see value_near)."""
        with self._lock:
            return float(self.t[self.n - 1]) if self.n else math.nan

    def mark_dropped(self, t: float) -> None:
        """Record the first dropout timestamp; later calls don't overwrite it."""
        if self.dropped_at is None:
            self.dropped_at = float(t)


def _get_or_create(buffers: dict, key: tuple, max_capacity: Optional[int]) -> _CameraBuffer:
    """Fetch-or-create buffers[key] with the given ring-trim cap (None =
    unbounded, what every bulk loader must use)."""
    buf = buffers.get(key)
    if buf is None:
        buf = buffers[key] = _CameraBuffer(max_capacity=max_capacity)
    return buf


def _points(buf: Optional[_CameraBuffer], t_lo: float, t_hi: float, max_points: int) -> list:
    """[[t, v], ...] for QML from one buffer's decimated window; [] if no buffer."""
    if buf is None:
        return []
    t_arr, v_arr = buf.window_decimated(t_lo, t_hi, int(max_points))
    return [[float(t), float(v)] for t, v in zip(t_arr, v_arr)]


class ScanDataSource(QObject):
    """Base for live and past scan sources. Owns the per-(side, cam, metric)
    _CameraBuffers and the throttled samplesAppended signal; subclasses
    populate the buffers."""

    # (side, cam_id, metric, added_count), coalesced across the flush window.
    samplesAppended = pyqtSignal(str, int, str, int)

    def __init__(self, plot_t0: float, parent: Optional[QObject] = None,
                 buffer_max_capacity: Optional[int] = _MAX_CAPACITY) -> None:
        super().__init__(parent)
        self.plot_t0 = float(plot_t0)
        self._live: bool = False
        # Camera masks this source represents (bit c set ⇒ camera c recorded)
        # and its display mode (0 per-camera / 1 clinical). -1 = unknown, the
        # live default: the viewer then follows the operator's live config.
        # PastScanSource sets what the replayed scan recorded (issue #175).
        self._left_mask: int = -1
        self._right_mask: int = -1
        self._clinical_mode: int = -1
        # Identity for the viewer's "Viewing" badge (issue #245); past scans only.
        self._user_label: str = ""
        self._date_time: str = ""
        self.buffers: dict[tuple[str, int, str], _CameraBuffer] = {}
        # Ring-trim cap for buffers this source creates (see _CameraBuffer).
        self._buffer_max_capacity = buffer_max_capacity
        # (side, cam, metric) -> samples added since the last flush.
        self._pending: dict[tuple[str, int, str], int] = {}
        self._released = False
        # Tests can call _flush() directly and ignore the timer.
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(_FLUSH_INTERVAL_MS)
        self._flush_timer.timeout.connect(self._flush)
        self._flush_timer.start()

    def release(self) -> None:
        """Stop the flush timer. Idempotent.

        Called by the connector right before deleteLater() when QML can no
        longer reach this source. Buffers stay readable (a late paint during
        the deleteLater window must not crash) but no further
        samplesAppended fires."""
        if self._released:
            return
        self._released = True
        self._flush_timer.stop()
        self._pending.clear()

    # These must be pyqtProperties: a plain Python attribute reads as
    # `undefined` in QML (which silently broke the toolbar's source-type label
    # and the "Back to live" variant, both branching on `scanSource.live`).
    # constant=True: set once at construction.
    #   live          True for LiveScanSource, False for PastScanSource.
    #   leftMask /    Camera masks, or -1 if unknown. PlotViewer prefers a real
    #   rightMask     mask (>= 0) over the live selection (issue #175).
    #   clinicalMode  -1 unknown / 0 per-camera / 1 clinical
    #                 (PlotViewer.effectiveClinical).
    #   userLabel     Operator label for the "Viewing" badge (#245), or "".
    #   dateTime      Scan date/time ``YYYY-MM-DD HH:MM:SS`` like the History
    #                 list, or ""; the viewer trims it to minutes.
    live = pyqtProperty(bool, lambda self: self._live, constant=True)
    leftMask = pyqtProperty(int, lambda self: self._left_mask, constant=True)
    rightMask = pyqtProperty(int, lambda self: self._right_mask, constant=True)
    clinicalMode = pyqtProperty(int, lambda self: self._clinical_mode, constant=True)
    userLabel = pyqtProperty(str, lambda self: self._user_label, constant=True)
    dateTime = pyqtProperty(str, lambda self: self._date_time, constant=True)

    @pyqtProperty(float)
    def liveEdge(self) -> float:
        """The maximum timestamp across all buffers, or 0.0 if empty.

        No notify signal: PlotCell repaints on samplesAppended and reads
        liveEdge fresh inside onPaint (as a plain Python property it would be
        `undefined` there and NaN-poison the paint math)."""
        edge = 0.0
        for b in self.buffers.values():
            if b.n > 0:
                edge = max(edge, float(b.t[b.n - 1]))
        return edge

    def get_or_create_buffer(self, side: str, cam_id: int, metric: str) -> _CameraBuffer:
        """The (side, cam_id, metric) buffer, created with this source's cap."""
        return _get_or_create(self.buffers, (side, int(cam_id), metric),
                              self._buffer_max_capacity)

    def _cam_buffers(self, side: str, cam_id: int) -> list:
        """The existing plotted-metric buffers of one (side, cam)."""
        found = (self.buffers.get((side, int(cam_id), metric)) for metric in _METRICS)
        return [buf for buf in found if buf is not None]

    @pyqtSlot(str, int, str, float, float, int, result="QVariantList")
    def points_for_window(self, side: str, cam_id: int, metric: str,
                          t_lo: float, t_hi: float, max_points: int) -> list:
        """QML-facing: a fresh list of [t, v] pairs for the (side, cam_id,
        metric) buffer over [t_lo, t_hi], decimated to at most max_points.
        [] when the buffer doesn't exist or the window is empty."""
        return _points(self.buffers.get((side, int(cam_id), metric)), t_lo, t_hi, max_points)

    @pyqtSlot(str, int, str, float, result=float)
    def value_at(self, side: str, cam_id: int, metric: str, t: float) -> float:
        """Value of the sample nearest to time `t` for the hover tooltip; NaN
        if the buffer doesn't exist, is empty, or that sample is non-finite."""
        buf = self.buffers.get((side, int(cam_id), metric))
        return math.nan if buf is None else buf.value_near(t)

    @pyqtSlot(str, int, result=float)
    def dropped_at_for(self, side: str, cam_id: int) -> float:
        """Dropout timestamp for (side, cam_id), or NaN if the camera hasn't
        been flagged as dropped this scan. PlotCell draws a vertical bar
        there; any one of the camera's metric buffers carries it."""
        for buf in self._cam_buffers(side, cam_id):
            if buf.dropped_at is not None:
                return float(buf.dropped_at)
        return math.nan

    def last_sample_t(self, side: str, cam_id: int) -> float:
        """Newest sample timestamp across this (side, cam)'s metric buffers,
        or NaN when the camera has no samples yet.

        This is where the camera's trace visibly ends. The dropout watchdog
        anchors the plot's dropout marker here so it lives on the SAME time
        axis as the samples (SDK-normalized, t=0 at the scan's first frame);
        on any other clock the marker renders clipped or at the wrong x
        (issue #284)."""
        latest = math.nan
        for buf in self._cam_buffers(side, cam_id):
            t = buf.last_t()
            if math.isnan(latest) or t > latest:
                latest = t
        return latest

    @pyqtSlot(str, result="QVariantMap")
    @pyqtSlot(str, float, float, float, result="QVariantMap")
    def compute_bounds_for_metric(self, metric: str, percentile_lo: float = 2.0,
                                  percentile_hi: float = 98.0, pad_frac: float = 0.25) -> dict:
        """Padded percentile bounds {"yMin": ..., "yMax": ...} over the finite
        values of every (side, cam_id) buffer of `metric` (2%/98% + 25%
        padding, carried over from the legacy plot's _boundsFromArray).
        Neutral {0.0, 1.0} when fewer than 4 finite samples exist (too noisy
        to autoscale)."""
        chunks = [self._finite_values(buf) for key, buf in self.buffers.items()
                  if key[2] == metric]
        return self._padded_percentile_bounds(chunks, percentile_lo, percentile_hi, pad_frac)

    @pyqtSlot(str, int, str, result="QVariantMap")
    def compute_bounds_for_cell(self, side: str, cam_id: int, metric: str) -> dict:
        """Per-plot autoscale (#452): compute_bounds_for_metric's default
        bounds over ONE (side, cam_id) buffer, same neutral fallback."""
        buf = self.buffers.get((side, int(cam_id), metric))
        chunks = [] if buf is None else [self._finite_values(buf)]
        return self._padded_percentile_bounds(chunks, 2.0, 98.0, 0.25)

    @staticmethod
    def _finite_values(buf: "_CameraBuffer") -> np.ndarray:
        slice_v = buf.v[: buf.n]
        return slice_v[np.isfinite(slice_v)]

    @staticmethod
    def _padded_percentile_bounds(chunks: list, percentile_lo: float,
                                  percentile_hi: float, pad_frac: float) -> dict:
        """Shared bounds math. Stays in numpy throughout: a Python list from
        `.tolist()` was the dominant cost of these slots at 1 Hz."""
        if sum(c.size for c in chunks) < 4:
            return {"yMin": 0.0, "yMax": 1.0}
        chunks = [c for c in chunks if c.size]
        combined = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
        lo = float(np.percentile(combined, percentile_lo))
        hi = float(np.percentile(combined, percentile_hi))
        if lo == hi:
            lo -= 0.5
            hi += 0.5
        pad = (hi - lo) * pad_frac
        return {"yMin": lo - pad, "yMax": hi + pad}

    def note_dirty(self, side: str, cam_id: int, metric: str, added: int) -> None:
        """Record that `added` new samples landed in (side, cam, metric);
        coalesces with earlier notes until the next flush."""
        if added > 0:
            key = (side, int(cam_id), metric)
            self._pending[key] = self._pending.get(key, 0) + added

    def _flush(self) -> None:
        """Emit one samplesAppended per dirty buffer and clear pending."""
        pending, self._pending = self._pending, {}
        for (side, cam_id, metric), count in pending.items():
            self.samplesAppended.emit(side, cam_id, metric, count)


class LiveScanSource(ScanDataSource):
    """ScanDataSource fed by the in-flight pipeline, one per scan.

    Bounded memory + DB tail: the in-memory buffers ring-trim at
    cache_max_samples, dropping the oldest half. Older samples remain in the
    scan DB (the SDK's ScanDBSink writes them as the scan runs). When the
    viewer pans BEFORE the in-memory window, points_for_window / value_at
    lazily load that range from the DB into a transient window on a worker
    thread (issue #256). Until the load lands, reads serve the in-memory
    portion plus whatever the previous cached window covers; then
    historyWindowLoaded triggers a repaint.

    scan_db_path=None (unit tests) disables the DB tail."""

    # Emitted from the loader thread (PyQt queues it to the GUI thread) when
    # a DB-window load lands. PlotViewer marks itself dirty on it so the
    # history paints even when no live samples tick the paint throttle.
    historyWindowLoaded = pyqtSignal()

    def __init__(self, plot_t0: float, parent: Optional[QObject] = None,
                 scan_db_path: Optional[str] = None,
                 cache_max_samples: int = _MAX_CAPACITY,
                 bvi_lpf_cutoff_hz: float = 0.0) -> None:
        super().__init__(plot_t0=plot_t0, parent=parent, buffer_max_capacity=cache_max_samples)
        self._live = True
        # BVI display low-pass (issue #228), state per (side, cam_id) stream
        # (cam_id -1 = clinical side average). Default 0.0 = OFF keeps raw
        # storage for direct constructions (tests); the connector passes
        # effective_bvi_lpf_cutoff(...).
        self._bvi_lpf_prev: dict[tuple[str, int], float] = {}
        self.set_bvi_lpf_cutoff_hz(bvi_lpf_cutoff_hz)
        # DB tail state, lazily initialized on the first pan into the past.
        self._scan_db_path = scan_db_path
        self._db = None                       # omotion.ScanDatabase read handle
        self._db_session_id: Optional[int] = None
        self._db_unavailable = False          # set True only on HARD failure
        self._scan_label: Optional[str] = None
        self._db_window_buffers: dict = {}    # transient (side, cam, metric) -> buffer
        self._db_window_lo = float("inf")     # loaded range (nothing loaded yet)
        self._db_window_hi = float("-inf")
        # _db_load_lock guards the window bounds/buffers install and the
        # thread handle. One load in flight at a time.
        self._db_load_lock = threading.Lock()
        self._db_load_thread: Optional[threading.Thread] = None
        # Test hook: False runs loads inline so assertions are deterministic.
        self._db_load_async = True

    def release(self) -> None:
        """Base release plus DB-tail teardown, so a retired live source can't
        hold the scan DB open for the app's lifetime."""
        if self._released:
            return
        super().release()
        db, self._db = self._db, None
        self._db_unavailable = True  # never reopen after release
        self._db_window_buffers = {}
        if db is not None:
            try:
                db.close()
            except Exception:
                logger.warning("scan DB close failed on release", exc_info=True)

    def set_scan_label(self, label: Optional[str]) -> None:
        """Bind this source to its scan-DB session by exact label
        (``f"{scan_id}_{subject_id}"``), so the DB tail never picks another
        scan's session. Called once, right after the scan starts."""
        if label:
            self._scan_label = str(label)

    # ── DB tail (lazy) ──────────────────────────────────────────────────────

    def _db_ready(self) -> bool:
        """Open the read-only DB handle and resolve this scan's session_id on
        first use (only reached after a ring-trim, when the session row
        exists).

        _db_unavailable (permanent) is reserved for HARD failures: no DB path,
        or an exception. A not-yet-visible session row is a SOFT miss: the
        handle stays open and a later paint retries."""
        if self._db_unavailable:
            return False
        if self._db is not None and self._db_session_id is not None:
            return True
        if not self._scan_db_path:
            self._db_unavailable = True
            return False
        try:
            if self._db is None:
                from omotion.ScanDatabase import ScanDatabase
                self._db = ScanDatabase(db_path=self._scan_db_path)
            sid = self._resolve_session_id()
            if sid is None:
                return False
            self._db_session_id = sid
            logger.info("LiveScanSource: DB tail engaged (session_id=%d, label=%r)",
                        sid, self._scan_label)
            return True
        except Exception:
            logger.exception("LiveScanSource: DB tail unavailable")
            self._db_unavailable = True
            return False

    def _resolve_session_id(self) -> Optional[int]:
        """This scan's session_id, or None if its row isn't visible yet. Exact
        label match when a label was bound; otherwise the newest session.
        Raises only on a real DB error (handled by _db_ready)."""
        sql, args = "SELECT id FROM sessions ORDER BY id DESC LIMIT 1", ()
        if self._scan_label:
            sql = ("SELECT id FROM sessions WHERE session_label = ? "
                   "ORDER BY session_start DESC, id DESC LIMIT 1")
            args = (self._scan_label,)
        row = next(self._db._connection().execute(sql, args), None)
        return None if row is None else int(row[0])

    def _ensure_db_window(self, t_lo: float, t_hi: float) -> None:
        """Schedule an ASYNC load when the transient DB window doesn't cover
        [t_lo, t_hi]. Never blocks the GUI thread on the query + bucketize: a
        synchronous load froze the app for seconds per zoom/pan step on long
        All-camera scans (issue #256: 16 cams × 40 Hz ≈ 640 rows/s, ~10⁵ rows
        per padded 60 s window).

        The load is padded by one window each side so small pans reuse it.
        Its upper end is capped at liveEdge so the window never claims to
        cover rows that don't exist yet (false coverage made the staleness
        check serve a stale window while live data piled up unshown).

        t_lo is clamped to >= 0 BEFORE the staleness check: no rows exist
        below 0 and the stored window start is floored at 0, so an unclamped
        negative request (followLive window wider than the elapsed scan)
        could never be satisfied by the cache and re-queried the DB on every
        points_for_window call (issue #151)."""
        t_lo = max(0.0, float(t_lo))
        t_hi = max(t_lo, float(t_hi))
        with self._db_load_lock:
            if t_lo >= self._db_window_lo and t_hi <= self._db_window_hi:
                return
            loader_busy = (self._db_load_thread is not None
                           and self._db_load_thread.is_alive())
        if loader_busy:
            # If this request is still uncovered when the in-flight load
            # lands, the next paint re-issues it: no queue needed.
            return
        span = max(1.0, t_hi - t_lo)
        want_lo = max(0.0, t_lo - span)
        want_hi = t_hi + span
        edge = self.liveEdge
        if edge > 0.0:
            want_hi = min(want_hi, edge)
        if not self._db_load_async:
            self._db_window_load(want_lo, want_hi)
            return
        thread = threading.Thread(target=self._db_window_load, args=(want_lo, want_hi),
                                  name="plot-db-window-load", daemon=True)
        with self._db_load_lock:
            self._db_load_thread = thread
        thread.start()

    def _db_window_load(self, want_lo: float, want_hi: float) -> None:
        """Loader body (worker thread; inline when _db_load_async is False):
        query + bucketize the range, install it under the lock, ping the
        viewer. Sharing the sqlite handle with the GUI thread is safe: it's
        opened check_same_thread=False, the GUI only uses it for the one-shot
        session-id resolve in _db_ready (before any load is scheduled), and
        loads are serialized."""
        try:
            buffers, _ = _bucketize_session_rows(
                self._db, self._db_session_id, t_lo=want_lo, t_hi=want_hi)
        except Exception:
            # The previous window stays in place; a later paint retries.
            if self._released:
                # release() closed the DB under an in-flight load.
                logger.debug("LiveScanSource: DB window load aborted by release",
                             exc_info=True)
            else:
                logger.exception("LiveScanSource: DB window load failed")
            return
        if self._released:
            return
        with self._db_load_lock:
            self._db_window_buffers = buffers
            self._db_window_lo = want_lo
            self._db_window_hi = want_hi
        self.historyWindowLoaded.emit()

    def _needs_db_tail(self, side: str, cam_id: int, metric: str, t_lo: float) -> bool:
        """True iff t_lo is below the oldest IN-MEMORY sample AND that buffer
        has ring-trimmed (otherwise t_lo is just before the scan start).
        Hot-path guard: pre-trim it is False without touching the DB, so
        nothing is opened or queried per paint until a scan outgrows the
        in-memory window."""
        buf = self.buffers.get((side, int(cam_id), metric))
        if buf is None or buf.n == 0 or not buf.ring_trimmed:
            return False
        return t_lo < float(buf.t[0])

    @pyqtSlot(str, int, str, float, float, int, result="QVariantList")
    def points_for_window(self, side: str, cam_id: int, metric: str,
                          t_lo: float, t_hi: float, max_points: int) -> list:
        """Base behaviour, plus the DB tail for the part of the window that
        has been ring-trimmed out of memory."""
        key = (side, int(cam_id), metric)
        buf = self.buffers.get(key)
        if not self._needs_db_tail(side, cam_id, metric, t_lo) or not self._db_ready():
            return _points(buf, t_lo, t_hi, max_points)

        # Straddling window: split at the oldest in-memory sample and stitch.
        # [t_lo, split] comes from the DB tail (static history), [split, t_hi]
        # from memory (fresh every paint). Serving the recent part from the
        # cached DB window would freeze the live trace until its next reload.
        t_lo, t_hi = float(t_lo), float(t_hi)
        split = float(buf.t[0])  # _needs_db_tail guarantees a non-empty buffer
        db_hi = min(split, t_hi)
        mem_lo = max(split, t_lo)

        # Proportional point budgets keep the trace density (stride)
        # consistent across the seam.
        total_span = max(1e-9, t_hi - t_lo)
        db_span = max(0.0, db_hi - t_lo)
        mem_span = max(0.0, t_hi - mem_lo)
        db_max = max(1, int(round(int(max_points) * db_span / total_span)))
        mem_max = max(1, int(round(int(max_points) * mem_span / total_span)))

        db_pts: list = []
        if db_span > 0.0:
            # Non-blocking; until a scheduled load lands this renders whatever
            # overlap the previous cached window still has (possibly nothing).
            self._ensure_db_window(t_lo, db_hi)
            db_pts = _points(self._db_window_buffers.get(key), t_lo, db_hi, db_max)
        mem_pts = _points(buf, mem_lo, t_hi, mem_max) if mem_span > 0.0 else []
        return db_pts + mem_pts

    @pyqtSlot(str, int, str, float, result=float)
    def value_at(self, side: str, cam_id: int, metric: str, t: float) -> float:
        """Base behaviour, reading the DB tail for times ring-trimmed out of memory."""
        if not self._needs_db_tail(side, cam_id, metric, t) or not self._db_ready():
            return super(LiveScanSource, self).value_at(side, cam_id, metric, t)
        self._ensure_db_window(t, t)
        buf = self._db_window_buffers.get((side, int(cam_id), metric))
        return math.nan if buf is None else buf.value_near(t)

    def append_uncorrected(self, side: str, cam_id: int, frame_id: int, t: float, bfi: float,
                           bvi: float, mean: Optional[float] = None,
                           contrast: Optional[float] = None, temp: Optional[float] = None) -> None:
        """Append one frame's worth of uncorrected metrics.

        bfi and bvi are always appended (NaN included); bvi passes through
        the display low-pass first (see _bvi_lpf; issue #228). mean, contrast
        and temp are appended only when non-None: _LivePlotSink passes None
        for a non-finite mean_dc_rt / contrast_sn_rt, and None temp for dark
        frames (whose camera-temp reading is meaningless)."""
        self._append_one(side, cam_id, "bfi", frame_id, t, bfi)
        self._append_one(side, cam_id, "bvi", frame_id, t, self._bvi_lpf(side, cam_id, bvi))
        if mean is not None:
            self._append_one(side, cam_id, "mean", frame_id, t, mean)
        if contrast is not None:
            self._append_one(side, cam_id, "contrast", frame_id, t, contrast)
        if temp is not None:
            self._append_one(side, cam_id, "temp", frame_id, t, temp)

    def mark_dropped(self, side: str, cam_id: int, t: float) -> None:
        """Record a dropout timestamp on every existing metric buffer of this
        (side, cam). Idempotent per _CameraBuffer.mark_dropped."""
        for buf in self._cam_buffers(side, cam_id):
            buf.mark_dropped(t)

    @property
    def bvi_lpf_cutoff_hz(self) -> float:
        """Current display low-pass cutoff in Hz (0.0 = off)."""
        return self._bvi_lpf_cutoff_hz

    def set_bvi_lpf_cutoff_hz(self, cutoff_hz: float) -> None:
        """Re-target the display low-pass mid-scan (#552: the Settings switch
        applies immediately). ``<= 0`` turns it off. Samples already buffered
        keep their filtering; the state is cleared so the next finite sample
        re-seeds the filter instead of continuing from an output computed
        under the old cutoff.

        Called from the GUI thread while the sink thread may be inside
        _bvi_lpf: the float store and the dict clear are each atomic under
        the GIL, so the worst case is one sample seeded a frame early."""
        self._bvi_lpf_cutoff_hz = float(cutoff_hz)
        self._bvi_lpf_alpha = bvi_lpf_alpha(self._bvi_lpf_cutoff_hz)
        self._bvi_lpf_prev.clear()

    def _bvi_lpf(self, side: str, cam_id: int, bvi: float) -> float:
        """1-pole IIR low-pass on the DISPLAY BVI stream (issue #228):
        ``y[n] = y[n-1] + alpha * (x[n] - y[n-1])``, seeded by the first
        finite sample (no ramp-in from zero). Non-finite input (NaN side
        averages while sensors are covered) is returned as-is and leaves the
        state untouched, so the filter resumes from the last finite output.
        alpha >= 1 (filter off) returns the raw value."""
        alpha = self._bvi_lpf_alpha
        if alpha >= 1.0 or not math.isfinite(bvi):
            return bvi
        key = (side, int(cam_id))
        prev = self._bvi_lpf_prev.get(key)
        out = bvi if prev is None else prev + alpha * (bvi - prev)
        self._bvi_lpf_prev[key] = out
        return out

    def _append_one(self, side: str, cam_id: int, metric: str,
                    frame_id: int, t: float, v: float) -> None:
        self.get_or_create_buffer(side, cam_id, metric).append(t=t, v=v, frame_id=frame_id)
        self.note_dirty(side, cam_id, metric, added=1)


def _bucketize_session_rows(scan_db, session_id: int, t_lo: Optional[float] = None,
                            t_hi: Optional[float] = None) -> tuple[dict, bool]:
    """Query a session's session_data rows (optionally a time slice) into a
    fresh {(side, cam_id, metric): _CameraBuffer} dict of unbounded buffers.
    Returns (buffers, has_bfi). Shared by the past-scan load and the live
    DB-tail window.

    Rows are bucketed by their stored cam_id verbatim: 0..7 are per-camera
    streams (normal-mode scans); -1 is the clinical-mode dark-corrected
    per-side average, persisted by ScanDBSink from the cam_id=-1 frames the
    SDK's SideAverageStage routes onto the "final" channel. has_bfi is True
    when any BFI/BVI value landed (either layout)."""
    buffers: dict = {}
    has_bfi = False
    for row in scan_db.iter_session_data(session_id, t_lo=t_lo, t_hi=t_hi):
        side = _SIDE_INT_TO_STR.get(int(row["side"]))
        if side is None:
            continue
        cam_id = int(row["cam_id"])
        frame_id = int(row["frame_id"])
        t = float(row["timestamp_s"])
        for metric in _METRICS:
            value = row.get(metric)
            if value is None:
                continue
            if metric in ("bfi", "bvi"):
                has_bfi = True
            _get_or_create(buffers, (side, cam_id, metric), None).append(
                t=t, v=float(value), frame_id=frame_id)
    return buffers, has_bfi


def _load_corrected_csv_into(buffers: dict, csv_path: str) -> None:
    """Load the corrected CSV the SDK's CsvSink writes for every scan (not
    gated by writeRawCsv) into `buffers`. Two layouts (docs/csv-formats.md):

    - Per-camera: frame_id, timestamp_s, then bfi/bvi/mean/contrast (and
      ignored temp) columns ``<metric>_l1..l8`` / ``_r1..r8``. Columns are
      1-indexed; each populates (side, cam_id=0..7, metric).
    - Clinical (6 cols): frame_id, timestamp_s, bfi_left, bfi_right,
      bvi_left, bvi_right; each populates (side, cam_id=-1, metric).

    Empty and unparsable cells are skipped. Best-effort: a missing or
    malformed file just means the viewer shows whatever else is in
    `buffers`."""
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            if "bfi_l1" in fieldnames:
                columns = [(f"{metric}_{prefix}{cam + 1}", (side, cam, metric))
                           for metric in _METRICS for side, prefix in _SIDES
                           for cam in range(8)]
            elif "bfi_left" in fieldnames:
                columns = [(f"{metric}_{side}", (side, -1, metric))
                           for metric in ("bfi", "bvi") for side, _ in _SIDES]
            else:
                return
            for row in reader:
                try:
                    frame_id = int(row.get("frame_id", -1))
                    t = float(row["timestamp_s"])
                except (ValueError, KeyError, TypeError):
                    continue
                for col, key in columns:
                    raw = row.get(col, "")
                    if raw is None or raw == "":
                        continue
                    try:
                        v = float(raw)
                    except ValueError:
                        continue
                    _get_or_create(buffers, key, None).append(t=t, v=v, frame_id=frame_id)
    except OSError:
        # Best-effort, but never silent: the viewer will show less data.
        logger.warning("corrected-CSV fallback load failed: %s", csv_path, exc_info=True)


def buffers_are_empty(buffers: Optional[dict]) -> bool:
    """True when a loaded past-scan buffer dict holds no samples at all.

    A scan interrupted before any dark interval closed yields a session row
    but zero session_data and no corrected CSV. The viewer uses this to show
    a 'no data' error instead of silently opening an empty plot."""
    return not buffers or all(getattr(b, "n", 0) == 0 for b in buffers.values())


def _derive_masks_from_buffers(buffers: dict) -> tuple[int, int]:
    """Reconstruct (left_mask, right_mask) from the per-camera streams a
    loaded scan carries: bit c set ⇒ camera c (cam_id 0..7) has a buffer.

    (-1, -1) when there are no per-camera streams (e.g. a clinical scan with
    only the cam_id=-1 side average) so the viewer falls back to its live
    masks rather than an empty grid. A side that recorded nothing yields 0
    as long as the other side has cameras, preserving a one-sided scan's
    true configuration (issue #175)."""
    masks = {"left": 0, "right": 0}
    for side, cam_id, _metric in buffers:
        if side in masks and 0 <= cam_id < 8:
            masks[side] |= (1 << cam_id)
    if not any(masks.values()):
        return -1, -1
    return masks["left"], masks["right"]


def _derive_clinical_from_buffers(buffers) -> int:
    """Tri-state recorded display mode from a loaded scan's cam_ids: 0 when
    it has per-camera streams (cam_id 0..7), 1 when it has only the clinical
    side average (cam_id -1), -1 unknown (no samples)."""
    if buffers_are_empty(buffers):
        return -1
    cam_ids = {key[1] for key in buffers}
    if any(0 <= cam_id < 8 for cam_id in cam_ids):
        return 0
    return 1 if -1 in cam_ids else -1


def _masks_from_flags(flags) -> Optional[tuple[int, int]]:
    """(left_mask, right_mask) from a scan's stored ``session_meta.sdk_flags``
    (written by the SDK's ScanDBSink at scan start), or None when `flags`
    isn't a dict, a mask is missing or not an int (a bool is not a mask), or
    BOTH are 0 (no real scan configures zero cameras)."""
    if not isinstance(flags, dict):
        return None
    masks = (flags.get("left_camera_mask"), flags.get("right_camera_mask"))
    if any(isinstance(m, bool) or not isinstance(m, int) for m in masks):
        return None
    left, right = masks[0] & 0xFF, masks[1] & 0xFF
    return (left, right) if left or right else None


def _clinical_from_flags(flags) -> Optional[int]:
    """Recorded display mode (0 per-camera / 1 clinical) from a scan's stored
    ``session_meta.sdk_flags.reduced_mode``, or None when absent."""
    if not isinstance(flags, dict) or flags.get("reduced_mode") is None:
        return None
    return 1 if flags["reduced_mode"] else 0


def load_past_scan_buffers(scan_db, session_id: int,
                           corrected_csv_path: Optional[str] = None) -> tuple[dict, bool]:
    """Bulk-load a past scan's samples into a plain
    {(side, cam_id, metric): _CameraBuffer} dict. Returns (buffers, has_bfi).

    This is the HEAVY part of opening a past scan: a multi-hour scan is
    millions of session_data rows (issue #152 measured ~8.3M samples ≈ 20 s).
    Deliberately Qt-free so callers can run it on a worker thread and hand
    the dict to PastScanSource(preloaded_buffers=...) on the GUI thread.

    See _bucketize_session_rows for the layout. Pre-pipeline scans carry no
    BFI/BVI in the DB; the corrected CSV covers those."""
    buffers, has_bfi = _bucketize_session_rows(scan_db, int(session_id))
    if not has_bfi and corrected_csv_path:
        _load_corrected_csv_into(buffers, corrected_csv_path)
    return buffers, has_bfi


def load_csv_scan_buffers(csv_path: str) -> dict:
    """Bulk-load a scan-export CSV into a fresh buffers dict, DB-free.

    The History → Export CSV / SDK ``materialize_corrected_csv`` output is
    the per-camera layout ``_load_corrected_csv_into`` parses (trailing
    temp/quality columns are ignored). Feeds a ``PastScanSource`` straight
    from a CSV, e.g. the bundled sample scan shown when no device is
    connected at boot (#314).

    Fail-soft: a missing or unreadable file yields an empty dict; callers
    check ``buffers_are_empty`` before displaying."""
    buffers: dict = {}
    _load_corrected_csv_into(buffers, csv_path)
    return buffers


class PastScanSource(ScanDataSource):
    """ScanDataSource for a finished scan, in the same buffer layout as
    LiveScanSource. Its buffers are unbounded: a ring-trim cap would drop
    the scan's own oldest rows as a long scan loaded.

    Pass `preloaded_buffers` (built off-thread via load_past_scan_buffers)
    to skip the synchronous DB/CSV walk: constructing the QObject is cheap
    and stays on the GUI thread (issue #152). Without it, `scan_db`
    (omotion.ScanDatabase, duck-typed for tests) is read synchronously."""

    def __init__(self, scan_db, session_id: int, corrected_csv_path: Optional[str] = None,
                 parent: Optional[QObject] = None, preloaded_buffers: Optional[dict] = None,
                 recorded_flags: Optional[dict] = None,
                 user_label: str = "", date_time: str = "") -> None:
        super().__init__(plot_t0=0.0, parent=parent, buffer_max_capacity=None)
        self.session_id = int(session_id)
        # Coerced so a stray None from the meta lookup can't reach QML.
        self._user_label = str(user_label or "")
        self._date_time = str(date_time or "")
        if preloaded_buffers is not None:
            self.buffers = preloaded_buffers
        else:
            self.buffers, _ = load_past_scan_buffers(scan_db, self.session_id, corrected_csv_path)

        # Lay the grid out from THIS scan's configuration, not the operator's
        # live selection (issue #175). Prefer the config the scan was RUN
        # with (session_meta.sdk_flags, threaded in as recorded_flags) over
        # what its data implies: an All/All scan where only the middle
        # cameras produced rows would otherwise render a Middle grid, hiding
        # the configured-but-empty cameras (issue #175 reopen). Scans whose
        # meta predates the flags fall back to the data-derived values.
        self._left_mask, self._right_mask = (
            _masks_from_flags(recorded_flags) or _derive_masks_from_buffers(self.buffers))
        self._clinical_mode = _clinical_from_flags(recorded_flags)
        if self._clinical_mode is None:
            self._clinical_mode = _derive_clinical_from_buffers(self.buffers)
