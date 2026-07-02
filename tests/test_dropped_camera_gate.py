"""
Unit tests for _rearm_dropped_camera (issue #85 follow-up).

What this exercises
-------------------
The pure-function helper the per-frame emit path in ``MotionConnector``
consults when a frame arrives for a camera:

  - Alive cameras (not in ``dropped``) are a no-op (return ``None``).
  - A camera in ``dropped`` is RE-ARMED: removed from the set, and the
    helper returns a recovery message for the caller to log. Dropout
    marking is no longer permanent for the scan — frames resuming is
    proof the camera is back, so display resumes and the watchdog can
    re-fire if the camera goes silent again.

This replaced the old ``_check_dropped_camera_emit`` gate, which
suppressed a recovered camera's samples for the rest of the scan —
turning any transient stall (or a covered sensor, back when liveness was
keyed to finite BFI instead of frame arrival) into a dead display.

Marker
------
``pytest.mark.unit`` — the autouse fixtures in ``conftest.py`` short-
circuit on this marker so unit tests don't drag in the bloodflow app
launch / panel-button calibration / liveness-guard machinery the UI
tests need. Pure Python, no QApplication, no hardware.

Run with:  pytest tests/test_dropped_camera_gate.py -v
"""

import sys
from pathlib import Path

import pytest

# Repo root holds motion_connector.py — make it importable without
# turning the project into an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from motion_connector import _rearm_dropped_camera  # noqa: E402

pytestmark = pytest.mark.unit


def test_alive_camera_is_noop():
    dropped = set()
    msg = _rearm_dropped_camera("left", 0, dropped)
    assert msg is None
    assert dropped == set()


def test_alive_camera_does_not_touch_other_keys():
    dropped = {("right", 3)}
    msg = _rearm_dropped_camera("left", 0, dropped)
    assert msg is None
    assert dropped == {("right", 3)}


def test_dropped_camera_is_rearmed_and_returns_message():
    dropped = {("left", 0)}
    msg = _rearm_dropped_camera("left", 0, dropped)
    assert msg is not None
    # Camera has been un-marked — display resumes, watchdog re-armed.
    assert dropped == set()
    # Message identifies the camera (1-indexed, side uppercased) and the
    # re-arm framing so an operator skimming the logs can find it.
    assert "LEFT 1" in msg
    assert "resumed" in msg
    assert "re-arming" in msg


def test_second_frame_after_rearm_is_silent():
    """The 40 Hz stream calls the helper per frame. Only the frame that
    performs the re-arm returns a message — the camera is out of the set
    afterwards, so subsequent frames are the alive-camera no-op."""
    dropped = {("left", 5)}
    messages = []
    for _ in range(50):
        msg = _rearm_dropped_camera("left", 5, dropped)
        if msg is not None:
            messages.append(msg)
    assert len(messages) == 1
    assert dropped == set()


def test_camera_can_drop_and_recover_repeatedly():
    """A flapping camera produces one recovery message per dropout
    episode — the watchdog re-adds the key when frames stop again."""
    dropped = set()
    messages = []
    for _ in range(3):
        dropped.add(("right", 2))          # watchdog fires
        msg = _rearm_dropped_camera("right", 2, dropped)
        if msg is not None:
            messages.append(msg)
        assert ("right", 2) not in dropped  # re-armed each time
    assert len(messages) == 3


def test_separate_dropped_cameras_each_rearm_independently():
    dropped = {("left", 0), ("right", 7)}
    m1 = _rearm_dropped_camera("left", 0, dropped)
    assert m1 is not None and "LEFT 1" in m1
    assert dropped == {("right", 7)}
    m2 = _rearm_dropped_camera("right", 7, dropped)
    assert m2 is not None and "RIGHT 8" in m2
    assert dropped == set()


def test_cam_id_accepts_numeric_strings():
    """sample.cam_id sometimes arrives as a numpy int / string-ish
    value off the SDK; the helper must coerce so set membership uses
    a canonical int in the tuple key."""
    dropped = {("left", 4)}
    msg = _rearm_dropped_camera("left", "4", dropped)
    assert msg is not None
    assert dropped == set()


def test_cam_id_one_indexed_in_message():
    """Internal cam_id is 0..7, but the operator-facing label is 1..8
    to match the physical camera numbering on the modules."""
    msg = _rearm_dropped_camera("left", 0, {("left", 0)})
    assert "LEFT 1" in msg
    msg = _rearm_dropped_camera("right", 7, {("right", 7)})
    assert "RIGHT 8" in msg
