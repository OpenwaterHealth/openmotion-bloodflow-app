"""Sensor connect-time init must not block the GUI thread.

The init sequence (debug flags + camera power + 0.5 s settle + ID-cache
fill + identity log) froze the GUI for >0.5 s per sensor (re)connect when
it ran on the main thread. It now runs on a daemon worker; these tests
pin the scheduling seam. test_send_defer_flag still calls
_run_sensor_init synchronously — that contract is preserved.
"""

import threading
from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector

pytestmark = pytest.mark.unit


def _connector(tmp_path):
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = None
    return MotionConnector(
        interface=iface, app_config={"engineeringMode": False},
        data_dir=str(tmp_path), config_dir="config",
    )


def test_init_worker_runs_off_the_calling_thread(tmp_path):
    c = _connector(tmp_path)
    ran_on = []
    done = threading.Event()

    def fake_init(side):
        ran_on.append((side, threading.current_thread()))
        done.set()

    c._run_sensor_init = fake_init  # instance attr shadows the method
    c._start_sensor_init_worker("left")
    assert done.wait(5), "init worker never ran"
    side, thread = ran_on[0]
    assert side == "left"
    assert thread is not threading.main_thread()
    assert thread.daemon


def test_run_sensor_init_never_raises(tmp_path):
    """An exception on a plain worker thread would vanish to stderr and
    leave the sensor half-initialized silently — the wrapper must catch
    and log instead."""
    c = _connector(tmp_path)

    def boom(side):
        raise RuntimeError("boom")

    c._run_sensor_init_impl = boom
    c._run_sensor_init("left")  # must not raise
