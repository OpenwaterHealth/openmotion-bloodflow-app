"""Issue #585 — a camera that never starts is marked offline.

What this exercises
-------------------
``MotionConnector._on_dropout_check`` (the 1 Hz per-camera dropout
watchdog) walked only ``_camera_last_seen``, which gains an entry on a
camera's FIRST frame. A camera enabled in the scan's mask that never
delivered a single frame therefore never got an entry and was never
marked offline: no toast, no log line, no ``cameraDropoutDetected``, so
the averaged plot (clinical layout, research Average view) had nothing
to show either.

The fix: once the scan is streaming (some camera has delivered, trigger
ON), a mask-enabled camera still without a frame after
``cameraDropoutThresholdSec`` goes through the same offline path as a
mid-scan dropout. A scan where NO camera ever delivers stays with the
E-303 whole-scan stall abort (issue #248) — no per-camera flood.

The average values themselves already exclude such a camera; the last
test pins that for the research-derived average.

Marker
------
``pytest.mark.unit`` — pure Python, no QApplication, no hardware. The
watchdog runs unbound on a stub ``self`` (same pattern as
tests/test_dropout_marker_time_axis.py).
"""

import math
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


def _watchdog_self(*, last_seen, masks, streaming_since=None,
                   trigger_on=0.0, threshold=2.0, src=None):
    toasts = []
    fake = SimpleNamespace(
        _capture_running=True,
        _trigger_state="ON",
        _camera_dropout_threshold_sec=threshold,
        _camera_last_seen=dict(last_seen),
        _camera_dropped=set(),
        _camera_last_temp={},
        _capture_camera_masks=dict(masks),
        _camera_streaming_since=streaming_since,
        _current_scan_source=src,
        _scan_elapsed_str=lambda: "00:00:05",
        notify=lambda text, **k: toasts.append((text, k)),
        cameraDropoutDetected=_Signal(),
        _scan_abort_notified=False,
        _scan_data_stall_timeout_sec=15.0,
        _trigger_on_mono=trigger_on,
        _abort_scan_data_stall=lambda s: None,
        _storage_check_due=lambda: False,
    )
    fake.toasts = toasts
    return fake


def _tick(fake, monkeypatch, now):
    monkeypatch.setattr(motion_connector.time, "monotonic", lambda: now)
    MotionConnector._on_dropout_check(fake)


def test_camera_that_never_delivers_is_marked_offline(monkeypatch):
    """LEFT 1 and LEFT 3 are in the mask; only LEFT 1 ever streams. Once
    the scan has been streaming longer than the threshold, LEFT 3 is
    marked offline through the dropout path: dropped set, signal, toast."""
    fake = _watchdog_self(
        last_seen={("left", 0): 1000.0},
        masks={"left": 0b101, "right": 0},
    )
    _tick(fake, monkeypatch, 1000.0)         # first tick sees streaming
    assert fake._camera_dropped == set()     # not before the threshold
    assert fake.cameraDropoutDetected.calls == []

    fake._camera_last_seen[("left", 0)] = 1003.0
    _tick(fake, monkeypatch, 1003.0)
    assert fake._camera_dropped == {("left", 2)}
    assert fake.cameraDropoutDetected.calls == [("left", 2, "00:00:05")]
    assert len(fake.toasts) == 1
    text, kw = fake.toasts[0]
    assert "LEFT 3" in text and "never" in text.lower()
    assert kw["tag"] == "dropout_left_2"   # dismissed with the other dropouts
    assert kw["type_"] == "warning"


def test_marked_once_not_every_tick(monkeypatch):
    fake = _watchdog_self(
        last_seen={("right", 4): 1000.0},
        masks={"left": 0, "right": 0b0011_0000},
        streaming_since=1000.0,
    )
    for now in (1003.0, 1004.0, 1005.0):
        fake._camera_last_seen[("right", 4)] = now
        _tick(fake, monkeypatch, now)
    assert fake.cameraDropoutDetected.calls == [("right", 5, "00:00:05")]
    assert len(fake.toasts) == 1


def test_cameras_outside_the_mask_are_ignored(monkeypatch):
    fake = _watchdog_self(
        last_seen={("left", 0): 1010.0},
        masks={"left": 0b1, "right": 0},
        streaming_since=1000.0,
    )
    _tick(fake, monkeypatch, 1010.0)
    assert fake._camera_dropped == set()
    assert fake.toasts == []


def test_no_camera_streaming_leaves_it_to_the_stall_abort(monkeypatch):
    """No camera has delivered: the per-camera pass stays silent (E-303
    owns a scan with no data at all) instead of flooding 16 toasts."""
    fake = _watchdog_self(
        last_seen={},
        masks={"left": 0xFF, "right": 0xFF},
        trigger_on=1000.0,
    )
    _tick(fake, monkeypatch, 1010.0)
    assert fake._camera_dropped == set()
    assert fake.toasts == []


def test_clock_starts_at_the_trigger_on_edge(monkeypatch):
    """Frames recorded before a trigger pause do not count toward the
    threshold after the trigger re-opens."""
    fake = _watchdog_self(
        last_seen={("left", 0): 1021.0},
        masks={"left": 0b11, "right": 0},
        streaming_since=1000.0,
        trigger_on=1020.0,
    )
    _tick(fake, monkeypatch, 1021.0)
    assert fake._camera_dropped == set()
    fake._camera_last_seen[("left", 0)] = 1023.0
    _tick(fake, monkeypatch, 1023.0)
    assert fake._camera_dropped == {("left", 1)}


def test_never_started_camera_gets_no_plot_marker(monkeypatch):
    """No trace to anchor a dropout bar on — the source stays unmarked."""
    src = LiveScanSource(plot_t0=0.0)
    fake = _watchdog_self(
        last_seen={("left", 0): 1003.0},
        masks={"left": 0b11, "right": 0},
        streaming_since=1000.0,
        src=src,
    )
    _tick(fake, monkeypatch, 1003.0)
    assert fake._camera_dropped == {("left", 1)}
    assert math.isnan(src.dropped_at_for("left", 1))


def test_late_first_frame_recovers_through_the_rearm_path():
    """A flagged camera that starts late is re-armed like any recovered
    dropout (the sink's existing _rearm_dropped_camera call)."""
    dropped = {("left", 1)}
    msg = motion_connector._rearm_dropped_camera("left", 1, dropped)
    assert msg is not None and dropped == set()


def test_research_side_average_excludes_a_silent_camera():
    """The averaged value is over the cameras that delivered — a camera
    that never sends is not averaged in as zero or NaN."""
    src = LiveScanSource(plot_t0=0.0, derive_side_average=True)
    for fid in (1, 2):
        for cam, bfi in ((0, 4.0), (2, 8.0)):   # cam 1 is silent
            src.append_uncorrected(side="left", cam_id=cam, frame_id=fid,
                                   t=fid * 0.025, bfi=bfi, bvi=1.0)
    src.flush_side_average()
    assert src.value_at("left", -1, "bfi", 0.025) == pytest.approx(6.0)
    assert src.value_at("left", -1, "bfi", 0.050) == pytest.approx(6.0)
