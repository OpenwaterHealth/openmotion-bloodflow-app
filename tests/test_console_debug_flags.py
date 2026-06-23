"""Settings → Developer switch for the console debug-logging flag.

``setConsoleDebugLogging`` toggles ``consoleDebugLogging`` in the runtime cache
and persisted config, then — when the console is connected — pushes the
``DEBUG_FLAG_USB_PRINTF`` bit live via ``console.enable_usb_printf`` (no
restart). Mirrors the sensor ``setSensorDebugFlag`` toggles and the live
``setConsoleFan`` developer toggle.
"""

from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector

pytestmark = pytest.mark.unit


def _connector(tmp_path, app_config=None, console=True):
    """Build a MotionConnector with a mocked interface.

    ``scan_db_path=None`` makes the audit log a no-op, and we stub
    ``_save_app_config`` so a toggle never writes the real
    ``config/app_config.json`` during tests. ``console`` controls whether
    ``is_device_connected`` reports the console as present.
    """
    iface = MagicMock()
    iface.is_device_connected.return_value = (console, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = None
    iface.console.enable_usb_printf.return_value = True
    c = MotionConnector(
        interface=iface, app_config=app_config or {},
        data_dir=str(tmp_path), config_dir="config",
    )
    c._save_app_config = MagicMock()
    return c, iface


def test_enable_persists_cache_config_and_pushes_printf(tmp_path):
    c, iface = _connector(tmp_path, {"consoleDebugLogging": False})

    c.setConsoleDebugLogging(True)

    assert c._console_debug_logging is True
    assert c._app_config["consoleDebugLogging"] is True
    c._save_app_config.assert_called_once()
    iface.console.enable_usb_printf.assert_called_once_with(True)


def test_disable_pushes_printf_false(tmp_path):
    c, iface = _connector(tmp_path, {"consoleDebugLogging": True})

    c.setConsoleDebugLogging(False)

    assert c._console_debug_logging is False
    assert c._app_config["consoleDebugLogging"] is False
    iface.console.enable_usb_printf.assert_called_once_with(False)


def test_disconnected_console_persists_without_hardware_call(tmp_path):
    c, iface = _connector(tmp_path, {"consoleDebugLogging": False}, console=False)

    c.setConsoleDebugLogging(True)

    # Value still persists for the next connect...
    assert c._console_debug_logging is True
    assert c._app_config["consoleDebugLogging"] is True
    c._save_app_config.assert_called_once()
    # ...but no live push is attempted when the console is absent.
    iface.console.enable_usb_printf.assert_not_called()
