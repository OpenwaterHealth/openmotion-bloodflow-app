"""Issue #303: Start clicked during a device's connect-time bring-up races it.

Repro: the UI enables Start as soon as the handles report READY, but the
connector's async sensor init (debug flags, camera power masks, sensor
info reads) keeps running for a few more seconds. A scan/CQ start inside
that window collides with the in-flight init — "Failed to program FPGA"
on both sensors within milliseconds — and can wedge a camera "not READY
for FPGA/config" until a DUT power-cycle. Since #667 the console's
bring-up (laser registers, TEC, fan) runs on a worker too, so a start in
its window would scan before the laser registers are loaded.

The connector raises a per-device bring-up counter when
``_schedule_device_init`` arms one and drops it when the worker drains
(success or failure). ``deviceInitBusy`` exposes it to QML (Start/Check
disable on it) and ``_ensure_idle`` refuses pipeline starts while it is
up, so BloodFlow's start gate defers instead of colliding.

Pure connector-level tests: the interface is a MagicMock, so no hardware
and no QML. ``_run_device_init`` is driven synchronously (its worker
thread never spawns because no Qt event loop runs the scheduling QTimer).
"""

from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector

pytestmark = pytest.mark.unit

_INIT_BUSY_MSG = "still initializing"


def _connector(tmp_path, app_config=None):
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = None
    cfg = {"engineeringMode": False}
    cfg.update(app_config or {})
    conn = MotionConnector(
        interface=iface, app_config=cfg,
        data_dir=str(tmp_path), config_dir="config",
    )
    # Fast no-op bring-ups; tests that need a real sequence call it directly.
    conn._init_sensor = lambda side, gen: None
    conn._init_console = lambda gen: None
    return conn


def _drain(conn, name):
    """What the worker thread runs for the current connection."""
    conn._run_device_init(name, conn._device_connect_gen[name])


def test_fresh_connector_is_idle(tmp_path):
    conn = _connector(tmp_path)

    assert conn.deviceInitBusy is False
    assert conn._ensure_idle() is None


@pytest.mark.parametrize("name", ["left", "console"])
def test_schedule_raises_the_gate(tmp_path, name):
    """The gate must engage at schedule time — the race window opens the
    moment the connect handler queues the bring-up, not when the worker
    starts."""
    conn = _connector(tmp_path)

    conn._schedule_device_init(name)

    assert conn.deviceInitBusy is True
    err = conn._ensure_idle()
    assert err is not None and _INIT_BUSY_MSG in err


def test_start_capture_refused_while_init_in_flight(tmp_path):
    conn = _connector(tmp_path)
    conn._schedule_device_init("left")
    log_lines = []
    conn.captureLog.connect(log_lines.append)

    ok = conn.startCapture("subj", 5, 0x99, 0x00, True)

    assert ok is False
    assert conn._capture_running is False
    assert any(_INIT_BUSY_MSG in line for line in log_lines)


def test_cq_check_refused_while_init_in_flight(tmp_path):
    conn = _connector(tmp_path)
    conn._schedule_device_init("right")
    finished = []
    conn.contactQualityCheckFinished.connect(
        lambda ok, err, warns: finished.append((ok, err)))

    conn.runContactQualityCheck(0x99, 0x00)

    assert finished == [(False, conn._ensure_idle())]
    assert _INIT_BUSY_MSG in finished[0][1]


@pytest.mark.parametrize("slot", ["runCalibration", "runTestScan"])
def test_calibration_and_test_scan_refused_while_console_init_in_flight(
        tmp_path, slot):
    """Calibrate and Test fire the laser but don't go through _ensure_idle.
    While the GUI thread ran the console's setup they could not start
    before it finished; on a worker they must be refused explicitly."""
    conn = _connector(tmp_path, {"engineeringMode": True})
    conn._schedule_device_init("console")
    log_lines = []
    conn.captureLog.connect(log_lines.append)

    getattr(conn, slot)("both")

    assert any(_INIT_BUSY_MSG in line for line in log_lines)
    conn._interface.start_calibration.assert_not_called()
    conn._interface.start_test_scan.assert_not_called()


def test_gate_clears_when_init_completes(tmp_path):
    conn = _connector(tmp_path)
    conn._schedule_device_init("left")
    assert conn.deviceInitBusy is True

    _drain(conn, "left")

    assert conn.deviceInitBusy is False
    assert conn._ensure_idle() is None


def test_gate_clears_when_init_fails(tmp_path):
    """A failed init must not leak the busy state — that would lock
    Start/Check forever."""
    conn = _connector(tmp_path)

    def _boom(side, gen):
        raise RuntimeError("init exploded")

    conn._init_sensor = _boom
    conn._schedule_device_init("left")

    _drain(conn, "left")  # swallows the exception by contract

    assert conn.deviceInitBusy is False
    assert conn._ensure_idle() is None


def test_gate_rearms_on_reconnect(tmp_path):
    """Init re-runs on every (re)connect — a mid-session replug must
    re-engage the gate, not just the app-boot init."""
    conn = _connector(tmp_path)
    conn._schedule_device_init("left")
    _drain(conn, "left")
    assert conn.deviceInitBusy is False

    conn._schedule_device_init("left")  # replug

    assert conn.deviceInitBusy is True
    err = conn._ensure_idle()
    assert err is not None and _INIT_BUSY_MSG in err


def test_every_device_must_drain(tmp_path):
    conn = _connector(tmp_path)
    conn._schedule_device_init("console")
    conn._schedule_device_init("left")
    conn._schedule_device_init("right")

    _drain(conn, "left")
    _drain(conn, "right")
    assert conn.deviceInitBusy is True  # console still in flight

    _drain(conn, "console")
    assert conn.deviceInitBusy is False


def test_busy_signal_fires_on_edges_only(tmp_path):
    """QML rebinds on deviceInitBusyChanged — emit on the busy/idle edges,
    not on every per-device count change."""
    conn = _connector(tmp_path)
    edges = []
    conn.deviceInitBusyChanged.connect(
        lambda: edges.append(conn.deviceInitBusy))

    conn._schedule_device_init("left")   # idle -> busy: emit
    conn._schedule_device_init("right")  # still busy: no emit
    _drain(conn, "left")                 # still busy: no emit
    _drain(conn, "right")                # busy -> idle: emit

    assert edges == [True, False]


def test_unbalanced_drain_never_goes_negative(tmp_path):
    """A drain without a matching schedule (e.g. a test driving
    _run_device_init directly) clamps at zero instead of corrupting the
    counter for the next real init."""
    conn = _connector(tmp_path)

    _drain(conn, "left")  # no schedule beforehand
    assert conn.deviceInitBusy is False

    conn._schedule_device_init("left")
    assert conn.deviceInitBusy is True  # next real init still gates
