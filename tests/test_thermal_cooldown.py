"""Unit tests for thermal_cooldown (issue #102). No hardware, no Qt app."""
import math

import pytest

from thermal_cooldown import q88_to_celsius, read_camera_temps

pytestmark = pytest.mark.unit


class _FakeSensor:
    """temps_by_cam: cam -> float degC, False (fw error), or absent (raises)."""

    def __init__(self, temps_by_cam, connected=True):
        self._temps = temps_by_cam
        self._connected = connected
        self.read_calls = 0

    def is_connected(self):
        return self._connected

    def i2c_read_register(self, dev, addr, read_len, reg_addr_size, mux_channel):
        self.read_calls += 1
        assert (dev, addr, read_len, reg_addr_size) == (0x36, 0x4D2A, 2, 2)
        val = self._temps[mux_channel]          # KeyError = unreachable camera
        if val is False:
            return False
        raw = int(round(val * 256)) & 0xFFFF
        return bytes([raw >> 8, raw & 0xFF])


class _FakeInterface:
    def __init__(self, left=None, right=None):
        self.left = left
        self.right = right


def test_q88_positive_and_negative():
    assert q88_to_celsius(bytes([0x3C, 0x80])) == pytest.approx(60.5)
    assert q88_to_celsius(bytes([0xFF, 0x00])) == pytest.approx(-1.0)


def test_read_camera_temps_mixed_outcomes():
    left = _FakeSensor({0: 45.0, 1: 113.5, 2: False})     # cams 3-7 raise
    iface = _FakeInterface(left=left, right=None)
    temps = read_camera_temps(iface)
    assert temps[("left", 0)] == pytest.approx(45.0)
    assert temps[("left", 1)] == pytest.approx(113.5)
    assert math.isnan(temps[("left", 2)])                  # fw error -> NaN
    assert math.isnan(temps[("left", 7)])                  # raise -> NaN
    assert not any(side == "right" for side, _ in temps)   # absent side skipped


def test_read_camera_temps_disconnected_sensor_skipped():
    left = _FakeSensor({0: 45.0}, connected=False)
    temps = read_camera_temps(_FakeInterface(left=left))
    assert temps == {} and left.read_calls == 0


def test_read_camera_temps_none_interface():
    assert read_camera_temps(None) == {}
