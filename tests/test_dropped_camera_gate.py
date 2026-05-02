"""
Unit tests for _check_dropped_camera_emit (issue #85 + follow-up).

What this exercises
-------------------
The pure-function gate that the per-frame and corrected-batch emit
paths in ``MOTIONConnector`` consult before pushing a sample to the
UI:

  - Alive cameras pass through (return ``(True, None)``).
  - Dropped cameras are suppressed (return ``(False, ...)``).
  - The first suppression for a given dropped camera key returns a
    non-None warning string AND inserts the key into ``already_logged``;
    subsequent suppressions for the same key return ``(False, None)``.

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

from motion_connector import _check_dropped_camera_emit  # noqa: E402

pytestmark = pytest.mark.unit


def test_alive_camera_passes_through():
    should, msg = _check_dropped_camera_emit("left", 0, set(), set())
    assert should is True
    assert msg is None


def test_alive_camera_does_not_touch_already_logged():
    already = set()
    _check_dropped_camera_emit("left", 0, set(), already)
    assert already == set()


def test_dropped_camera_first_sample_returns_recovery_message():
    dropped = {("left", 0)}
    already_logged = set()
    should, msg = _check_dropped_camera_emit(
        "left", 0, dropped, already_logged,
    )
    assert should is False
    assert msg is not None
    # Message identifies the camera (1-indexed, side uppercased) and
    # includes the 'UNEXPECTED' / 'Connection Lost' framing so an
    # operator skimming the logs can find it.
    assert "UNEXPECTED" in msg
    assert "LEFT 1" in msg
    assert "Connection Lost" in msg


def test_dropped_camera_first_sample_marks_already_logged():
    dropped = {("right", 3)}
    already_logged = set()
    _check_dropped_camera_emit("right", 3, dropped, already_logged)
    # The helper mutates already_logged so the caller gets one-per-
    # dropout semantics for free — no need to track this externally.
    assert ("right", 3) in already_logged


def test_dropped_camera_second_sample_silent():
    dropped = {("left", 0)}
    already_logged = {("left", 0)}
    should, msg = _check_dropped_camera_emit(
        "left", 0, dropped, already_logged,
    )
    assert should is False
    assert msg is None  # already logged once, don't re-emit


def test_dropped_camera_repeated_calls_log_only_once():
    """The 40 Hz uncorrected stream calls the gate per sample. A
    dropped, flapping camera must produce exactly one warning per
    dropout — not 40/sec."""
    dropped = {("left", 5)}
    already_logged = set()
    messages = []
    for _ in range(50):
        _, msg = _check_dropped_camera_emit(
            "left", 5, dropped, already_logged,
        )
        if msg is not None:
            messages.append(msg)
    assert len(messages) == 1


def test_separate_dropped_cameras_each_log_once():
    """Each (side, cam_id) key gets its own one-time slot."""
    dropped = {("left", 0), ("right", 7)}
    already_logged = set()
    _, m1 = _check_dropped_camera_emit("left",  0, dropped, already_logged)
    _, m2 = _check_dropped_camera_emit("right", 7, dropped, already_logged)
    _, m3 = _check_dropped_camera_emit("left",  0, dropped, already_logged)
    _, m4 = _check_dropped_camera_emit("right", 7, dropped, already_logged)
    assert m1 is not None and "LEFT 1"  in m1
    assert m2 is not None and "RIGHT 8" in m2
    assert m3 is None
    assert m4 is None


def test_cam_id_accepts_numeric_strings():
    """sample.cam_id sometimes arrives as a numpy int / string-ish
    value off the SDK; the helper must coerce so set membership uses
    a canonical int in the tuple key."""
    dropped = {("left", 4)}
    already_logged = set()
    should, msg = _check_dropped_camera_emit(
        "left", "4", dropped, already_logged,
    )
    assert should is False
    assert msg is not None


def test_cam_id_one_indexed_in_message():
    """Internal cam_id is 0..7, but the operator-facing label is 1..8
    to match the physical camera numbering on the modules."""
    dropped = {("left", 0)}
    _, msg = _check_dropped_camera_emit("left", 0, dropped, set())
    assert "LEFT 1" in msg
    dropped = {("right", 7)}
    _, msg = _check_dropped_camera_emit("right", 7, dropped, set())
    assert "RIGHT 8" in msg
