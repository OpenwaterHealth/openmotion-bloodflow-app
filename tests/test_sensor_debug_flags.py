"""Settings → Engineering switches for the sensor debug flags.

``setSensorDebugFlag`` toggles ``histoCmp`` / ``sensorDebugLogging`` in the
runtime cache and persisted config, then re-pushes the recomputed firmware
debug-flag bitmask to every connected sensor immediately (no restart) —
mirroring the live ``setConsoleFan`` engineering toggle.
"""

from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector
from omotion.config import DEBUG_FLAG_HISTO_CMP, DEBUG_FLAG_USB_PRINTF

pytestmark = pytest.mark.unit


def _connector(tmp_path, app_config=None, left=True, right=True):
    """Build a MotionConnector with a mocked sensor interface.

    ``scan_db_path=None`` makes the audit log a no-op, and we stub
    ``_save_app_config`` so a toggle never writes the real
    ``config/app_config.json`` during tests.
    """
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


def test_enable_histo_cmp_sets_cache_config_and_pushes_bit(tmp_path):
    c, iface = _connector(tmp_path, {"histoCmp": False})

    c.setSensorDebugFlag("histoCmp", True)

    assert c._histo_cmp is True
    assert c._app_config["histoCmp"] is True
    c._save_app_config.assert_called_once()
    iface.left.set_debug_flags.assert_called_once_with(DEBUG_FLAG_HISTO_CMP)
    iface.right.set_debug_flags.assert_called_once_with(DEBUG_FLAG_HISTO_CMP)


def test_enable_sensor_debug_logging_preserves_other_bits(tmp_path):
    c, iface = _connector(tmp_path, {"histoCmp": True, "sensorDebugLogging": False})

    c.setSensorDebugFlag("sensorDebugLogging", True)

    expected = DEBUG_FLAG_HISTO_CMP | DEBUG_FLAG_USB_PRINTF
    assert c._sensor_debug_logging is True
    assert c._app_config["sensorDebugLogging"] is True
    iface.left.set_debug_flags.assert_called_once_with(expected)


def test_disabling_last_flag_pushes_zero(tmp_path):
    # No != 0 guard on the live path: clearing the last flag must actually
    # write 0 so the firmware turns the bit off.
    c, iface = _connector(tmp_path, {"histoCmp": True})

    c.setSensorDebugFlag("histoCmp", False)

    assert c._histo_cmp is False
    iface.left.set_debug_flags.assert_called_once_with(0)


def test_only_connected_sensors_receive_push(tmp_path):
    c, iface = _connector(tmp_path, {"histoCmp": False}, left=True, right=False)

    c.setSensorDebugFlag("histoCmp", True)

    iface.left.set_debug_flags.assert_called_once_with(DEBUG_FLAG_HISTO_CMP)
    iface.right.set_debug_flags.assert_not_called()


def test_unknown_key_is_noop(tmp_path):
    c, iface = _connector(tmp_path, {"histoCmp": False})

    c.setSensorDebugFlag("bogusKey", True)

    assert "bogusKey" not in c._app_config
    c._save_app_config.assert_not_called()
    iface.left.set_debug_flags.assert_not_called()
