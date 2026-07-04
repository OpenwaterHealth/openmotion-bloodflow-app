"""Pulse-view glue: the pipeline sink and the no-hardware demo driver.

Kept out of ``motion_connector.py`` (already ~4,700 lines) — the connector just
owns instances of these and forwards their output to QML via ``pulseSnapshot``.

* ``PulseSink`` — subscribes to the SDK pipeline's ``"pulse"`` channel and
  forwards each ``PulseAnalysis`` to an ``emit_fn`` callback (the connector
  wires this to the ``pulseSnapshot`` signal). Decoupled from the connector so
  it is trivially unit-testable.
* ``PulseDemoController`` — streams the SDK's synthetic BFI generator through
  the SAME ``PulseWaveformAnalyzer`` the live pipeline uses and calls the same
  ``emit_fn``. Because the app has no working no-hardware mock mode, this is how
  the pulse view is demonstrated and verified without a sensor attached.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from typing import Callable, Optional, Tuple

import numpy as np
from PyQt6.QtCore import QObject, QTimer

from omotion.pulse.analyzer import PulseWaveformAnalyzer
from omotion.pulse.synth import synth_pair


def load_bfi_results_csv(path: str) -> Tuple[np.ndarray, np.ndarray]:
    """Load a ``*_bfi_results.csv`` (columns camera,side,time_s,BFI,BVI) and
    return per-side, spatially-averaged BFI time series ``(left, right)``.

    Averages across cameras at each timestamp (the reduced-mode side average),
    ordered by time. Used to replay a recorded scan through the pulse view as
    sample data — see PulseDemoController.start_replay."""
    left: dict = defaultdict(list)
    right: dict = defaultdict(list)
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                t = float(row["time_s"])
                v = float(row["BFI"])
            except (KeyError, ValueError, TypeError):
                continue
            if not np.isfinite(v):
                continue
            (left if row.get("side") == "left" else right)[t].append(v)

    def _series(d: dict) -> np.ndarray:
        return np.array([float(np.mean(d[t])) for t in sorted(d)],
                        dtype=np.float64)

    return _series(left), _series(right)


class PulseSink:
    """Pipeline sink for the ``"pulse"`` channel → ``emit_fn(PulseAnalysis)``."""

    channels = {"pulse"}

    def __init__(self, emit_fn: Callable[[object], None]):
        self._emit = emit_fn

    def on_scan_start(self, meta) -> None:  # pipeline lifecycle hook
        pass

    def consume(self, channel: str, payload) -> None:
        if channel == "pulse":
            self._emit(payload)

    def on_complete(self) -> None:
        pass


class PulseDemoController(QObject):
    """QTimer-driven synthetic pulse feed for the no-hardware demo.

    Pre-generates a block of synthetic left/right BFI, then streams it a few
    samples per tick through two ``PulseWaveformAnalyzer`` instances, emitting a
    fresh snapshot per side each tick. Blocks regenerate (new seed) as they are
    consumed so the demo runs indefinitely. Timestamps come from an internal
    clock, so the effective heart rate is exactly the requested ``bpm``
    regardless of real timer jitter.
    """

    _BLOCK_S = 60.0

    def __init__(self, emit_fn: Callable[[object], None], *, fs: float = 40.0,
                 interval_ms: int = 50, step: Optional[int] = None, parent=None):
        super().__init__(parent)
        self._emit = emit_fn
        self._fs = float(fs)
        self._dt = 1.0 / self._fs
        self._step = int(step) if step else max(1, round(interval_ms / 1000.0 * fs))
        self._timer = QTimer(self)
        self._timer.setInterval(int(interval_ms))
        self._timer.timeout.connect(self._tick)
        self._left = PulseWaveformAnalyzer(side="left")
        self._right = PulseWaveformAnalyzer(side="right")
        self._running = False
        self._clock = 0.0
        self._i = 0
        self._seed = 0
        self._bpm = 72.0
        self._preset = "normal"
        self._right_amp_ratio = 1.0
        self._right_shape: Optional[object] = None
        self._mode = "synth"          # "synth" (regenerate) | "replay" (loop)
        self._lv = np.empty(0, dtype=np.float64)
        self._rv = np.empty(0, dtype=np.float64)

    @property
    def running(self) -> bool:
        return self._running

    @property
    def mode(self) -> str:
        """'synth' (generated) or 'replay' (recorded sample data)."""
        return self._mode

    def start(self, preset: str = "normal", bpm: float = 72.0,
              right_amp_ratio: float = 0.75, right_shape=None) -> None:
        """(Re)start the demo. ``right_amp_ratio`` < 1 gives the right side a
        smaller pulse so the L-vs-R comparison panel shows a real difference."""
        self._mode = "synth"
        self._preset = preset
        self._bpm = float(bpm)
        self._right_amp_ratio = float(right_amp_ratio)
        self._right_shape = right_shape
        self._left.reset()
        self._right.reset()
        self._clock = 0.0
        self._i = 0
        self._seed = 0
        self._gen_block()
        self._running = True
        if not self._timer.isActive():
            self._timer.start()

    def start_replay(self, left: np.ndarray, right: np.ndarray) -> None:
        """(Re)start the feed replaying recorded per-side BFI (e.g. from
        ``load_bfi_results_csv``), looping. Both sides are truncated to a common
        length and streamed at ``fs`` through the same analyzers the live path
        uses, so the pulse view shows real recorded pulses as sample data."""
        left = np.asarray(left, dtype=np.float64).ravel()
        right = np.asarray(right, dtype=np.float64).ravel()
        n = min(left.size, right.size)
        if n < 2:
            raise ValueError("replay data too short")
        self._mode = "replay"
        self._lv = left[:n]
        self._rv = right[:n]
        self._left.reset()
        self._right.reset()
        self._clock = 0.0
        self._i = 0
        self._running = True
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._running = False
        self._timer.stop()

    def _gen_block(self) -> None:
        _, lv, rv = synth_pair(
            duration_s=self._BLOCK_S, fs=self._fs, bpm=self._bpm,
            pulse_shape=self._preset, right_amp_ratio=self._right_amp_ratio,
            right_shape=self._right_shape, seed=self._seed,
        )
        self._lv = lv
        self._rv = rv
        self._i = 0

    def _tick(self) -> None:
        if not self._running or self._lv.size == 0:
            return
        step = self._step
        if self._i + step > self._lv.size:
            if self._mode == "replay":
                self._i = 0                 # loop the recording
            else:
                self._seed += 1
                self._gen_block()
        end = self._i + step
        lv = self._lv[self._i:end]
        rv = self._rv[self._i:end]
        n = lv.size
        ts = self._clock + np.arange(n) * self._dt
        self._left.add_samples(ts, lv)
        self._right.add_samples(ts, rv)
        self._clock += n * self._dt
        self._i = end
        self._emit(self._left.snapshot())
        self._emit(self._right.snapshot())
