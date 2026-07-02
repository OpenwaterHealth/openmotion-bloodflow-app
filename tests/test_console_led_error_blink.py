"""Issue #257: critical errors must blink the console LED blue.

Repro: read-only output folder → Start scan → E-301 modal shows, but the
console front-panel LED stayed solid green. The connector now starts a
blue/off blink (mirroring the firmware Error_Handler's 500 ms blink) when
``_raise_critical`` fires, and ``criticalErrorsDismissed`` — called by
CriticalErrorModal when its queue empties — stops the blink and restores
the idle green state.

Pure connector-level tests: the console handle is a MagicMock, so no
hardware and no QML. The blink QTimer is started/stopped synchronously;
ticks are driven by calling ``_on_error_led_tick`` directly (no event loop
runs in unit tests).
"""

from unittest.mock import MagicMock

import pytest

from motion_connector import (
    MotionConnector,
    _RGB_LED_BLUE,
    _RGB_LED_GREEN,
    _RGB_LED_OFF,
)

pytestmark = pytest.mark.unit


def _connector(tmp_path):
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = None
    # set_rgb_led echoes the requested state back, like the SDK on success.
    iface.console.set_rgb_led.side_effect = lambda s: s
    return MotionConnector(
        interface=iface, app_config={"engineeringMode": False},
        data_dir=str(tmp_path), config_dir="config",
    )


def _led_calls(conn):
    return [c.args[0] for c in
            conn._interface.console.set_rgb_led.call_args_list]


def test_raise_critical_starts_blue_blink(tmp_path):
    conn = _connector(tmp_path)

    conn._raise_critical("E-301", detail="scan DB read-only")

    # First edge fires immediately: LED driven blue, timer running for the
    # subsequent off/blue toggles.
    assert _led_calls(conn) == [_RGB_LED_BLUE]
    assert conn._error_led_timer.isActive()


def test_ticks_toggle_blue_and_off(tmp_path):
    conn = _connector(tmp_path)
    conn._raise_critical("E-301")

    conn._on_error_led_tick()  # what the QTimer does every 500 ms
    conn._on_error_led_tick()

    assert _led_calls(conn) == [_RGB_LED_BLUE, _RGB_LED_OFF, _RGB_LED_BLUE]


def test_second_error_keeps_single_blink(tmp_path):
    """Errors queued while the modal is already up must not restart or
    double-drive the blink."""
    conn = _connector(tmp_path)

    conn._raise_critical("E-301")
    conn._raise_critical("E-302")

    assert _led_calls(conn) == [_RGB_LED_BLUE]  # one first-edge only
    assert conn._error_led_timer.isActive()


def test_dismiss_stops_blink_and_restores_green(tmp_path):
    conn = _connector(tmp_path)
    conn._raise_critical("E-301")

    conn.criticalErrorsDismissed()

    assert not conn._error_led_timer.isActive()
    assert _led_calls(conn)[-1] == _RGB_LED_GREEN


def test_dismiss_without_prior_error_leaves_led_alone(tmp_path):
    """criticalErrorsDismissed with no blink running must not touch the
    LED — the firmware owns it in normal operation."""
    conn = _connector(tmp_path)

    conn.criticalErrorsDismissed()

    conn._interface.console.set_rgb_led.assert_not_called()


def test_console_write_failure_stops_blink_and_never_raises(tmp_path):
    """Console unreachable (e.g. not connected): the SDK raises ValueError.
    _raise_critical must still emit the modal signal and the blink must
    stop retrying instead of spamming failing UART writes."""
    conn = _connector(tmp_path)
    conn._interface.console.set_rgb_led.side_effect = ValueError(
        "Console controller not connected")
    received = []
    conn.criticalErrorRaised.connect(lambda *a: received.append(a))

    conn._raise_critical("E-301")

    assert len(received) == 1  # modal still raised
    assert not conn._error_led_timer.isActive()

    # A later dismiss is a clean no-op (no restore attempted on top of the
    # failed blink).
    conn._interface.console.set_rgb_led.reset_mock()
    conn.criticalErrorsDismissed()
    conn._interface.console.set_rgb_led.assert_not_called()
