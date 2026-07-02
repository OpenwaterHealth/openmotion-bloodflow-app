"""Unit tests for the camera-dropout plot marker's time axis (issue #284).

What this exercises
-------------------
``MotionConnector._on_dropout_check`` — the 1 Hz watchdog — feeds the
LiveScanSource a dropout timestamp that PlotCell.qml renders as a
vertical red bar. The samples in that source carry SDK-normalized
timestamps (t = 0 at the FIRST FRAME of the scan; see the SDK's
``_BaseSource._t0_normalize``), so the marker must live on that same
axis.

The bug: the watchdog recorded ``time.monotonic() - self._plot_t0``,
where ``_plot_t0`` is captured at the top of ``startCapture`` — seconds
of sensor init / trigger setup BEFORE the first frame arrives. The
marker therefore landed ahead of the source's live edge: PlotCell's
``dropT <= tHi`` window check clipped it invisible while following
live, and once the live edge finally caught up the bar drew at an x
offset by the whole setup latency (or never, if every camera had
stopped and the live edge froze).

The fix anchors the marker at the dropped camera's own newest sample —
exactly where its trace visibly stops.

Marker
------
``pytest.mark.unit`` — pure Python, no QApplication, no hardware. The
watchdog method is invoked unbound on a stub ``self`` (same pattern as
the other connector-seam unit tests).

Run with:  pytest tests/test_dropout_marker_time_axis.py -v
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import motion_connector  # noqa: E402
from data_sources import LiveScanSource  # noqa: E402
from motion_connector import MotionConnector  # noqa: E402

pytestmark = pytest.mark.unit


class _Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


def _watchdog_self(src, *, plot_t0, last_seen, threshold=2.0):
    """Stub `self` carrying exactly the state _on_dropout_check reads."""
    return SimpleNamespace(
        _capture_running=True,
        _trigger_state="ON",
        _camera_dropout_threshold_sec=threshold,
        _camera_last_seen=dict(last_seen),
        _camera_dropped=set(),
        _camera_last_temp={},
        _plot_t0=plot_t0,
        _current_scan_source=src,
        _scan_elapsed_str=lambda: "00:00:13",
        notify=lambda *a, **k: None,
        cameraDropoutDetected=_Signal(),
        # All-camera stall watchdog (issue #248) — armed but far from
        # tripping; these tests exercise the per-camera marker path.
        _scan_stall_abort_fired=False,
        _scan_data_stall_timeout_sec=15.0,
        _trigger_on_mono=plot_t0,
    )


def test_dropout_marker_lands_on_sample_time_axis(monkeypatch):
    """Timeline (monotonic seconds):

      1000.0  startCapture         → plot_t0
      1008.0  first frame arrives  → SDK-normalized sample t = 0.0
      1018.0  LEFT 1's last frame  → sample t = 10.0, then silence
      1021.0  watchdog fires (3 s > 2 s threshold); LEFT 2 still
              streaming, its newest sample t = 13.0 (= liveEdge)

    The marker must land at sample-t 10.0 — the end of LEFT 1's trace —
    not at 1021.0 - 1000.0 = 21.0, which is past the live edge (13.0)
    and therefore never renders while the viewer follows live.
    """
    src = LiveScanSource(plot_t0=1000.0)
    for i in range(401):  # LEFT 1: t = 0.0 .. 10.0
        src.append_uncorrected(side="left", cam_id=0, frame_id=i,
                               t=i * 0.025, bfi=1.0, bvi=2.0)
    for i in range(521):  # LEFT 2: t = 0.0 .. 13.0 (keeps liveEdge moving)
        src.append_uncorrected(side="left", cam_id=1, frame_id=i,
                               t=i * 0.025, bfi=1.0, bvi=2.0)

    fake = _watchdog_self(
        src,
        plot_t0=1000.0,
        last_seen={("left", 0): 1018.0, ("left", 1): 1021.0},
    )
    monkeypatch.setattr(motion_connector.time, "monotonic", lambda: 1021.0)

    MotionConnector._on_dropout_check(fake)

    # Watchdog fired for the silent camera only.
    assert fake._camera_dropped == {("left", 0)}
    assert fake.cameraDropoutDetected.calls == [("left", 0, "00:00:13")]

    drop_t = src.dropped_at_for("left", 0)
    # The marker must be on the plotted time axis — at or before the
    # newest plotted sample — or PlotCell clips it invisible.
    assert drop_t <= src.liveEdge
    # And specifically at the dropped camera's own trace end.
    assert drop_t == pytest.approx(10.0)


def test_dropout_marker_without_samples_does_not_mark_nan(monkeypatch):
    """A camera that delivered frames (heartbeat alive) but never any
    plottable sample (e.g. covered — NaN BFI gate) has no trace to
    anchor a bar on. The watchdog must still fire the toast/signal, and
    must not poison the source with a NaN/garbage marker."""
    import math

    src = LiveScanSource(plot_t0=1000.0)

    fake = _watchdog_self(
        src,
        plot_t0=1000.0,
        last_seen={("right", 3): 1018.0},
    )
    monkeypatch.setattr(motion_connector.time, "monotonic", lambda: 1021.0)

    MotionConnector._on_dropout_check(fake)

    assert fake._camera_dropped == {("right", 3)}  # detection still fires
    assert math.isnan(src.dropped_at_for("right", 3))  # no bogus bar


def test_dropout_marker_uses_camera_own_trace_end_not_live_edge(monkeypatch):
    """Regression guard for the anchor choice: the bar belongs where THIS
    camera's data stops, not at the global live edge (which other
    cameras keep advancing)."""
    src = LiveScanSource(plot_t0=500.0)
    src.append_uncorrected(side="right", cam_id=6, frame_id=1,
                           t=4.0, bfi=1.0, bvi=2.0)
    src.append_uncorrected(side="right", cam_id=7, frame_id=2,
                           t=9.0, bfi=1.0, bvi=2.0)  # live edge = 9.0

    fake = _watchdog_self(
        src,
        plot_t0=500.0,
        last_seen={("right", 6): 510.0, ("right", 7): 515.0},
    )
    monkeypatch.setattr(motion_connector.time, "monotonic", lambda: 515.0)

    MotionConnector._on_dropout_check(fake)

    assert src.dropped_at_for("right", 6) == pytest.approx(4.0)


def test_last_sample_t_newest_across_metric_buffers():
    """last_sample_t returns the newest timestamp across the (side, cam)
    metric buffers — metrics can trail (e.g. mean absent on some
    samples), and the trace end is the newest of any of them."""
    src = LiveScanSource(plot_t0=0.0)
    src.append_uncorrected(side="left", cam_id=2, frame_id=1, t=1.0,
                           bfi=1.0, bvi=2.0, mean=100.0, contrast=0.3)
    src.append_uncorrected(side="left", cam_id=2, frame_id=2, t=2.0,
                           bfi=1.0, bvi=2.0)  # no mean/contrast this sample
    assert src.last_sample_t("left", 2) == pytest.approx(2.0)


def test_last_sample_t_nan_when_no_samples():
    import math

    src = LiveScanSource(plot_t0=0.0)
    assert math.isnan(src.last_sample_t("left", 0))
