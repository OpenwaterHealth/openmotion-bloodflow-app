"""Unit tests for ensure_tec_trip (motion_config) — TEC_TRIP push from app cfg.

ensure_tec_trip does a read-modify-write of the console user config, touching
ONLY the TEC_TRIP key so the calibration block and OPT_*/EE_* safety thresholds
survive. A 0/garbage value must never reach the device, because the firmware
treats TEC_TRIP == 0 as "over-temp trip disabled".
"""

import math

import pytest
from omotion import MotionConfig

from motion_config import (
    TEC_TRIP_MAX_C,
    TEC_TRIP_MIN_C,
    TecTripOutcome,
    ensure_tec_trip,
)

pytestmark = pytest.mark.unit


def _device_config(tec_trip=40):
    """Representative device user config: TEC_TRIP + OPT/EE safety keys + a
    calibration block. ensure_tec_trip must preserve every key but TEC_TRIP."""
    return MotionConfig(json_data={
        "TEC_TRIP": tec_trip,
        "OPT_THRESH": 7143,
        "EE_THRESH": 5000,
        "EE_GAIN": 1.86,
        "OPT_GAIN": 1.86,
        "calibration": {"version": 1, "bfi_scale": [1.0, 2.0]},
    })


class FakeConsole:
    """Records read/write of the user config for assertions.

    read_result: MotionConfig returned by read_config, or None, or an Exception
    instance to simulate read failure. write_returns_none / write_raises
    simulate a failed write.
    """

    def __init__(
        self, read_result, write_returns_none=False, write_raises=False
    ):
        self._read_result = read_result
        self._write_returns_none = write_returns_none
        self._write_raises = write_raises
        self.written = None
        self.write_calls = 0

    def read_config(self):
        if isinstance(self._read_result, Exception):
            raise self._read_result
        return self._read_result

    def write_config(self, config):
        self.write_calls += 1
        if self._write_raises:
            raise RuntimeError("simulated write failure")
        if self._write_returns_none:
            return None
        self.written = config
        return config


def test_valid_and_different_writes_and_preserves_other_keys():
    console = FakeConsole(_device_config(tec_trip=40))
    outcome = ensure_tec_trip(console, 45)
    assert outcome is TecTripOutcome.WROTE
    assert console.write_calls == 1
    written = console.written.json_data
    assert float(written["TEC_TRIP"]) == 45.0
    # Calibration + OPT/EE keys must ride through the read-modify-write.
    assert written["OPT_THRESH"] == 7143
    assert written["EE_THRESH"] == 5000
    assert written["EE_GAIN"] == 1.86
    assert written["OPT_GAIN"] == 1.86
    assert written["calibration"] == {"version": 1, "bfi_scale": [1.0, 2.0]}


def test_valid_and_equal_skips_write():
    console = FakeConsole(_device_config(tec_trip=40))
    assert ensure_tec_trip(console, 40) is TecTripOutcome.UNCHANGED
    assert console.write_calls == 0


def test_valid_and_equal_across_int_float_skips_write():
    console = FakeConsole(_device_config(tec_trip=40))
    assert ensure_tec_trip(console, 40.0) is TecTripOutcome.UNCHANGED
    assert console.write_calls == 0


@pytest.mark.parametrize(
    "bad",
    [0, -5, math.nan, None, "oops", TEC_TRIP_MAX_C + 1, TEC_TRIP_MIN_C - 0.5],
)
def test_invalid_values_never_write(bad):
    console = FakeConsole(_device_config(tec_trip=40))
    assert ensure_tec_trip(console, bad) is TecTripOutcome.SKIPPED_INVALID
    assert console.write_calls == 0


def test_read_config_none_is_failed():
    console = FakeConsole(None)
    assert ensure_tec_trip(console, 45) is TecTripOutcome.FAILED
    assert console.write_calls == 0


def test_read_config_raises_is_failed():
    console = FakeConsole(ValueError("not connected"))
    assert ensure_tec_trip(console, 45) is TecTripOutcome.FAILED
    assert console.write_calls == 0


def test_write_config_none_is_failed():
    console = FakeConsole(_device_config(tec_trip=40), write_returns_none=True)
    assert ensure_tec_trip(console, 45) is TecTripOutcome.FAILED
    assert console.write_calls == 1


def test_write_config_raises_is_failed():
    console = FakeConsole(_device_config(tec_trip=40), write_raises=True)
    assert ensure_tec_trip(console, 45) is TecTripOutcome.FAILED
    assert console.write_calls == 1


def test_device_missing_tec_trip_is_treated_as_differing():
    cfg = _device_config()
    del cfg.json_data["TEC_TRIP"]
    console = FakeConsole(cfg)
    assert ensure_tec_trip(console, 45) is TecTripOutcome.WROTE
    assert console.write_calls == 1
