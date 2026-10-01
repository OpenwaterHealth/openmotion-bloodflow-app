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

    def __init__(self, capture_running=False, cancel_scheduled=False):
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
