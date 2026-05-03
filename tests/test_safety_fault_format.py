"""
Unit tests for MOTIONConnector._format_safety_fault_detail (issue #76).

Background
----------
Issue #76 reworked ``readSafetyStatus`` so the laser-safety toast
re-fires on every telemetry poll while safety is in the failed state
(previously the fire was gated on the False→True transition, so the
popup didn't reappear on subsequent scan attempts without a console
power-cycle). Both the initial-trip path and the re-fire path now
share a single fault-detail string built by
``_format_safety_fault_detail`` — kept stable across re-fires so the
toast text doesn't flicker between equivalent representations.

Marker
------
``pytest.mark.unit`` — pure function, no QApplication, no hardware.
The conftest autouse fixtures short-circuit on this marker so unit
tests don't drag in the bloodflow app launch / panel-button
calibration / liveness-guard machinery the UI tests need.
"""

import sys
from pathlib import Path

import pytest

# Repo root holds motion_connector.py — make it importable without
# turning the project into an installed package (same trick the
# other tests/ unit tests use).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from motion_connector import MOTIONConnector  # noqa: E402

pytestmark = pytest.mark.unit

# Mirror of omotion.ConsoleTelemetry._SAFETY_FAULTS so tests don't
# silently drift if the SDK adds a new bit. If this assertion fails,
# the SDK added or renamed a fault — update the dict below to match.
_EXPECTED_FAULT_BITS = {
    0x01: "POWER_PEAK_CURRENT_LIMIT_FAIL",
    0x02: "PULSE_UPPER_LIMIT_FAIL_OR_PULSE_LOWER_LIMIT_FAIL",
    0x04: "RATE_LOWER_LIMIT_FAIL",
}


def test_sdk_fault_mapping_matches_expectation():
    """Guard against silent SDK drift — if a new bit is added, the
    other tests below stop being authoritative until updated."""
    from omotion.ConsoleTelemetry import _SAFETY_FAULTS
    assert _SAFETY_FAULTS == _EXPECTED_FAULT_BITS


def test_no_faults():
    out = MOTIONConnector._format_safety_fault_detail(0x00, 0x00)
    assert out == "safety_se=0x00 (no faults); safety_so=0x00 (no faults)"


def test_single_se_fault_decoded():
    out = MOTIONConnector._format_safety_fault_detail(0x01, 0x00)
    assert "safety_se=0x01" in out
    assert "POWER_PEAK_CURRENT_LIMIT_FAIL" in out
    # safety_so still reads no faults
    assert "safety_so=0x00 (no faults)" in out


def test_single_so_fault_decoded():
    out = MOTIONConnector._format_safety_fault_detail(0x00, 0x04)
    assert "safety_so=0x04" in out
    assert "RATE_LOWER_LIMIT_FAIL" in out
    assert "safety_se=0x00 (no faults)" in out


def test_both_byte_faults():
    out = MOTIONConnector._format_safety_fault_detail(0x02, 0x01)
    assert "safety_se=0x02" in out
    assert "PULSE_UPPER_LIMIT_FAIL_OR_PULSE_LOWER_LIMIT_FAIL" in out
    assert "safety_so=0x01" in out
    assert "POWER_PEAK_CURRENT_LIMIT_FAIL" in out


def test_multiple_bits_in_one_byte():
    """All three SE fault bits set."""
    out = MOTIONConnector._format_safety_fault_detail(0x07, 0x00)
    assert "safety_se=0x07" in out
    for label in _EXPECTED_FAULT_BITS.values():
        assert label in out
    # so still no faults
    assert "safety_so=0x00 (no faults)" in out


def test_pure_function_stable_across_calls():
    """Issue #76: the toast text must not flicker between equivalent
    representations on re-fire. Same input → same output, exactly."""
    a = MOTIONConnector._format_safety_fault_detail(0x05, 0x02)
    b = MOTIONConnector._format_safety_fault_detail(0x05, 0x02)
    assert a == b


def test_reserved_bits_outside_fault_mask_dont_show_as_fault_labels():
    """Bits in the reserved range (>0x07) shouldn't surface as labels
    even though the hex value still includes them."""
    out = MOTIONConnector._format_safety_fault_detail(0xF8, 0xF8)
    # Hex shows the full byte
    assert "safety_se=0xF8" in out
    assert "safety_so=0xF8" in out
    # No fault labels match — they all live in 0x07 territory
    for label in _EXPECTED_FAULT_BITS.values():
        assert label not in out
    assert "no faults" in out
