"""Unit tests for pre-scan contact-quality escalation (#390).

During the 725-cycle reliability campaign the preflight check detected total
loss of optical signal on up to all 8 cameras of a sensor side, logged the
whole verdict at INFO inside a table, announced itself as "completed", and
described the fault to the operator as "Poor sensor contact" — the same
words a loose headband gets.

These tests pin the three halves of that:

* the verdict rows and the end banner must escalate when a camera reports
  ``no_signal`` (the documented triage grep is WARNING|ERROR|FAIL|aborted,
  so an INFO-only fault is invisible to log review);
* the banner must state the verdict rather than always saying "completed";
* ``no_signal`` must carry its own typeKey and operator string, because
  "reposition the headset" is the wrong instruction for a dark fiber.

No hardware — the connector fixture fakes the interface and runs the CQ
worker synchronously.
"""
import logging
import math
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.unit


@pytest.fixture
def connector():
    from motion_connector import MotionConnector

    fake_iface = MagicMock()
    fake_iface.console = MagicMock()
    fake_iface.left = MagicMock()
    fake_iface.right = MagicMock()
    fake_iface.is_device_connected.return_value = (True, True, True)
    fake_iface.scan_workflow = MagicMock()
    fake_iface.scan_workflow.running = False
    fake_iface.scan_workflow.config_running = False
    fake_iface.contact_quality_workflow = MagicMock()

    c = MotionConnector(
        interface=fake_iface,
        app_config={"engineeringMode": False},
        data_dir=".",
        config_dir="config",
    )
    c._consoleConnected = True
    c._leftSensorConnected = True
    c._rightSensorConnected = True
    return c


def _cam(reason, light_avg_dn=float("nan"), dark_max_dn=float("nan")):
    """A CamCQResult-alike. A bare MagicMock crashes the per-camera loop."""
    return SimpleNamespace(
        light_avg_dn=light_avg_dn, dark_max_dn=dark_max_dn, reason=reason,
    )


def _result(per_camera, passed):
    return SimpleNamespace(per_camera=per_camera, passed=passed)


def _dark_side_result():
    """The campaign's signature failure: the whole right side dark."""
    per_camera = {("left", i): _cam("ok", 67.8, -0.7) for i in range(8)}
    per_camera.update({("right", i): _cam("no_signal") for i in range(8)})
    return _result(per_camera, passed=False)


def _emitted(connector):
    box = {}
    connector.contactQualityCheckFinished.connect(
        lambda ok, err, warnings: box.update(ok=ok, err=err, warnings=warnings)
    )
    return box


# --- (a) log level -------------------------------------------------------

def test_no_signal_camera_escalates_above_info(connector, caplog):
    """A dark camera must be findable by the documented triage grep."""
    caplog.set_level(logging.DEBUG)

    connector._on_cq_result_ready(_dark_side_result())

    escalated = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert escalated, "total optical loss produced no record above INFO"
    assert any("R1" in r.getMessage() for r in escalated), (
        "no escalated record names an affected camera"
    )


def test_all_ok_result_stays_quiet(connector, caplog):
    """Regression fence: a clean check must not cry wolf."""
    caplog.set_level(logging.DEBUG)
    per_camera = {(side, i): _cam("ok", 67.8, -0.7)
                  for side in ("left", "right") for i in range(8)}

    connector._on_cq_result_ready(_result(per_camera, passed=True))

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# --- (b) end banner ------------------------------------------------------

def test_end_banner_states_a_failed_verdict(connector, caplog):
    """The banner said "completed" over eight dead cameras."""
    caplog.set_level(logging.DEBUG)

    connector._on_cq_result_ready(_dark_side_result())

    banners = [r.getMessage() for r in caplog.records
               if "Contact-quality check ended" in r.getMessage()]
    assert len(banners) == 1, f"expected one end banner, got {banners}"
    assert "completed" not in banners[0], (
        "a failing check still announces itself as completed"
    )


def test_end_banner_still_reports_a_passing_check(connector, caplog):
    caplog.set_level(logging.DEBUG)
    per_camera = {(side, i): _cam("ok", 67.8, -0.7)
                  for side in ("left", "right") for i in range(8)}

    connector._on_cq_result_ready(_result(per_camera, passed=True))

    banners = [r.getMessage() for r in caplog.records
               if "Contact-quality check ended" in r.getMessage()]
    assert len(banners) == 1
    assert "passed" in banners[0].lower()


def test_sdk_throw_is_still_distinguishable_from_a_failed_verdict(connector, caplog):
    """result is None means the workflow errored — a third state, not a
    failed verdict. Collapsing the two loses the distinction."""
    caplog.set_level(logging.DEBUG)

    connector._on_cq_result_ready(None)

    banners = [r.getMessage() for r in caplog.records
               if "Contact-quality check ended" in r.getMessage()]
    assert len(banners) == 1
    assert "passed" not in banners[0].lower()


# --- (c) operator vocabulary ---------------------------------------------

def test_no_signal_gets_its_own_type_key(connector):
    """"Poor sensor contact" sends the operator to reposition the headset
    for ten minutes instead of plugging a fiber back in."""
    box = _emitted(connector)

    connector._on_cq_result_ready(_dark_side_result())

    keys = {w["typeKey"] for w in box["warnings"] if w["camera"].startswith("R")}
    assert keys == {"no_signal"}, f"no_signal collapsed into {keys}"


def test_no_signal_text_differs_from_poor_contact(connector):
    box = _emitted(connector)
    per_camera = {("left", 0): _cam("poor_contact", 14.5, -0.7),
                  ("right", 0): _cam("no_signal")}

    connector._on_cq_result_ready(_result(per_camera, passed=False))

    texts = {w["camera"]: w["typeText"] for w in box["warnings"]}
    assert texts["L1"] != texts["R1"], "both faults describe themselves identically"
    assert "signal" in texts["R1"].lower()


def test_poor_contact_is_unchanged(connector):
    """Regression fence: the normal reposition-the-headset path is the one
    the clinical loop is built around. Do not disturb it."""
    box = _emitted(connector)
    per_camera = {("left", 5): _cam("poor_contact", 14.49, -0.7)}

    connector._on_cq_result_ready(_result(per_camera, passed=False))

    assert box["warnings"] == [{
        "camera": "L6",
        "typeKey": "poor_contact",
        "typeText": "Poor sensor contact",
        "value": pytest.approx(14.49),
    }]


def test_no_signal_value_is_not_a_nan(connector):
    """NaN crosses into QML as a value that renders as "nan"; the existing
    code already floors it to 0.0. Keep that when the key splits."""
    box = _emitted(connector)

    connector._on_cq_result_ready(_result({("right", 0): _cam("no_signal")}, False))

    assert not math.isnan(box["warnings"][0]["value"])
