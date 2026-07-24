"""
Unit tests for the mid-scan device-disconnect abort (issue #387).

What this exercises
-------------------
When a device participating in a running scan (the console, or a sensor
whose camera mask was non-zero at scan start) is physically unplugged, the
app must raise the big critical-error modal with code E-304 — not merely the
end-of-scan toast the SDK's own disconnect teardown would leave behind.

Three seams:

  - ``_should_abort_on_disconnect`` — the pure decision the connection
    handler consults: abort only when a scan is capturing AND the dropped
    device is part of THIS scan. An idle / masked-out sensor being
    unplugged must NOT abort a good scan.

  - ``MotionConnector._abort_scan_device_disconnect`` — the abort path,
    twin of ``_abort_scan_data_stall``: one-shot guard shared with the
    stall watchdog (so a disconnect + a stall can't stack two modals),
    capture-log line, E-304 critical modal, then stopCapture().

  - ``_emit_or_defer_scan_notes`` / ``criticalErrorsDismissed`` — the
    notes-modal deferral: the notes modal opens seconds after the abort
    (once the scan finishes unwinding), so it must be held back until the
    critical-error modal is dismissed, keeping the error modal on top.

Marker
------
``pytest.mark.unit`` — pure Python, no QApplication, no hardware; the
conftest autouse fixtures short-circuit on this marker.

Run with:  pytest tests/test_scan_disconnect_abort.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from motion_connector import (  # noqa: E402
    MotionConnector,
    _should_abort_on_disconnect,
)

pytestmark = pytest.mark.unit


# ── _should_abort_on_disconnect ─────────────────────────────────────────


def test_console_loss_while_capturing_aborts():
    # The console always participates — it drives the trigger/laser.
    assert _should_abort_on_disconnect(
        name="console", capture_running=True,
        active_sides={"console", "left"},
    ) is True


def test_participating_sensor_loss_while_capturing_aborts():
    assert _should_abort_on_disconnect(
        name="right", capture_running=True,
        active_sides={"console", "left", "right"},
    ) is True


def test_idle_sensor_loss_does_not_abort():
    """A connected-but-masked-out sensor (not part of this scan) being
    unplugged must NOT abort a scan running on the other side."""
    assert _should_abort_on_disconnect(
        name="right", capture_running=True,
        active_sides={"console", "left"},
    ) is False


def test_disconnect_while_not_capturing_does_not_abort():
    """Idle (not-scanning) unplug stays low-key — no modal."""
    assert _should_abort_on_disconnect(
        name="console", capture_running=False,
        active_sides={"console", "left"},
    ) is False


# ── MotionConnector._abort_scan_device_disconnect (unbound, fake) ───────


class _Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class _FakeConnector:
    """Just the attributes _abort_scan_device_disconnect touches."""

    def __init__(self):
        self._scan_abort_notified = False
        self.captureLog = _Signal()
        self.criticals = []
        self.stop_calls = 0

    def _scan_elapsed_str(self):
        return "00:02:05"

    def _raise_critical(self, code, detail=""):
        self.criticals.append((code, detail))

    def stopCapture(self):
        self.stop_calls += 1


def test_abort_raises_e304_and_stops_capture():
    fake = _FakeConnector()
    MotionConnector._abort_scan_device_disconnect(fake, "right")

    assert fake._scan_abort_notified is True
    assert fake.stop_calls == 1
    # E-304 critical modal, detail names the device + scan-elapsed.
    assert len(fake.criticals) == 1
    code, detail = fake.criticals[0]
    assert code == "E-304"
    assert "right" in detail
    assert "00:02:05" in detail
    # Capture-log line names the disconnected device and the elapsed stamp.
    assert len(fake.captureLog.calls) == 1
    (msg,) = fake.captureLog.calls[0]
    assert "right" in msg
    assert "[00:02:05]" in msg
    assert "disconnected" in msg.lower()


def test_abort_is_one_shot_shared_with_stall_guard():
    """If a scan-ending abort was already notified (e.g. the stall watchdog
    fired E-303 first, or a prior disconnect), a later disconnect must not
    stack a second modal."""
    fake = _FakeConnector()
    fake._scan_abort_notified = True

    MotionConnector._abort_scan_device_disconnect(fake, "console")

    assert fake.criticals == []
    assert fake.stop_calls == 0
    assert fake.captureLog.calls == []


# ── Notes-modal deferral (unbound, fake) ────────────────────────────────


class _FakeTimer:
    def __init__(self, active=False):
        self._active = active
        self.stopped = 0

    def isActive(self):
        return self._active

    def stop(self):
        self.stopped += 1


class _FakeNotesConnector:
    """Attributes the notes-deferral seams touch."""

    def __init__(self):
        self._critical_modal_active = False
        self._scan_notes_pending = False
        self.scanNotesReady = _Signal()
        # LED bits criticalErrorsDismissed also touches — inert here.
        self._error_led_timer = _FakeTimer(active=False)
        self._error_led_on = False
        self.led_writes = []

    def _set_console_led(self, state):
        self.led_writes.append(state)
        return True


def test_notes_emit_immediately_when_no_modal_active():
    fake = _FakeNotesConnector()
    MotionConnector._emit_or_defer_scan_notes(fake)

    assert len(fake.scanNotesReady.calls) == 1
    assert fake._scan_notes_pending is False


def test_notes_deferred_while_critical_modal_active():
    fake = _FakeNotesConnector()
    fake._critical_modal_active = True

    MotionConnector._emit_or_defer_scan_notes(fake)

    # Held back — the error modal must stay on top of the notes modal.
    assert fake.scanNotesReady.calls == []
    assert fake._scan_notes_pending is True


def test_dismiss_flushes_deferred_notes():
    fake = _FakeNotesConnector()
    fake._critical_modal_active = True
    fake._scan_notes_pending = True

    MotionConnector.criticalErrorsDismissed(fake)

    assert fake._critical_modal_active is False
    assert fake._scan_notes_pending is False
    assert len(fake.scanNotesReady.calls) == 1


def test_dismiss_without_pending_notes_does_not_emit():
    fake = _FakeNotesConnector()
    fake._critical_modal_active = True

    MotionConnector.criticalErrorsDismissed(fake)

    assert fake._critical_modal_active is False
    assert fake.scanNotesReady.calls == []
