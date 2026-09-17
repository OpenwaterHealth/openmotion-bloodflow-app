"""Unit tests for the EVT2-aware TEC DAC setpoint selection (issue #269).

EVT2 consoles need +1.16 V on the TEC DAC to hold the lasers at 25 C; DVT
and beyond use the ``TEC_VOLTAGE_DEFAULT`` from ``config/tec_params.py``
(compiled in since #546; it was ``tec_params.json`` before).
The unit revision is detected from the console's 3-bit board-ID strap
(``console.read_board_id()``, BRD_V0..V2): EVT2 straps 1, DVT1a straps 2.

Fail-safe direction: any read failure, unknown id, or the SDK's error
return (0) falls back to the config default — i.e., exactly the pre-#269
behavior. EVT2 is the only special case.
"""

import pytest

from config import tec_params as compiled
from motion_config import (
    EVT2_BOARD_IDS_DEFAULT,
    TEC_VOLTAGE_EVT2_DEFAULT,
    TecVoltageParams,
    select_tec_voltage,
    tec_voltage_params,
)

pytestmark = pytest.mark.unit


def _stamp(monkeypatch, **values):
    """Pretend config/tec_params.py carried these values."""
    for key, value in values.items():
        monkeypatch.setattr(compiled, key, value, raising=False)


class FakeConsole:
    """Mock of the SDK console's read_board_id seam."""

    def __init__(self, board_id=None, raises=None):
        self._board_id = board_id
        self._raises = raises
        self.calls = 0

    def read_board_id(self):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._board_id


# --- tec_voltage_params ------------------------------------------------------

def test_compiled_values():
    """Guard the shipped config/tec_params.py values."""
    params = tec_voltage_params()
    assert params.default_v == -0.07
    assert params.evt2_v == TEC_VOLTAGE_EVT2_DEFAULT == 1.16
    assert params.evt2_board_ids == EVT2_BOARD_IDS_DEFAULT == (1,)


def test_custom_values(monkeypatch):
    _stamp(monkeypatch, TEC_VOLTAGE_DEFAULT=-0.5, TEC_VOLTAGE_EVT2=1.2, EVT2_BOARD_IDS=[1, 5])
    params = tec_voltage_params()
    assert params == TecVoltageParams(default_v=-0.5, evt2_v=1.2, evt2_board_ids=(1, 5))


def test_bad_default_voltage_uses_hardcoded(monkeypatch):
    _stamp(monkeypatch, TEC_VOLTAGE_DEFAULT="oops")
    assert tec_voltage_params().default_v == -0.07


def test_bad_evt2_voltage_uses_default(monkeypatch):
    _stamp(monkeypatch, TEC_VOLTAGE_EVT2=None)
    assert tec_voltage_params().evt2_v == TEC_VOLTAGE_EVT2_DEFAULT


def test_board_ids_not_a_list_uses_default(monkeypatch):
    _stamp(monkeypatch, EVT2_BOARD_IDS="1")
    assert tec_voltage_params().evt2_board_ids == EVT2_BOARD_IDS_DEFAULT


def test_board_ids_filters_non_ints(monkeypatch):
    """bools (True == 1) and strings must never widen EVT2 detection."""
    _stamp(monkeypatch, EVT2_BOARD_IDS=[1, "2", 2.5, True, 3])
    assert tec_voltage_params().evt2_board_ids == (1, 3)


def test_empty_board_ids_disables_evt2_detection(monkeypatch):
    _stamp(monkeypatch, EVT2_BOARD_IDS=[])
    params = tec_voltage_params()
    assert params.evt2_board_ids == ()
    voltage, _reason = select_tec_voltage(FakeConsole(board_id=1), params)
    assert voltage == params.default_v


# --- select_tec_voltage ------------------------------------------------------

def test_select_evt2_board_id():
    params = TecVoltageParams()
    voltage, reason = select_tec_voltage(FakeConsole(board_id=1), params)
    assert voltage == params.evt2_v == 1.16
    assert "EVT2" in reason
    assert "1" in reason


def test_select_dvt_board_id():
    params = TecVoltageParams()
    voltage, reason = select_tec_voltage(FakeConsole(board_id=2), params)
    assert voltage == params.default_v
    assert "EVT2" not in reason
    assert "2" in reason


def test_select_error_return_zero_uses_default():
    """The SDK returns 0 on OW_ERROR / bad length — never treat as EVT2."""
    params = TecVoltageParams()
    voltage, _ = select_tec_voltage(FakeConsole(board_id=0), params)
    assert voltage == params.default_v


def test_select_read_raises_uses_default():
    params = TecVoltageParams()
    console = FakeConsole(raises=ValueError("Console controller not connected"))
    voltage, reason = select_tec_voltage(console, params)
    assert voltage == params.default_v
    assert "fail" in reason.lower()


def test_select_bool_true_is_not_evt2():
    """SDK demo mode returns True (== 1) from read_board_id — a bool must
    never satisfy EVT2 detection."""
    params = TecVoltageParams()
    voltage, _ = select_tec_voltage(FakeConsole(board_id=True), params)
    assert voltage == params.default_v


def test_select_custom_board_ids():
    params = TecVoltageParams(evt2_board_ids=(1, 3))
    voltage, reason = select_tec_voltage(FakeConsole(board_id=3), params)
    assert voltage == params.evt2_v
    assert "EVT2" in reason


def test_select_uses_configured_voltages():
    params = TecVoltageParams(default_v=-0.5, evt2_v=1.2)
    assert select_tec_voltage(FakeConsole(board_id=1), params)[0] == 1.2
    assert select_tec_voltage(FakeConsole(board_id=2), params)[0] == -0.5
