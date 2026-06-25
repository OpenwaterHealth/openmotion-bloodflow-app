"""``deferHistoSend`` config flag -> firmware ``DEBUG_FLAG_SEND_DEFER`` (bit 7).

Unlike ``histoCmp``/``sensorDebugLogging`` (live Settings -> Developer
toggles), ``deferHistoSend`` is config-only: it is read from the app config at
construction into ``_send_data_defer`` and folded into the sensor debug-flag
bitmask by ``_compute_sensor_debug_flags``, which is pushed to each sensor at
connect via ``_run_sensor_init``. The firmware bit moves the per-frame
histogram send out of the FSIN ISR into the main loop (sensor-fw#68).
"""

from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector
from omotion.config import DEBUG_FLAG_HISTO_CMP, DEBUG_FLAG_SEND_DEFER

pytestmark = pytest.mark.unit


def _connector(tmp_path, app_config=None, left=True, right=True):
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, left, right)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = None
    iface.left.is_connected.return_value = left
    iface.right.is_connected.return_value = right
    iface.left.set_debug_flags.return_value = True
    iface.right.set_debug_flags.return_value = True
    c = MotionConnector(
        interface=iface, app_config=app_config or {},
        data_dir=str(tmp_path), config_dir="config",
    )
    c._save_app_config = MagicMock()
    return c, iface


def test_defaults_off_when_absent(tmp_path):
    c, _ = _connector(tmp_path, {})

    assert c._send_data_defer is False
    assert c._compute_sensor_debug_flags() & DEBUG_FLAG_SEND_DEFER == 0


def test_enabled_sets_bit(tmp_path):
    c, _ = _connector(tmp_path, {"deferHistoSend": True})

    assert c._send_data_defer is True
    assert c._compute_sensor_debug_flags() == DEBUG_FLAG_SEND_DEFER


def test_combines_with_histo_cmp(tmp_path):
    c, _ = _connector(tmp_path, {"deferHistoSend": True, "histoCmp": True})

    assert c._compute_sensor_debug_flags() == (
        DEBUG_FLAG_SEND_DEFER | DEBUG_FLAG_HISTO_CMP
    )


def test_pushed_to_connected_sensor_at_init(tmp_path):
    # Config-only flags have no live-toggle path: they reach the firmware via
    # the connect-time _run_sensor_init push, so that is what we exercise here.
    c, iface = _connector(tmp_path, {"deferHistoSend": True})
    c._leftSensorConnected = True

    c._run_sensor_init("left")

    iface.left.set_debug_flags.assert_called_once_with(DEBUG_FLAG_SEND_DEFER)
