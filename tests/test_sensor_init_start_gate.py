"""Issue #303: Start clicked during the post-connect sensor init races it.

Repro: the UI enables Start as soon as the handles report READY, but the
connector's async sensor init (debug flags, camera power masks, sensor
info reads) keeps running for a few more seconds. A scan/CQ start inside
that window collides with the in-flight init — "Failed to program FPGA"
on both sensors within milliseconds — and can wedge a camera "not READY
for FPGA/config" until a DUT power-cycle.

The connector now raises a per-side init-in-flight counter when
``_schedule_sensor_init`` arms an init and drops it when the worker
drains (success or failure). ``sensorInitBusy`` exposes it to QML (Start/
Check disable on it) and ``_ensure_idle`` refuses pipeline starts while
it is up, so BloodFlow's start gate defers instead of colliding.

Pure connector-level tests: the interface is a MagicMock, so no hardware
and no QML. ``_run_sensor_init`` is driven synchronously (its worker
thread never spawns because no Qt event loop runs the settle QTimer).
"""

from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector

pytestmark = pytest.mark.unit

_INIT_BUSY_MSG = "Sensors are still initializing"


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


def test_fresh_connector_is_idle(tmp_path):
    conn = _connector(tmp_path)

    assert conn.sensorInitBusy is False
    assert conn._ensure_idle() is None


def test_schedule_raises_the_gate(tmp_path):
    """The gate must engage at schedule time — the race window opens the
    moment the connect handler queues the init, not when the worker starts."""
    conn = _connector(tmp_path)

    conn._schedule_sensor_init("left")

    assert conn.sensorInitBusy is True
    err = conn._ensure_idle()
    assert err is not None and _INIT_BUSY_MSG in err


def test_start_capture_refused_while_init_in_flight(tmp_path):
    conn = _connector(tmp_path)
    conn._schedule_sensor_init("left")
    log_lines = []
    conn.captureLog.connect(log_lines.append)

    ok = conn.startCapture("subj", 5, 0x99, 0x00, True)

    assert ok is False
    assert conn._capture_running is False
    assert any(_INIT_BUSY_MSG in line for line in log_lines)


def test_cq_check_refused_while_init_in_flight(tmp_path):
    conn = _connector(tmp_path)
    conn._schedule_sensor_init("right")
    finished = []
    conn.contactQualityCheckFinished.connect(
        lambda ok, err, warns: finished.append((ok, err)))

    conn.runContactQualityCheck(0x99, 0x00)

    assert finished == [(False, conn._ensure_idle())]
    assert _INIT_BUSY_MSG in finished[0][1]


def test_gate_clears_when_init_completes(tmp_path):
    conn = _connector(tmp_path)
    conn._run_sensor_init_impl = lambda side: None  # fast no-op init
    conn._schedule_sensor_init("left")
    assert conn.sensorInitBusy is True

    conn._run_sensor_init("left")  # what the worker thread runs

    assert conn.sensorInitBusy is False
    assert conn._ensure_idle() is None


def test_gate_clears_when_init_fails(tmp_path):
    """A failed init must not leak the busy state — that would lock
    Start/Check forever."""
    conn = _connector(tmp_path)

    def _boom(side):
        raise RuntimeError("init exploded")

    conn._run_sensor_init_impl = _boom
    conn._schedule_sensor_init("left")

    conn._run_sensor_init("left")  # swallows the exception by contract

    assert conn.sensorInitBusy is False
    assert conn._ensure_idle() is None


def test_gate_rearms_on_reconnect(tmp_path):
    """Init re-runs on every sensor (re)connect — a mid-session replug
    must re-engage the gate, not just the app-boot init."""
    conn = _connector(tmp_path)
    conn._run_sensor_init_impl = lambda side: None
    conn._schedule_sensor_init("left")
    conn._run_sensor_init("left")
    assert conn.sensorInitBusy is False

    conn._schedule_sensor_init("left")  # replug

    assert conn.sensorInitBusy is True
    err = conn._ensure_idle()
    assert err is not None and _INIT_BUSY_MSG in err


def test_both_sides_must_drain(tmp_path):
    conn = _connector(tmp_path)
    conn._run_sensor_init_impl = lambda side: None
    conn._schedule_sensor_init("left")
    conn._schedule_sensor_init("right")

    conn._run_sensor_init("left")
    assert conn.sensorInitBusy is True  # right still in flight

    conn._run_sensor_init("right")
    assert conn.sensorInitBusy is False


def test_busy_signal_fires_on_edges_only(tmp_path):
    """QML rebinds on sensorInitBusyChanged — emit on the busy/idle edges,
    not on every per-side count change."""
    conn = _connector(tmp_path)
    conn._run_sensor_init_impl = lambda side: None
    edges = []
    conn.sensorInitBusyChanged.connect(
        lambda: edges.append(conn.sensorInitBusy))

    conn._schedule_sensor_init("left")   # idle -> busy: emit
    conn._schedule_sensor_init("right")  # still busy: no emit
    conn._run_sensor_init("left")        # still busy: no emit
    conn._run_sensor_init("right")       # busy -> idle: emit

    assert edges == [True, False]


def test_unbalanced_drain_never_goes_negative(tmp_path):
    """A drain without a matching schedule (e.g. a test driving
    _run_sensor_init directly) clamps at zero instead of corrupting the
    counter for the next real init."""
    conn = _connector(tmp_path)
    conn._run_sensor_init_impl = lambda side: None

    conn._run_sensor_init("left")  # no schedule beforehand
    assert conn.sensorInitBusy is False

    conn._schedule_sensor_init("left")
    assert conn.sensorInitBusy is True  # next real init still gates
