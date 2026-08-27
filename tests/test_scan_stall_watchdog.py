"""
Unit tests for the whole-scan data-stall watchdog (issue #248).

What this exercises
-------------------
Two seams around the E-303 "all cameras lost" abort:

  - ``_scan_data_stall_decision`` — the pure function the 1 Hz dropout
    watchdog consults to decide whether data acquisition has completely
    stopped. It measures silence since the newest frame from ANY camera
    (``_camera_last_seen``), floored at the current trigger-ON edge so a
    scan whose cameras never delivered a single frame still trips.

  - ``MotionConnector._abort_scan_data_stall`` — the main-thread abort
    path: one-shot guard, capture-log line, E-303 critical modal, then
    stopCapture() (which cancels the SDK scan so the pipeline finalizes
    the data files and captureFinished returns the scan flow to idle).
    Called unbound on a fake connector, same pattern as the other
    connector-seam unit tests.

Per-camera dropouts stay fail-soft (the scan keeps running on the
remaining cameras); TOTAL data loss must not be — that's the #248 bug:
the app kept the timer counting on dead air and reported "complete".

Marker
------
``pytest.mark.unit`` — pure Python, no QApplication, no hardware; the
conftest autouse fixtures short-circuit on this marker.

Run with:  pytest tests/test_scan_stall_watchdog.py -v
"""

import sys
from pathlib import Path

import pytest

# Repo root holds motion_connector.py — make it importable without
# turning the project into an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from motion_connector import (  # noqa: E402
    MotionConnector,
    _scan_data_stall_decision,
)

pytestmark = pytest.mark.unit


# ── _scan_data_stall_decision ───────────────────────────────────────────


def test_disabled_timeout_never_trips():
    assert _scan_data_stall_decision(
        now_mono=1000.0, trigger_on_mono=0.0, last_seen={}, timeout_sec=0,
    ) is None
    assert _scan_data_stall_decision(
        now_mono=1000.0, trigger_on_mono=0.0, last_seen={}, timeout_sec=-1,
    ) is None


def test_no_trigger_edge_never_trips():
    """No open trigger-ON interval → no baseline to measure silence
    against (the watchdog is additionally gated on trigger state ON)."""
    assert _scan_data_stall_decision(
        now_mono=1000.0, trigger_on_mono=None, last_seen={}, timeout_sec=15,
    ) is None


def test_recent_frame_from_any_camera_holds_off():
    """One live camera is enough — total loss means EVERY camera silent."""
    last_seen = {
        ("left", 0): 100.0,   # silent for 900 s
        ("right", 3): 995.0,  # 5 s ago — still delivering
    }
    assert _scan_data_stall_decision(
        now_mono=1000.0, trigger_on_mono=50.0,
        last_seen=last_seen, timeout_sec=15,
    ) is None


def test_all_cameras_silent_past_timeout_trips():
    last_seen = {("left", 0): 100.0, ("right", 3): 110.0}
    stalled = _scan_data_stall_decision(
        now_mono=1000.0, trigger_on_mono=50.0,
        last_seen=last_seen, timeout_sec=15,
    )
    # Silence is measured from the NEWEST frame across all cameras.
    assert stalled == pytest.approx(890.0)


def test_silence_at_exactly_timeout_does_not_trip():
    last_seen = {("left", 0): 985.0}
    assert _scan_data_stall_decision(
        now_mono=1000.0, trigger_on_mono=50.0,
        last_seen=last_seen, timeout_sec=15,
    ) is None


def test_no_frames_ever_trips_from_trigger_on_edge():
    """A scan where no camera ever delivered a frame must still abort:
    the trigger-ON edge is the silence baseline when last_seen is empty."""
    stalled = _scan_data_stall_decision(
        now_mono=100.0, trigger_on_mono=50.0, last_seen={}, timeout_sec=15,
    )
    assert stalled == pytest.approx(50.0)
    # ... but not before the timeout has elapsed since trigger-ON.
    assert _scan_data_stall_decision(
        now_mono=60.0, trigger_on_mono=50.0, last_seen={}, timeout_sec=15,
    ) is None


def test_trigger_reopen_restarts_the_clock():
    """A trigger re-ON later than the newest (stale) frame restarts the
    silence measurement — old frames from before a pause don't count
    against the fresh interval."""
    last_seen = {("left", 0): 100.0}
    assert _scan_data_stall_decision(
        now_mono=1000.0, trigger_on_mono=995.0,
        last_seen=last_seen, timeout_sec=15,
    ) is None


# ── MotionConnector._abort_scan_data_stall (unbound, fake connector) ────


class _Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class _FakeConnector:
    """Just the attributes _abort_scan_data_stall touches."""

    # Real per-camera toast-dismissal helper (issue #489) — funnels into the
    # dismissNotification recorder below.
    _dismiss_dropout_toasts = MotionConnector._dismiss_dropout_toasts

    def __init__(self):
        self._scan_abort_notified = False
        self._scan_data_stall_timeout_sec = 15.0
        self._camera_dropped = {("left", 0), ("right", 5)}
        self.captureLog = _Signal()
        self.criticals = []
        self.stop_calls = 0
        self.notificationDismissByTagRequested = _Signal()

    @property
    def dismissed(self):
        return [c[0] for c in self.notificationDismissByTagRequested.calls]

    def _scan_elapsed_str(self):
        return "00:01:23"

    def _raise_critical(self, code, detail=""):
        self.criticals.append((code, detail))

    def stopCapture(self):
        self.stop_calls += 1


def test_abort_raises_e303_and_stops_capture():
    fake = _FakeConnector()
    MotionConnector._abort_scan_data_stall(fake, 16.2)

    assert fake._scan_abort_notified is True
    assert fake.stop_calls == 1
    # The E-303 modal supersedes the per-camera 'connection lost' toasts
    # the watchdog fired earlier in this scan (issue #489).
    assert sorted(fake.dismissed) == ["dropout_left_0", "dropout_right_5"]
    # E-303 critical modal with the stall duration + elapsed in the detail.
    assert len(fake.criticals) == 1
    code, detail = fake.criticals[0]
    assert code == "E-303"
    assert "16 s" in detail
    assert "00:01:23" in detail
    # Capture-log line names the condition and the configured timeout.
    assert len(fake.captureLog.calls) == 1
    (msg,) = fake.captureLog.calls[0]
    assert "No data from any camera" in msg
    assert ">15 s" in msg
    assert "[00:01:23]" in msg
