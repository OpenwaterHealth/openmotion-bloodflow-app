"""Connect-time bring-up must not block the GUI thread.

SDK state changes reach the connector queued on the GUI thread. The
sensor sequence (debug flags + camera power + 0.5 s settle + ID-cache
fill + identity log) froze the GUI for >0.5 s per sensor (re)connect, and
the console's (identity, TEC, fan, 18 laser-register writes + read-back,
TEC trip) for ~3.7 s, which also delayed every queued state change and
timer behind it (#667). Both now run on a daemon worker; these tests pin
the scheduling seam and the console sequence.
"""

import threading
from unittest.mock import MagicMock

import pytest

import motion_connector
from motion_connector import MotionConnector

pytestmark = pytest.mark.unit


def _connector(tmp_path):
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = None
    # An empty version keeps _log_device_stats from starting a firmware
    # update check (a network call) on a worker thread.
    iface.console.get_version.return_value = ""
    return MotionConnector(
        interface=iface, app_config={"engineeringMode": False},
        data_dir=str(tmp_path), config_dir="config",
    )


def _console_connect(conn):
    from omotion import ConnectionState as S
    handle = MagicMock()
    handle.name = "console"
    conn._on_handle_state_changed_impl(handle, S.CONNECTING, S.CONNECTED, "ping_ok")


def _console_init(conn):
    conn._run_device_init("console", conn._device_connect_gen["console"])


def _critical_codes(conn):
    out = []
    conn.criticalErrorRaised.connect(lambda code, *rest: out.append(code))
    return out


@pytest.mark.parametrize("name", ["console", "left"])
def test_init_worker_runs_off_the_calling_thread(tmp_path, name):
    c = _connector(tmp_path)
    ran_on = []
    done = threading.Event()

    def fake_init(dev, gen):
        ran_on.append((dev, threading.current_thread()))
        done.set()

    c._run_device_init = fake_init  # instance attr shadows the method
    c._start_device_init_worker(name, 0)
    assert done.wait(5), "init worker never ran"
    dev, thread = ran_on[0]
    assert dev == name
    assert thread is not threading.main_thread()
    assert thread.daemon


def test_run_device_init_never_raises(tmp_path):
    """An exception on a plain worker thread would vanish to stderr and
    leave the device half-initialized silently — the wrapper must catch
    and log instead."""
    c = _connector(tmp_path)

    def boom(side, gen):
        raise RuntimeError("boom")

    c._init_sensor = boom
    c._run_device_init("left", 0)  # must not raise


def test_console_connect_does_no_console_io_in_the_slot(tmp_path):
    """The regression behind the #667 freeze: the CONNECTED slot only
    schedules the bring-up and raises the busy gate."""
    c = _connector(tmp_path)
    console = c._interface.console

    _console_connect(c)

    for call in ("tec_voltage", "set_fan_speed", "read_board_id",
                 "get_version", "get_hardware_id", "read_serial_number"):
        getattr(console, call).assert_not_called()
    c._interface.log_console_info.assert_not_called()
    c._interface.apply_laser_power.assert_not_called()
    assert c.deviceInitBusy is True
    assert c._consoleConnected is True


def test_console_init_runs_every_step_in_order(tmp_path, monkeypatch):
    monkeypatch.setattr(motion_connector, "select_tec_voltage",
                        lambda console, params: (-0.07, "test"))
    c = _connector(tmp_path)
    _console_connect(c)

    _console_init(c)

    names = [call[0] for call in c._interface.method_calls]
    order = ["log_console_info", "console.tec_voltage",
             "console.set_fan_speed", "apply_laser_power",
             "console.get_version"]
    positions = [names.index(n) for n in order]
    assert positions == sorted(positions), names
    c._interface.console.tec_voltage.assert_called_once_with(-0.07)
    assert c.deviceInitBusy is False


def test_failed_early_step_does_not_skip_the_laser(tmp_path):
    """The old single try/except skipped the laser load (and E-103) when
    any earlier step raised, leaving the laser dark on a connected
    console. Each step now stands alone."""
    c = _connector(tmp_path)
    c._interface.log_console_info.side_effect = AttributeError("old SDK")
    c._interface.console.set_fan_speed.side_effect = ValueError("fan")
    _console_connect(c)

    _console_init(c)

    c._interface.apply_laser_power.assert_called_once()


def test_laser_failure_on_a_connected_console_raises_e103(tmp_path):
    c = _connector(tmp_path)
    c._interface.apply_laser_power.return_value = False
    codes = _critical_codes(c)
    _console_connect(c)

    _console_init(c)

    assert codes == ["E-103"]


def test_laser_error_on_a_connected_console_raises_e103(tmp_path):
    c = _connector(tmp_path)
    c._interface.apply_laser_power.side_effect = TimeoutError("i2c")
    codes = _critical_codes(c)
    _console_connect(c)

    _console_init(c)

    assert codes == ["E-103"]


def test_console_lost_mid_init_stops_quietly(tmp_path):
    """A disconnect during the bring-up skips the rest, with no E-103:
    the disconnect has its own notice."""
    c = _connector(tmp_path)
    console = c._interface.console
    console.is_connected.return_value = True

    def drop(**kwargs):
        console.is_connected.return_value = False
        raise ValueError("Motion Console not connected")

    console.set_fan_speed.side_effect = drop
    codes = _critical_codes(c)
    _console_connect(c)

    _console_init(c)

    c._interface.apply_laser_power.assert_not_called()
    assert codes == []
    assert c.deviceInitBusy is False


def test_laser_failure_after_disconnect_raises_no_e103(tmp_path):
    c = _connector(tmp_path)
    console = c._interface.console
    console.is_connected.return_value = True

    def drop(**kwargs):
        console.is_connected.return_value = False
        return False

    c._interface.apply_laser_power.side_effect = drop
    codes = _critical_codes(c)
    _console_connect(c)

    _console_init(c)

    assert codes == []


def test_bring_up_for_an_older_connection_stops(tmp_path):
    """A reconnect while an older bring-up is still queued or running:
    the old one must not write to the new connection."""
    c = _connector(tmp_path)
    _console_connect(c)
    stale_gen = c._device_connect_gen["console"]
    _console_connect(c)  # reconnect

    c._run_device_init("console", stale_gen)

    c._interface.log_console_info.assert_not_called()
    c._interface.apply_laser_power.assert_not_called()
