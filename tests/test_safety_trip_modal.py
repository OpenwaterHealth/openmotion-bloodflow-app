"""
Unit tests for the laser-safety trip modal (issue #431).

A Safety FPGA fault reported by the console telemetry must raise a blocking
critical-error modal whenever it happens, not just a toast:

  - during a scan  → ``safetyTripDuringCaptureRequested`` (E-202 + scan
    cancel, handled by ``_on_safety_trip_during_capture``);
  - any other time → E-203 directly.

Either way it fires once per trip (on the transition into failure), so the
1 Hz telemetry poll can't stack modals while the fault stays latched.

Runs the real ``MotionConnector.readSafetyStatus`` against a fake ``self``
— no QApplication, no hardware.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from motion_connector import (  # noqa: E402
    SAFETY_UNKNOWN_STREAK_THRESHOLD,
    MotionConnector,
)

pytestmark = pytest.mark.unit


class _Signal:
    def __init__(self):
        self.count = 0

    def emit(self):
        self.count += 1


class _FakeConnector:
    readSafetyStatus = MotionConnector.readSafetyStatus
    _surface_safety_trip = MotionConnector._surface_safety_trip
    POST_FLASH_LATCH_BITS = MotionConnector.POST_FLASH_LATCH_BITS
    POWER_CYCLE_CLEAR_AFTER_S = MotionConnector.POWER_CYCLE_CLEAR_AFTER_S
    POWER_CYCLE_TOAST_TAG = MotionConnector.POWER_CYCLE_TOAST_TAG
    _is_expected_post_flash_latch = MotionConnector._is_expected_post_flash_latch
    _maybe_finish_console_power_cycle = MotionConnector._maybe_finish_console_power_cycle
    _set_console_power_cycle_pending = MotionConnector._set_console_power_cycle_pending
    _notify_console_power_cycle = MotionConnector._notify_console_power_cycle
    _mark_console_flashed = MotionConnector._mark_console_flashed

    def __init__(self, capture_running=False, cancel_scheduled=False):
        self._app_config = {}
        self._console_flash_done_at = None
        self.consolePowerCycleChanged = _Signal()
        self.notices = []
        self.dismissed = []
        self.saves = 0
        self._safetyFailure = False
        self._safety_unknown_streak = 0
        self._console_connected_at = None
        self._capture_running = capture_running
        self._safety_cancel_scheduled = cancel_scheduled
        self._laserOn = True
        self.laserStateChanged = _Signal()
        self.safetyTripDuringCaptureRequested = _Signal()
        self.criticals = []
        self.toasts = []
        self.trigger_stops = 0

    @property
    def safetyFailure(self):
        return self._safetyFailure

    @safetyFailure.setter
    def safetyFailure(self, value):
        self._safetyFailure = value

    def _raise_critical(self, code, detail=""):
        self.criticals.append((code, detail))

    def _fire_safety_notification(self, fault_detail=""):
        self.toasts.append(fault_detail)

    def stopTrigger(self):
        self.trigger_stops += 1

    def notify(self, text, type_="info", duration_ms=4000, dismissible=True, tag=""):
        self.notices.append((tag, type_, text))

    def dismissNotification(self, tag):
        self.dismissed.append(tag)

    def _save_app_config(self):
        self.saves += 1


def _fault():
    # 0x01 on the EE monitor: one fault bit set.
    return SimpleNamespace(safety_known=True, safety_ok=False,
                           safety_se=0x01, safety_so=0x00)


def _clear():
    return SimpleNamespace(safety_known=True, safety_ok=True,
                           safety_se=0x00, safety_so=0x00)


def _unknown():
    return SimpleNamespace(safety_known=False, safety_ok=True,
                           safety_se=0x00, safety_so=0x00)


def test_idle_trip_raises_e203_modal():
    c = _FakeConnector()
    c.readSafetyStatus(_fault())
    assert [code for code, _ in c.criticals] == ["E-203"]
    assert "safety_se=0x01" in c.criticals[0][1]
    assert c.safetyTripDuringCaptureRequested.count == 0
    # The persistent toast and the laser shutdown still happen.
    assert len(c.toasts) == 1
    assert c.trigger_stops == 1
    assert c._laserOn is False


def test_latched_fault_does_not_restack_the_modal():
    c = _FakeConnector()
    for _ in range(5):
        c.readSafetyStatus(_fault())
    assert [code for code, _ in c.criticals] == ["E-203"]


def test_trip_after_recovery_raises_again():
    c = _FakeConnector()
    c.readSafetyStatus(_fault())
    c.readSafetyStatus(_clear())
    assert c.safetyFailure is False
    c.readSafetyStatus(_fault())
    assert [code for code, _ in c.criticals] == ["E-203", "E-203"]


def test_trip_during_scan_goes_through_capture_path_not_e203():
    c = _FakeConnector(capture_running=True)
    c.readSafetyStatus(_fault())
    assert c.safetyTripDuringCaptureRequested.count == 1
    assert c.criticals == []  # E-202 is raised by the queued capture slot


def test_trip_during_scan_already_cancelling_raises_nothing_more():
    c = _FakeConnector(capture_running=True, cancel_scheduled=True)
    c.readSafetyStatus(_fault())
    assert c.safetyTripDuringCaptureRequested.count == 0
    assert c.criticals == []


def test_unresponsive_monitor_still_raises_e201_only():
    c = _FakeConnector()
    for _ in range(SAFETY_UNKNOWN_STREAK_THRESHOLD):
        c.readSafetyStatus(_unknown())
    assert [code for code, _ in c.criticals] == ["E-201"]


# ── After a console firmware update (#767) ────────────────────────────────

def _latch(se=0x04, so=0x04):
    return SimpleNamespace(safety_known=True, safety_ok=False,
                           safety_se=se, safety_so=so)


def _flashed(monkeypatch, connected_ago=1.0):
    import motion_connector as mc
    now = [1000.0]
    monkeypatch.setattr(mc.time, "monotonic", lambda: now[0])
    c = _FakeConnector()
    c._console_connected_at = now[0] - connected_ago
    c._mark_console_flashed()
    return c, now


def test_flash_marks_power_cycle_pending_and_asks_for_it(monkeypatch):
    c, _ = _flashed(monkeypatch)
    assert c._app_config["consolePowerCyclePending"] is True
    assert c.saves == 1 and c.consolePowerCycleChanged.count == 1
    assert [t for t, _, _ in c.notices] == ["console_power_cycle"]
    assert "power-cycle the console" in c.notices[0][2].lower()


def test_post_flash_rate_latch_asks_for_power_cycle_not_e203(monkeypatch):
    """The bench case: RATE_LOWER_LIMIT on both monitors ~1 s after the
    console comes back from its own update."""
    c, _ = _flashed(monkeypatch)
    c.readSafetyStatus(_latch())
    assert c.criticals == [], "no E-203 for the latch our own flash causes"
    assert c.toasts == [], "no 'contact support' laser-safety toast"
    # Still shut down and still blocking scans until the power cycle.
    assert c.safetyFailure is True
    assert c.trigger_stops == 1 and c._laserOn is False
    assert c.notices[-1][0] == "console_power_cycle"


def test_other_fault_bits_after_flash_still_raise_e203(monkeypatch):
    c, _ = _flashed(monkeypatch)
    c.readSafetyStatus(_latch(se=0x04 | 0x01, so=0x04))
    assert [code for code, _ in c.criticals] == ["E-203"]
    assert len(c.toasts) == 1


def test_rate_latch_without_a_pending_flash_is_still_e203():
    c = _FakeConnector()
    c.readSafetyStatus(_latch())
    assert [code for code, _ in c.criticals] == ["E-203"]


def test_clear_interlock_after_power_cycle_finishes_it(monkeypatch):
    c, now = _flashed(monkeypatch)
    c.readSafetyStatus(_latch())
    # Power cycle: console reconnects, interlock reads clear.
    now[0] += 30.0
    c._console_connected_at = now[0]
    c.readSafetyStatus(_clear())
    assert c.safetyFailure is False
    assert c._app_config["consolePowerCyclePending"] is True, (
        "a clear read right after reconnect is not yet proof")
    now[0] += c.POWER_CYCLE_CLEAR_AFTER_S
    c.readSafetyStatus(_clear())
    assert c._app_config["consolePowerCyclePending"] is False
    assert "console_power_cycle" in c.dismissed
    assert c.criticals == []


def test_clear_read_right_after_flash_does_not_end_pending(monkeypatch):
    """The latch shows ~1 s after reconnect; a clear poll before it must
    not end the pending state and let the latch become an E-203."""
    c, now = _flashed(monkeypatch, connected_ago=30.0)
    c.readSafetyStatus(_clear())
    now[0] += 1.0
    c.readSafetyStatus(_latch())
    assert c.criticals == []
