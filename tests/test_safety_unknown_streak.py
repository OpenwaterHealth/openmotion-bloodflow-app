"""
test_safety_unknown_streak.py — unit test for laser-safety streak gate.

Issue #119 repro
----------------
A single telemetry poll with ``safety_known=False`` (safety I2C reads
returned no data) used to immediately fire the persistent "Laser safety
warning detected" toast. During a USB disconnect cascade the safety
I2C reads can fail for a single poll while the rest of the snapshot
still has ``read_ok=True`` — producing a phantom safety failure that
doesn't correspond to any real safety-chip fault.

The fix is to require a streak of consecutive unresponsive polls before
tripping, gated by ``SAFETY_UNKNOWN_STREAK_THRESHOLD``. The decision is
extracted into ``_safety_unknown_streak_decision`` so it can be tested
without standing up a QObject runtime.

The streak only gates the ``safety_known=False`` (unresponsive) branch.
A real fault with ``safety_known=True, safety_ok=False`` still trips on
the first observation — that path is exercised by the HIL test
``test_force_laser_fail.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from motion_connector import (
    SAFETY_UNKNOWN_STREAK_THRESHOLD,
    _safety_unknown_streak_decision,
)

pytestmark = pytest.mark.unit


def _snap(safety_known: bool, safety_ok: bool = True) -> SimpleNamespace:
    """Build a minimal telemetry snapshot for the streak helper.

    Only the two attributes the helper reads are populated; the helper
    must not touch anything else on the snapshot.
    """
    return SimpleNamespace(safety_known=safety_known, safety_ok=safety_ok)


def test_threshold_is_at_least_two():
    """The whole point of the streak is to ignore one transient miss.

    A threshold of 1 would be functionally identical to the pre-fix
    behaviour and would let issue #119 re-regress silently. Guard the
    constant so a future tweak can't accidentally undo the fix.
    """
    assert SAFETY_UNKNOWN_STREAK_THRESHOLD >= 2


def test_single_unknown_does_not_trip():
    new_streak, should_fire = _safety_unknown_streak_decision(_snap(False), 0)
    assert new_streak == 1
    assert should_fire is False


def test_streak_reaches_threshold_then_trips():
    streak = 0
    fires: list[bool] = []
    for _ in range(SAFETY_UNKNOWN_STREAK_THRESHOLD):
        streak, fire = _safety_unknown_streak_decision(_snap(False), streak)
        fires.append(fire)
    expected = [False] * (SAFETY_UNKNOWN_STREAK_THRESHOLD - 1) + [True]
    assert fires == expected
    assert streak == SAFETY_UNKNOWN_STREAK_THRESHOLD


def test_streak_keeps_firing_past_threshold():
    """Once the streak has tripped, every subsequent unknown poll
    should keep returning ``should_fire=True``. The connector's outer
    ``if not self._safetyFailure`` guard is what stops the toast from
    re-firing every second; the helper itself just reports the state.
    """
    streak = SAFETY_UNKNOWN_STREAK_THRESHOLD
    new_streak, fire = _safety_unknown_streak_decision(_snap(False), streak)
    assert new_streak == SAFETY_UNKNOWN_STREAK_THRESHOLD + 1
    assert fire is True


def test_known_resets_streak():
    streak = SAFETY_UNKNOWN_STREAK_THRESHOLD - 1  # one short of tripping
    new_streak, fire = _safety_unknown_streak_decision(_snap(True), streak)
    assert new_streak == 0
    assert fire is False


def test_intervening_known_prevents_trip():
    """Unknown,unknown,KNOWN,unknown must not trip on the final unknown:
    the known poll in the middle resets the streak. This is the case
    that issue #119 actually hits — the USB transient produces one or
    two unresponsive polls, then telemetry recovers.
    """
    streak = 0
    for snap in (
        _snap(False),
        _snap(False),
        _snap(True),
        _snap(False),
    ):
        streak, fire = _safety_unknown_streak_decision(snap, streak)
    assert streak == 1
    assert fire is False


def test_missing_safety_known_treats_snapshot_as_known():
    """Legacy SDK snapshots without ``safety_known`` are assumed to be
    from the chip-responded path (backward compat with the
    ``getattr(snap, "safety_known", True)`` guard in
    ``readSafetyStatus``). Any accumulated streak must reset so a
    legacy snapshot can't carry a stuck counter.
    """
    snap = SimpleNamespace(safety_ok=True)
    new_streak, fire = _safety_unknown_streak_decision(snap, 5)
    assert new_streak == 0
    assert fire is False


def test_custom_threshold_argument():
    """Threshold argument lets the helper be tested at boundary
    values without depending on the production constant. Two unknowns
    with threshold=2 trip on the second.
    """
    streak, fire1 = _safety_unknown_streak_decision(_snap(False), 0, threshold=2)
    assert (streak, fire1) == (1, False)
    streak, fire2 = _safety_unknown_streak_decision(_snap(False), streak, threshold=2)
    assert (streak, fire2) == (2, True)
