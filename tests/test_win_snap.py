"""Unit tests for utils/win_snap.py (issue #642)."""

import sys

import pytest

from utils import win_snap

pytestmark = pytest.mark.unit


def test_signed16_decodes_negative_screen_coords():
    # WM_NCHITTEST packs signed 16-bit coords; monitors left of / above the
    # primary one produce negative values.
    assert win_snap._signed16(0x0010) == 16
    assert win_snap._signed16(0xFFF8) == -8
    assert win_snap._signed16(0x1234FFFF) == -1


@pytest.mark.parametrize("hwnd", [0, None, "not-a-handle"])
def test_enable_window_snap_rejects_bad_hwnd(hwnd):
    assert win_snap.enable_window_snap(object(), hwnd) is False


def test_enable_window_snap_is_noop_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert win_snap.enable_window_snap(object(), 1234) is False
