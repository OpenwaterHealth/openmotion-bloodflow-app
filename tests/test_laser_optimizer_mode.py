"""Laser optimizer mode (#746, epic #745): the app-side gate, the scan request
the connector hands the SDK optimizer, and the unwind when the optimizer ends
without a scan. The SDK workflow itself is tested in openmotion-sdk
(tests/test_laser_optimizer.py); here the hardware seam is
``interface.start_laser_optimizer``."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import laser_optimizer_mode as lom

pytestmark = pytest.mark.unit

ENG_RESEARCH = {"engineeringMode": True, "clinicalMode": False}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cfg, sdk, fragment", [
    ({"engineeringMode": True, "clinicalMode": True}, True, "clinical"),
    ({"engineeringMode": False}, True, "engineering mode"),
    (ENG_RESEARCH, False, "SDK"),
    ({**ENG_RESEARCH, "forceLaserFail": True}, True, "Force laser fail"),
])
def test_refusal_reasons(cfg, sdk, fragment):
    assert fragment in lom.refusal(cfg, sdk_supported=sdk)


def test_engineering_research_build_may_start():
    assert lom.refusal(ENG_RESEARCH, sdk_supported=True) is None


@pytest.mark.parametrize("value, expected", [
    (10, 10), (30, 30), (1, 1), (3600, 3600), (20.0, 20),
    (None, 10), (0, 10), (-5, 10), (3601, 10), (2.5, 10),
    (True, 10), ("10", 10), (float("nan"), 10),
])
def test_scan_duration_validation(value, expected):
    assert lom.scan_duration_sec({"laserOptimizerScanDurationSec": value}) == expected


def test_compiled_default_is_ten_seconds_and_persisted():
    from config import app_config
    assert app_config.APP_CONFIG["laserOptimizerScanDurationSec"] == 10
    assert "laserOptimizerScanDurationSec" in app_config.PREFERENCE_KEYS
    assert lom.DURATION_DEFAULT_SEC in lom.DURATION_CHOICES_SEC


def test_insufficient_signal_text_asks_to_reposition():
    text = lom.outcome_text("insufficient_signal", "near mean 40.0 DN at the brightest setting")
    assert text.startswith("Not enough light") and "Retry" in text


def test_sdk_probe_row_and_notes_block():
    pytest.importorskip("omotion.LaserOptimizerWorkflow")
    from omotion.LaserOptimizerWorkflow import (
        Action, CameraReading, Decision, OptimizerSetting, ProbeResult,
    )
    cam = CameraReading("right", 3, 150.0, 0.002, 72.0, 30)
    probe = ProbeResult(2, OptimizerSetting(8, 2000), [cam], [], 0.01)
    row = lom.probe_row(probe, Decision(Action.PROBE, OptimizerSetting(8, 1339), "0.67x"))
    assert row["probe"] == 2 and row["setting"] == "8x / 2000 µs"
    assert row["meanDn"] == 150.0 and row["camera"] == "Right 4"
    assert row["clippedPct"] == pytest.approx(0.2)
    assert row["action"] == "probe" and row["next"] == "8x / 1339 µs"
    assert "index" not in row   # a ListModel role named index breaks delegates

    text = lom.notes_block([probe], OptimizerSetting(8, 1339))
    assert "8x / 1339 µs" in text and "probe 2" in text


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------

def _make_connector(tmp_path, app_config=None):
    from motion_connector import MotionConnector

    iface = MagicMock()
    iface.is_device_connected.return_value = (True, False, True)
    iface.scan_workflow = MagicMock()
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.laser_optimizer_running = False
    iface.start_laser_optimizer.return_value = True
    iface.scan_db_path = None
    c = MotionConnector(
        interface=iface,
        app_config=dict(app_config or ENG_RESEARCH),
        data_dir=str(tmp_path),
        config_dir="config",
    )
    c._consoleConnected = True
    c._leftSensorConnected = False
    c._rightSensorConnected = True
    c._laser_optimizer_sdk = True
    c._sync_alt_scan_settings = MagicMock()
    return c


def test_button_hidden_unless_engineering_research(tmp_path):
    assert _make_connector(tmp_path).laserOptimizerAvailable is True
    plain = _make_connector(tmp_path, {"engineeringMode": False, "clinicalMode": False})
    assert plain.laserOptimizerAvailable is False
    # Force laser fail refuses the start but keeps the button visible.
    armed = _make_connector(tmp_path, {**ENG_RESEARCH, "forceLaserFail": True})
    assert armed.laserOptimizerAvailable is True


def test_refused_outside_engineering_mode(tmp_path):
    c = _make_connector(tmp_path, {"engineeringMode": False, "clinicalMode": False})
    assert c.startLaserOptimizerCapture("hoof", 0x00, 0xFF) is False
    c._interface.start_laser_optimizer.assert_not_called()
    c._interface.start_scan.assert_not_called()


def test_request_handed_to_the_sdk_optimizer(tmp_path):
    pytest.importorskip("omotion.LaserOptimizerWorkflow")
    from omotion.contact_quality import ContactQualityMonitor

    c = _make_connector(tmp_path, {**ENG_RESEARCH, "laserOptimizerScanDurationSec": 30})
    assert c.startLaserOptimizerCapture("hoof", 0x99, 0xFF) is True

    c._interface.start_scan.assert_not_called()
    c._interface.start_laser_optimizer.assert_called_once()
    request = c._interface.start_laser_optimizer.call_args.args[0]
    final = request.final_scan
    assert final.duration_sec == 30 and final.subject_id == "hoof"
    # Disconnected left side is recorded as no cameras (#352 gate still applies).
    assert final.left_camera_mask == 0x00 and final.right_camera_mask == 0xFF
    assert final.disable_laser is False
    assert not any(isinstance(s, ContactQualityMonitor) for s in final.sinks)
    assert final.sinks, "the live-plot / DB / completion sinks stay"
    assert request.console_lock is c._console_mutex
    # The optimizer owns the alt registers; the next normal scan rewrites
    # the cameras in case the process dies before the SDK restore.
    c._sync_alt_scan_settings.assert_not_called()
    assert c._app_config["altCameraSettingsDirty"] is True


def test_normal_scan_still_starts_directly(tmp_path):
    c = _make_connector(tmp_path)
    c._interface.start_scan.return_value = True
    assert c.startCapture("subj", 5, 0x00, 0xFF, False) is True
    c._interface.start_scan.assert_called_once()
    c._interface.start_laser_optimizer.assert_not_called()
    c._sync_alt_scan_settings.assert_called_once()


def test_sdk_refusal_unwinds_capture_state(tmp_path):
    pytest.importorskip("omotion.LaserOptimizerWorkflow")
    c = _make_connector(tmp_path)
    c._interface.start_laser_optimizer.return_value = False
    c._interface.laser_optimizer_workflow.last_result = SimpleNamespace(
        message="no near camera (positions 4/5) is active on a connected sensor")
    outcomes = []
    c.laserOptimizerFinished.connect(lambda o, t: outcomes.append((o, t)))
    assert c.startLaserOptimizerCapture("hoof", 0x00, 0xC3) is False
    assert c._capture_running is False
    # The modal gets the reason, not just the toast.
    assert outcomes and outcomes[0][0] == "error" and "near camera" in outcomes[0][1]


def test_outcome_without_a_scan_finishes_the_capture(tmp_path):
    c = _make_connector(tmp_path)
    c._capture_running = True
    c._capture_start_time = 0.0
    c._capture_subject_id = "hoof"
    finished, outcomes = [], []
    c.captureFinished.connect(lambda ok, err, l, r: finished.append((ok, err)))
    c.laserOptimizerFinished.connect(lambda o, t: outcomes.append((o, t)))
    result = SimpleNamespace(
        outcome=SimpleNamespace(value="insufficient_signal"),
        message="near mean 40.0 DN at the brightest setting (16x / 2000 µs) is below 85 DN",
        restored=True, scan_ran=False,
    )
    c._on_laser_optimizer_complete(result)
    assert c._capture_running is False
    assert outcomes[0][0] == "insufficient_signal"
    assert finished == [(False, outcomes[0][1])]
    assert "Reposition" in outcomes[0][1]


def test_cancel_before_the_scan_reports_canceled(tmp_path):
    c = _make_connector(tmp_path)
    c._capture_running = True
    c._capture_start_time = 0.0
    c._capture_subject_id = "hoof"
    finished = []
    c.captureFinished.connect(lambda ok, err, l, r: finished.append((ok, err)))
    c._on_laser_optimizer_complete(SimpleNamespace(
        outcome=SimpleNamespace(value="canceled"), message="stopped before the scan",
        restored=True, scan_ran=False))
    assert finished == [(False, "Canceled")]


def test_completed_scan_leaves_the_capture_to_its_own_completion(tmp_path):
    c = _make_connector(tmp_path)
    c._capture_running = True
    finished = []
    c.captureFinished.connect(lambda *a: finished.append(a))
    c._on_laser_optimizer_complete(SimpleNamespace(
        outcome=SimpleNamespace(value="completed"), message="scanned at 8x / 1339 µs",
        restored=True, scan_ran=True))
    assert finished == [] and c._capture_running is True


def test_nothing_starts_while_the_optimizer_runs(tmp_path):
    c = _make_connector(tmp_path)
    c._interface.laser_optimizer_running = True
    assert c._ensure_idle() == "Laser optimizer still running"
    assert c.isPipelineIdle() is False


def test_stop_also_stops_the_optimizer(tmp_path):
    c = _make_connector(tmp_path)
    c.stopCapture()
    c._interface.cancel_laser_optimizer.assert_called_once()
    c._interface.cancel_scan.assert_called_once()
