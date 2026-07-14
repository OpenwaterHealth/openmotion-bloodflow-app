"""Issue #213: a read-only application configuration must surface E-301.

When the scan database can't be written (e.g. the app folder is marked
read-only), the scan must fail *gracefully* and the critical-error modal
must show **E-301** — instead of the old behavior where the laser fired and
the SDK worker thread died silently with nothing logged under a code.

Two paths, both landing on E-301:

* **synchronous** — the SDK's start_scan write-probe fails the preflight
  (laser still off), start_scan returns False with ``last_scan_error`` set,
  and the connector's existing ``not started`` branch raises E-301.

* **asynchronous** — the scan DB dies in the race window after the preflight
  passed; the SDK worker invokes ``ScanRequest.on_error``, which the connector
  routes to a main-thread slot that raises E-301 and tears down the capture.

These mock the hardware seam (``interface.start_scan`` / the ``on_error``
callback) — no hardware, no app launch.
"""

from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector

pytestmark = pytest.mark.unit


def _make_connector(tmp_path, app_config=None):
    iface = MagicMock()
    iface.console = MagicMock()
    iface.left = MagicMock()
    iface.right = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow = MagicMock()
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_workflow.last_scan_error = None
    iface.start_scan.return_value = True
    iface.scan_db_path = None

    c = MotionConnector(
        interface=iface,
        app_config=app_config or {"engineeringMode": False},
        data_dir=str(tmp_path),
        config_dir="config",
    )
    c._consoleConnected = True
    c._leftSensorConnected = True
    c._rightSensorConnected = True
    return c


def _captured_request(connector):
    ok = connector.startCapture("subj", 5, 0x66, 0x66, False)
    assert ok is True
    connector._interface.start_scan.assert_called_once()
    return connector._interface.start_scan.call_args.args[0]


# ── synchronous preflight path ─────────────────────────────────────────────

def test_startcapture_raises_e301_when_preflight_reports_db_unavailable(tmp_path):
    """start_scan returns False with a DB reason (the SDK write-probe tripped)
    → the connector raises E-301 carrying that reason as detail, and reports
    the scan did not start."""
    connector = _make_connector(tmp_path)
    connector._interface.start_scan.return_value = False
    connector._interface.scan_workflow.last_scan_error = (
        "Scan database unavailable: attempt to write a readonly database"
    )
    received = []
    connector.criticalErrorRaised.connect(lambda *a: received.append(a))

    ok = connector.startCapture("subj", 5, 0x66, 0x66, False)

    assert ok is False
    assert len(received) == 1
    code, _title, _msg, _action, detail = received[0]
    assert code == "E-301"
    assert "readonly" in detail.lower()


# ── asynchronous worker-abort path ─────────────────────────────────────────

def test_scan_request_carries_on_error_handler(tmp_path):
    """The connector must give the SDK an on_error callback so an async worker
    abort (start_scan already returned True) can still be surfaced."""
    connector = _make_connector(tmp_path)
    req = _captured_request(connector)
    assert callable(req.on_error)


def test_worker_on_error_surfaces_e301_and_stops_capture(tmp_path):
    """Invoking the SDK's on_error callback (what the worker does when a
    critical sink dies mid-scan) raises E-301 and clears the running state so
    the app isn't wedged 'capturing' forever."""
    connector = _make_connector(tmp_path)
    req = _captured_request(connector)
    assert connector._capture_running is True  # start_scan returned True

    received = []
    connector.criticalErrorRaised.connect(lambda *a: received.append(a))

    req.on_error(
        RuntimeError(
            "ScanDBSink failed to initialize: attempt to write a readonly database"
        )
    )

    assert any(a[0] == "E-301" for a in received)
    assert connector._capture_running is False
