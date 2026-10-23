"""Unit tests for how the pre-scan contact-quality check turns the SDK's
per-camera reason into the modal's warning types (issue #731).

A camera the SDK reports as ``no_signal`` delivered no usable frames, so
there is no reading to judge its contact by. It used to be shown as "Poor
sensor contact" (epic #722: the operator was told to re-seat sensors that
sat on the phantom the whole time). It now keeps its own warning type, worded
as a data problem, and the live adapter words it identically.

No hardware: the interface is faked and the CQ worker thread runs inline, so
the whole chain runContactQualityCheck → _cq_result_signal →
_on_cq_result_ready → contactQualityCheckFinished runs on the test thread.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from test_contact_quality_mask import _SyncThread  # noqa: E402

pytestmark = pytest.mark.unit

NAN = float("nan")


def _cam(reason, light_avg_dn=NAN, dark_max_dn=NAN):
    return SimpleNamespace(
        reason=reason, light_avg_dn=light_avg_dn, dark_max_dn=dark_max_dn,
    )


@pytest.fixture
def connector(monkeypatch):
    from motion_connector import MotionConnector

    iface = MagicMock()
    iface.scan_db_path = None  # a MagicMock path becomes a DB file in cwd (#620)
    iface.console = MagicMock()
    iface.left = MagicMock()
    iface.right = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow = MagicMock()
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.contact_quality_workflow = MagicMock()

    c = MotionConnector(
        interface=iface,
        app_config={"engineeringMode": False},
        data_dir=".",
        config_dir="config",
    )
    c._consoleConnected = True
    c._leftSensorConnected = True
    c._rightSensorConnected = True

    monkeypatch.setattr("motion_connector.threading.Thread", _SyncThread)
    return c


def _run_check(connector, per_camera, passed=False):
    """Run the preflight check against a canned SDK result and return the
    (ok, error, warnings) that contactQualityCheckFinished carried."""
    connector._interface.contact_quality_workflow.check.return_value = (
        SimpleNamespace(per_camera=per_camera, passed=passed)
    )
    finished = []
    connector.contactQualityCheckFinished.connect(
        lambda ok, err, warnings: finished.append((ok, err, warnings)))

    connector.runContactQualityCheck(0xFF, 0xFF)

    assert len(finished) == 1
    return finished[0]


def test_preflight_reports_no_signal_as_its_own_warning(connector):
    """The 2026-10-08 shape from #722 (camera 7 on both sides sent nothing)
    next to one genuinely poorly coupled camera: each keeps its own type,
    and no no_signal camera is reported as poor contact."""
    ok, err, warnings = _run_check(connector, {
        ("left", 6): _cam("no_signal"),
        ("right", 1): _cam("poor_contact", light_avg_dn=1.5, dark_max_dn=0.4),
        ("right", 6): _cam("no_signal"),
        ("left", 0): _cam("ok", light_avg_dn=40.0, dark_max_dn=0.2),
    })

    assert ok is False
    assert err == "Contact quality check failed"
    assert warnings == [
        {"camera": "L7", "typeKey": "no_signal",
         "typeText": "No data from camera", "value": 0.0},
        {"camera": "R2", "typeKey": "poor_contact",
         "typeText": "Poor sensor contact", "value": 1.5},
        {"camera": "R7", "typeKey": "no_signal",
         "typeText": "No data from camera", "value": 0.0},
    ]


def test_preflight_keeps_ambient_and_poor_contact_mappings(connector):
    """The two existing types are untouched by #731."""
    _, _, warnings = _run_check(connector, {
        ("left", 2): _cam("ambient_light", light_avg_dn=30.0, dark_max_dn=9.0),
        ("left", 3): _cam("poor_contact", light_avg_dn=2.0, dark_max_dn=0.1),
    })

    assert warnings == [
        {"camera": "L3", "typeKey": "ambient_light",
         "typeText": "Ambient light detected", "value": 9.0},
        {"camera": "L4", "typeKey": "poor_contact",
         "typeText": "Poor sensor contact", "value": 2.0},
    ]


def test_preflight_and_live_word_no_signal_identically(connector):
    """Live and preflight must agree on the key and the operator text, so a
    camera never changes wording between the pre-scan check and the scan.
    The live half goes through the real __init__-wired signal."""
    _, _, warnings = _run_check(connector, {("left", 3): _cam("no_signal")})
    live = []
    connector.contactQualityWarning.connect(lambda *a: live.append(a))

    connector._cqTransitionSignal.emit("left", 3, "no_signal", 0.0, True)

    assert len(warnings) == 1 and len(live) == 1
    pre = warnings[0]
    assert (pre["camera"], pre["typeKey"], pre["typeText"]) == live[0][:3]
    assert live[0][:3] == ("L4", "no_signal", "No data from camera")
