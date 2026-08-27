"""Unit tests for disconnect-time notification hygiene (issue #489).

What this exercises
-------------------
A system-wide disconnect must surface at most one card per device (the 3
``disconnect_*`` toasts) — or, mid-scan, the one E-303/E-304 critical
modal — never a wall of per-camera "Camera X connection lost" cards mixed
in. Three seams:

  - ``MotionConnector._on_dropout_check`` — once a scan-ending abort has
    been notified (``_scan_abort_notified``), the 1 Hz watchdog must stop
    emitting per-camera dropout toasts: the critical modal is the
    notification, and the scan is already tearing down. Without this gate
    a whole-system unplug detected slower than the 2 s per-camera silence
    threshold fired up to 16 camera cards on top of the modal.

  - ``MotionConnector._dismiss_dropout_toasts`` — the shared dismissal
    helper. Side-scoped, it clears a sensor's 8 possible per-camera tags
    (used on that sensor's USB disconnect, where the device-level card
    subsumes its cameras' cards — including a card lingering from a scan
    that already ended). Un-scoped, it clears exactly the cameras the
    current scan flagged dropped (used by the E-303/E-304 abort paths;
    those call sites are asserted in test_scan_stall_watchdog.py and
    test_scan_disconnect_abort.py).

Marker
------
``pytest.mark.unit`` — pure Python, no QApplication, no hardware; methods
are invoked unbound on stub ``self`` objects (same pattern as the other
connector-seam unit tests).

Run with:  pytest tests/test_disconnect_notifications.py -v
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import motion_connector  # noqa: E402
from motion_connector import MotionConnector  # noqa: E402

pytestmark = pytest.mark.unit


class _Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


# ── _on_dropout_check bails once a scan abort has been notified ─────────


def _watchdog_self(*, abort_notified, last_seen, toasts, aborts):
    """Stub `self` carrying exactly the state _on_dropout_check reads."""
    return SimpleNamespace(
        _capture_running=True,
        _trigger_state="ON",
        _scan_abort_notified=abort_notified,
        _camera_dropout_threshold_sec=2.0,
        _camera_last_seen=dict(last_seen),
        _camera_dropped=set(),
        _camera_last_temp={},
        _current_scan_source=None,
        _scan_elapsed_str=lambda: "00:00:13",
        notify=lambda *a, **k: toasts.append((a, k)),
        cameraDropoutDetected=_Signal(),
        _scan_data_stall_timeout_sec=15.0,
        _trigger_on_mono=0.0,
        _abort_scan_data_stall=lambda stalled_s: aborts.append(stalled_s),
    )


def test_watchdog_silent_after_scan_abort_notified(monkeypatch):
    """After E-303/E-304 fired, silent cameras must produce NO new
    per-camera toasts, dropout marks, or stall re-aborts."""
    toasts, aborts = [], []
    # Every camera ancient-silent — would flood 4 toasts without the gate.
    fake = _watchdog_self(
        abort_notified=True,
        last_seen={("left", i): 0.0 for i in range(2)}
        | {("right", i): 0.0 for i in range(2)},
        toasts=toasts, aborts=aborts,
    )
    monkeypatch.setattr(motion_connector.time, "monotonic", lambda: 100.0)

    MotionConnector._on_dropout_check(fake)

    assert toasts == []
    assert fake._camera_dropped == set()
    assert fake.cameraDropoutDetected.calls == []
    assert aborts == []


def test_watchdog_still_fires_during_live_scan(monkeypatch):
    """Control: with no abort notified, a silent camera still gets its
    per-camera dropout toast (the fail-soft path is unchanged)."""
    toasts, aborts = [], []
    fake = _watchdog_self(
        abort_notified=False,
        # LEFT 1 silent for 5 s; RIGHT 1 streaming (keeps the stall
        # decision far from tripping).
        last_seen={("left", 0): 95.0, ("right", 0): 100.0},
        toasts=toasts, aborts=aborts,
    )
    monkeypatch.setattr(motion_connector.time, "monotonic", lambda: 100.0)

    MotionConnector._on_dropout_check(fake)

    assert fake._camera_dropped == {("left", 0)}
    assert len(toasts) == 1
    args, kwargs = toasts[0]
    assert "Camera LEFT 1" in args[0]
    assert kwargs.get("tag") == "dropout_left_0"
    assert aborts == []


# ── _dismiss_dropout_toasts ─────────────────────────────────────────────


class _FakeDismisser:
    def __init__(self, dropped=()):
        self._camera_dropped = set(dropped)
        self.notificationDismissByTagRequested = _Signal()

    @property
    def dismissed(self):
        return [c[0] for c in self.notificationDismissByTagRequested.calls]


def test_dismiss_by_side_clears_all_eight_camera_tags():
    """Side-scoped dismissal covers every possible camera tag for that
    sensor — not just the current scan's `_camera_dropped` — so a card
    lingering from an already-finished scan is cleared too."""
    fake = _FakeDismisser(dropped={("left", 2)})
    MotionConnector._dismiss_dropout_toasts(fake, side="right")
    assert fake.dismissed == [f"dropout_right_{i}" for i in range(8)]


def test_dismiss_without_side_clears_flagged_cameras_only():
    fake = _FakeDismisser(dropped={("left", 2), ("right", 7)})
    MotionConnector._dismiss_dropout_toasts(fake)
    assert sorted(fake.dismissed) == ["dropout_left_2", "dropout_right_7"]


def test_dismiss_without_side_no_flagged_cameras_is_a_noop():
    fake = _FakeDismisser()
    MotionConnector._dismiss_dropout_toasts(fake)
    assert fake.dismissed == []


# ── _surface_device_loss — the first-loss gate ──────────────────────────
#
# After a real unplug the SDK monitor keeps retrying the absent device,
# and every failed retry ends in another CONNECTING -> DISCONNECTED
# transition (bench trace 2026-08-27: repeats at ~2 s intervals, four in
# a row for the console). Only the FIRST loss since the device last
# reached CONNECTED may notify — the repeats re-fired the disconnect
# toast, visibly recreating the card and resetting its clock.


class _FakeLossSurfacer:
    _dismiss_dropout_toasts = MotionConnector._dismiss_dropout_toasts

    def __init__(self):
        self._camera_dropped = set()
        self.notificationDismissByTagRequested = _Signal()
        self.surfaced = []

    def _surface_disconnect(self, name):
        self.surfaced.append(name)


def test_first_sensor_loss_dismisses_camera_cards_and_surfaces():
    fake = _FakeLossSurfacer()
    MotionConnector._surface_device_loss(fake, "left", True, "usb_io_error")
    assert fake.surfaced == ["left"]
    tags = [c[0] for c in fake.notificationDismissByTagRequested.calls]
    assert tags == [f"dropout_left_{i}" for i in range(8)]


def test_first_console_loss_surfaces_without_camera_dismissals():
    # The console has no cameras — nothing to dismiss.
    fake = _FakeLossSurfacer()
    MotionConnector._surface_device_loss(fake, "console", True,
                                         "usb_io_error")
    assert fake.surfaced == ["console"]
    assert fake.notificationDismissByTagRequested.calls == []


def test_repeat_loss_is_suppressed():
    fake = _FakeLossSurfacer()
    MotionConnector._surface_device_loss(
        fake, "console", False, "connect_retry_exhausted"
    )
    assert fake.surfaced == []
    assert fake.notificationDismissByTagRequested.calls == []
