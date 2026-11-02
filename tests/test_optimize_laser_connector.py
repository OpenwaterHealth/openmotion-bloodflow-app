"""Optimize Laser in the connector: the run slot and its gates, the active
settings every scan and check re-applies, the restore, and the clinical
compile-out. The SDK workflow is a fake and worker threads run inline, so
no hardware and no event loop are needed.
"""

import dataclasses
import re
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import motion_connector
import optimize_laser as ol
from motion_connector import MotionConnector
from omotion.OptimizeLaserWorkflow import OptimizeResult
from omotion.exposure.controller import (
    CameraRecord,
    CameraStatus,
    ExposureSettings,
    IterationRecord,
    OptimizeStatus,
)
from omotion.exposure.settings import OptimizeLaserSettings

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
RESEARCH_ENG = {"clinicalMode": False, "engineeringMode": True}
OVERRIDES = {"trigger_config": {"LaserPulseSkipDelayUsec": 2400, "LaserPulseWidthUsec": 1901},
             "camera_gains": "gains", "calibration_override": {"i_max": 400.0}}


@pytest.fixture(autouse=True)
def _no_config_writes(monkeypatch):
    """_save_app_config persists nothing here (no settings store)."""
    from utils import config_store
    monkeypatch.setattr(config_store, "persistable_diff", lambda cur, base: ({}, []))


class _InlineThread:
    def __init__(self, target=None, daemon=None, name=None, **_):
        self._target = target
        self.name = name

    def start(self):
        self._target()

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass


class _InlineThreading:
    """The connector's ``threading`` with Thread running inline."""
    Thread = _InlineThread

    def __getattr__(self, name):
        return getattr(threading, name)


@pytest.fixture
def inline_threads(monkeypatch):
    monkeypatch.setattr(motion_connector, "threading", _InlineThreading())


def _result(passed=True, status=OptimizeStatus.PARTIAL):
    cam = CameraRecord(gain=16.0, gain_code=256, dark_dn=128.0, mean_raw=328.0, signal=200.0,
                       kind="fit", sat_frac=0.0, points=1, points_in_range=1, on_target=True,
                       desired_factor=1.0, status=CameraStatus.ON_TARGET)
    settings = ExposureSettings(gain_codes={(0, 0): 256}, gains={(0, 0): 16.0},
                                pulse_width_us=1901.0, current_ma=5000.0)
    record = IterationRecord(iteration=1, settings=settings, cameras={(0, 0): cam},
                             next_settings=settings, done=True, status=status)
    return OptimizeResult(config=OptimizeLaserSettings(), status=status,
                          settings=settings if passed else None, exposure_us=2196.0,
                          cameras=((0, 0),), records=(record,), restored=not passed)


class FakeWorkflow:
    def __init__(self, result=None, run_error=None, reapply_error=None, restore_problems=()):
        self.result = result
        self.run_error = run_error
        self.reapply_error = reapply_error
        self.restore_problems = list(restore_problems)
        self.calls = []

    def run(self, settings, **kw):
        self.calls.append(("run", settings, kw))
        if self.run_error:
            raise self.run_error
        kw["on_iteration"](self.result.records[-1])
        return self.result

    def reapply(self, result):
        self.calls.append(("reapply", result))
        if self.reapply_error:
            raise RuntimeError(self.reapply_error)

    def scan_overrides(self, result):
        return dict(OVERRIDES)

    def restore_defaults(self, left, right):
        self.calls.append(("restore", left, right))
        return list(self.restore_problems)


def _connector(tmp_path, cfg=None, workflow=None):
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = None
    c = MotionConnector(interface=iface, app_config=dict(cfg if cfg is not None else RESEARCH_ENG),
                        data_dir=str(tmp_path), config_dir="config")
    c._leftSensorConnected = True
    c._rightSensorConnected = False
    c._optimize_workflow = workflow or FakeWorkflow(_result())
    return c


def _record(signal):
    got = []
    signal.connect(lambda *args: got.append(args))
    return got


# ---- the run --------------------------------------------------------------

def test_research_source_tree_binds_the_module():
    assert motion_connector.optimize_laser is ol


def test_idle_gate_reports_a_run_and_a_restore(tmp_path):
    c = _connector(tmp_path)
    assert c._ensure_idle() is None
    c._optimize_running = True
    assert "Optimize Laser" in c._ensure_idle()
    c._optimize_running = False
    c._laser_restore_running = True
    assert "restored" in c._ensure_idle()


@pytest.mark.parametrize("cfg, fragment", [
    ({"clinicalMode": False, "engineeringMode": False}, "engineering mode"),
    ({**RESEARCH_ENG, "forceLaserFail": True}, "Force laser fail"),
    ({**RESEARCH_ENG, "altLaserPulseWidthEnabled": True}, "alternative"),
    ({**RESEARCH_ENG, ol.SETTINGS_KEY: {"pulseWidthBounds": [900, 100]}}, "invalid"),
])
def test_refusals_report_and_start_nothing(tmp_path, cfg, fragment):
    wf = FakeWorkflow(_result())
    c = _connector(tmp_path, cfg, wf)
    finished = _record(c.optimizeLaserFinished)
    assert c.optimizeLaser(0xFF, 0xFF) is False
    assert len(finished) == 1 and finished[0][0] is False and fragment in finished[0][1]
    assert wf.calls == [] and not c.optimizeLaserRunning


def test_no_cameras_on_a_connected_sensor_refuses(tmp_path):
    c = _connector(tmp_path)
    finished = _record(c.optimizeLaserFinished)
    assert c.optimizeLaser(0x00, 0xFF) is False     # right sensor is not connected
    assert "No cameras" in finished[0][1]


def test_clinical_build_constant_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(motion_connector, "_CLINICAL_BUILD", True)
    c = _connector(tmp_path)
    assert not c.optimizeLaserAvailable
    assert "clinical" in c.optimizeLaserRefusal()


def test_passed_run_activates_its_settings(tmp_path, inline_threads):
    wf = FakeWorkflow(_result())
    c = _connector(tmp_path, {**RESEARCH_ENG, ol.SETTINGS_KEY: {"targetMean": 250}}, wf)
    progress = _record(c.optimizeLaserProgress)
    finished = _record(c.optimizeLaserFinished)
    assert c.optimizeLaser(0xFF, 0xFF) is True
    _, settings, kw = wf.calls[0]
    assert settings.target_mean == 250
    # The disconnected right side is masked out; the log lands under data/.
    assert (kw["left_camera_mask"], kw["right_camera_mask"]) == (0xFF, 0x00)
    assert Path(kw["log_path"]).parent == Path(c._data_root) / "optimize-laser"
    assert progress and progress[0][0]["iteration"] == 1
    assert finished[0][0] is True and finished[0][2]["status"] == "partial"
    assert c.optimizeLaserActive and not c.optimizeLaserRunning
    assert c.optimizeLaserSummary["pulseWidthUs"] == 1901.0


def test_a_new_run_starts_from_the_active_gains(tmp_path, inline_threads):
    wf = FakeWorkflow(_result())
    c = _connector(tmp_path, workflow=wf)
    c.optimizeLaser(0x01, 0)
    c.optimizeLaser(0x01, 0)
    assert wf.calls[0][2]["initial_gains"] is None
    assert wf.calls[1][2]["initial_gains"] == {(0, 0): 16.0}


def test_failed_run_leaves_nothing_active(tmp_path, inline_threads):
    c = _connector(tmp_path, workflow=FakeWorkflow(
        _result(passed=False, status=OptimizeStatus.NOT_CONVERGED)))
    finished = _record(c.optimizeLaserFinished)
    c.optimizeLaser(0xFF, 0)
    assert finished[0][0] is False and "Defaults restored" in finished[0][1]
    assert not c.optimizeLaserActive


def test_workflow_exception_reports_failure(tmp_path, inline_threads):
    c = _connector(tmp_path, workflow=FakeWorkflow(run_error=RuntimeError("boom")))
    finished = _record(c.optimizeLaserFinished)
    c.optimizeLaser(0xFF, 0)
    assert finished[0][:2] == (False, "Optimize Laser failed: boom")
    assert not c.optimizeLaserActive and not c.optimizeLaserRunning


class AskingWorkflow(FakeWorkflow):
    """Hits the iteration limit and asks, like the SDK's run()."""

    def run(self, settings, **kw):
        self.calls.append(("run", settings, kw))
        self.answer = kw["on_not_converged"](self.result.records[-1])
        if self.answer == "accept":
            return dataclasses.replace(self.result, accepted=True)
        return _result(passed=False, status=OptimizeStatus.NOT_CONVERGED)


def _pump(until, timeout=5.0):
    """Run the Qt event loop (queued worker -> main signals) until ``until()``."""
    from PyQt6.QtCore import QCoreApplication
    deadline = time.monotonic() + timeout
    while not until():
        assert time.monotonic() < deadline, "timed out waiting"
        QCoreApplication.processEvents()
        time.sleep(0.01)


@pytest.mark.parametrize("answer, ok", [("accept", True), ("stop", False), ("nonsense", False)])
def test_not_converged_asks_the_operator(tmp_path, answer, ok):
    wf = AskingWorkflow(_result(status=OptimizeStatus.NOT_CONVERGED))
    c = _connector(tmp_path, workflow=wf)
    asked = _record(c.optimizeLaserDecisionNeeded)
    finished = _record(c.optimizeLaserFinished)
    assert c.optimizeLaser(0xFF, 0) is True
    _pump(lambda: asked)
    assert c.optimizeLaserWaiting and "Keep iterating" in asked[0][0]["question"]
    c.answerOptimizeLaser(answer)
    _pump(lambda: finished)
    assert wf.answer == (answer if answer in ("accept", "stop") else "stop")
    assert finished[0][0] is ok and c.optimizeLaserActive is ok
    assert not c.optimizeLaserWaiting
    if ok:
        assert finished[0][2]["accepted"] is True
        assert finished[0][2]["runSeconds"] is not None


def test_cancel_answers_an_open_question_with_stop(tmp_path):
    wf = AskingWorkflow(_result(status=OptimizeStatus.NOT_CONVERGED))
    c = _connector(tmp_path, workflow=wf)
    asked = _record(c.optimizeLaserDecisionNeeded)
    finished = _record(c.optimizeLaserFinished)
    c.optimizeLaser(0xFF, 0)
    _pump(lambda: asked)
    c.cancelOptimizeLaser()
    _pump(lambda: finished)
    assert wf.answer == "stop" and finished[0][0] is False


def test_current_values_follow_the_settings_in_use(tmp_path):
    from omotion.exposure.actuator import LaserBaseline
    c = _connector(tmp_path)
    assert c.optimizeLaserCurrentValues["source"] == "default"
    c._optimize_baseline = LaserBaseline("1.8.1", 529.92, 5000.0, 5500.0, {})
    assert c.optimizeLaserCurrentValues["pulseWidthBounds"] == "530 µs"
    c._set_optimize_result(_result())
    v = c.optimizeLaserCurrentValues
    assert v["source"] == "optimized" and v["pulseWidthBounds"] == "1901 µs"


def test_a_run_remembers_the_consoles_own_values(tmp_path, inline_threads):
    from omotion.exposure.actuator import LaserBaseline
    base = LaserBaseline("1.8.1", 529.92, 5000.0, 5500.0, {})
    failed = dataclasses.replace(_result(passed=False, status=OptimizeStatus.NOT_CONVERGED),
                                 baseline=base)
    c = _connector(tmp_path, workflow=FakeWorkflow(failed))
    c.optimizeLaser(0xFF, 0)
    assert c._optimize_baseline is base
    assert c.optimizeLaserCurrentValues["currentBounds"] == "5000 mA"


def test_cancel_sets_the_stop_event(tmp_path):
    c = _connector(tmp_path)
    c._optimize_running = True
    c.cancelOptimizeLaser()
    assert c._optimize_stop.is_set()


def test_a_safety_trip_stops_the_run(tmp_path):
    c = _connector(tmp_path)
    c._optimize_running = True
    c._surface_safety_trip("EE: RATE_LOWER_LIMIT_FAIL")
    assert c._optimize_stop.is_set()


# ---- the active settings at each scan / check ------------------------------

def test_prepare_without_active_settings_does_nothing(tmp_path):
    wf = FakeWorkflow(_result())
    c = _connector(tmp_path, workflow=wf)
    assert c._prepare_optimized_scan(0xFF, 0) == (None, None)
    assert wf.calls == []


def test_prepare_reapplies_and_returns_the_overrides(tmp_path):
    wf = FakeWorkflow(_result())
    c = _connector(tmp_path, workflow=wf)
    c._set_optimize_result(_result())
    assert c._prepare_optimized_scan(0x01, 0) == (OVERRIDES, None)
    assert wf.calls[0][0] == "reapply"


def test_failed_reapply_refuses_the_scan(tmp_path):
    c = _connector(tmp_path, workflow=FakeWorkflow(reapply_error="RATE_LL write refused"))
    c._set_optimize_result(_result())
    overrides, err = c._prepare_optimized_scan(0x01, 0)
    assert overrides is None and "RATE_LL write refused" in err


def test_engineering_mode_off_drops_the_settings_with_a_restore(tmp_path):
    wf = FakeWorkflow(_result())
    c = _connector(tmp_path, workflow=wf)
    c._set_optimize_result(_result())
    c._optimize_masks = (0x81, 0)
    c._app_config["engineeringMode"] = False
    assert c._prepare_optimized_scan(0x81, 0) == (None, None)
    assert wf.calls == [("restore", 0x81, 0)]
    assert not c.optimizeLaserActive


def test_drop_with_a_failed_laser_restore_refuses_the_scan(tmp_path):
    c = _connector(tmp_path, workflow=FakeWorkflow(
        restore_problems=["laser power restore failed: EE_RATE_LL"]))
    c._set_optimize_result(_result())
    c._app_config["forceLaserFail"] = True
    overrides, err = c._prepare_optimized_scan(0x01, 0)
    assert overrides is None and "Power-cycle" in err


def test_alt_settings_are_skipped_while_optimized_settings_are_active(tmp_path):
    cfg = {**RESEARCH_ENG, "altCameraSettingsEnabled": True,
           "altCameraExposureUs": 648, "altCameraGains": [16, 4, 2, 1, 1, 2, 4, 16]}
    c = _connector(tmp_path, cfg)
    c._set_optimize_result(_result())
    c._sync_alt_scan_settings(0x01, 0)
    c._interface.left.camera_i2c_write.assert_not_called()
    # Control: without them the toggle writes the cameras.
    c._set_optimize_result(None)
    c._sync_alt_scan_settings(0x01, 0)
    assert c._interface.left.camera_i2c_write.called


def test_check_runs_with_the_optimized_trigger_config(tmp_path, inline_threads):
    c = _connector(tmp_path)
    c._prepare_optimized_scan = lambda left, right: (dict(OVERRIDES), None)
    c._interface.contact_quality_workflow.check.return_value = None
    c.runContactQualityCheck(0xFF, 0)
    kw = c._interface.contact_quality_workflow.check.call_args.kwargs
    assert kw["trigger_config"] == OVERRIDES["trigger_config"]


def test_check_without_active_settings_passes_no_trigger_config(tmp_path, inline_threads):
    c = _connector(tmp_path)
    c._interface.contact_quality_workflow.check.return_value = None
    c.runContactQualityCheck(0xFF, 0)
    assert "trigger_config" not in c._interface.contact_quality_workflow.check.call_args.kwargs


def test_check_refused_when_reapply_fails(tmp_path):
    c = _connector(tmp_path)
    c._prepare_optimized_scan = lambda left, right: (None, "could not be re-applied")
    finished = _record(c.contactQualityCheckFinished)
    c.runContactQualityCheck(0xFF, 0)
    assert finished == [(False, "could not be re-applied", [])]
    c._interface.contact_quality_workflow.check.assert_not_called()
    assert not c._cq_quick_running


def test_scan_refused_when_reapply_fails(tmp_path):
    c = _connector(tmp_path)
    c.checkStorageForScan = lambda: True
    c._prepare_optimized_scan = lambda left, right: (None, "could not be re-applied")
    assert c.startCapture("subj", 10, 0x01, 0, False) is False
    assert not c._capture_running
    c._interface.start_scan.assert_not_called()


def test_scan_carries_the_overrides_and_the_notes_block(tmp_path):
    c = _connector(tmp_path)
    c.checkStorageForScan = lambda: True
    c._set_optimize_result(_result())
    c._prepare_optimized_scan = lambda left, right: (dict(OVERRIDES), None)
    c._interface.start_scan.return_value = True
    assert c.startCapture("subj", 10, 0x01, 0, False) is True
    req = c._interface.start_scan.call_args.args[0]
    assert req.trigger_config == OVERRIDES["trigger_config"]
    assert req.camera_gains == "gains"
    assert req.calibration_override == {"i_max": 400.0}
    assert c._scan_notes.startswith("--- Optimize Laser settings")
    c.stopCapture()


# ---- settings form and restore --------------------------------------------

def test_settings_form_saves_only_valid_values(tmp_path):
    c = _connector(tmp_path)
    form = c.optimizeLaserSettings()
    assert form["values"] == ol.DEFAULT_FORM and form["problems"] == []
    assert c.setOptimizeLaserSettings({**ol.DEFAULT_FORM, "targetMean": 300}) == []
    assert c._app_config[ol.SETTINGS_KEY] == {"targetMean": 300.0}
    errors = c.setOptimizeLaserSettings({**ol.DEFAULT_FORM, "pulseWidthBounds": [100, 2200]})
    assert errors and c._app_config[ol.SETTINGS_KEY] == {"targetMean": 300.0}


def test_restore_uses_the_run_masks_and_clears_the_settings(tmp_path, inline_threads):
    wf = FakeWorkflow(_result())
    c = _connector(tmp_path, workflow=wf)
    c._set_optimize_result(_result())
    c._optimize_masks = (0x0F, 0x00)
    restored = _record(c.laserDefaultsRestored)
    assert c.restoreDefaultLaserSettings() is True
    assert wf.calls == [("restore", 0x0F, 0x00)]
    assert restored[0][0] is True and not c.optimizeLaserActive


def test_restore_without_a_run_covers_the_connected_sensors(tmp_path, inline_threads):
    wf = FakeWorkflow(_result(), restore_problems=["laser power restore failed: x"])
    c = _connector(tmp_path, workflow=wf)
    restored = _record(c.laserDefaultsRestored)
    c.restoreDefaultLaserSettings()
    assert wf.calls == [("restore", 0xFF, 0x00)]
    assert restored[0][0] is False


def test_restore_refused_while_busy(tmp_path):
    wf = FakeWorkflow(_result())
    c = _connector(tmp_path, workflow=wf)
    c._optimize_running = True
    restored = _record(c.laserDefaultsRestored)
    assert c.restoreDefaultLaserSettings() is False
    assert wf.calls == [] and restored[0][0] is False


class _SlowThread:
    """A worker still running at shutdown; finishes when joined."""
    name = "OptimizeLaser"

    def __init__(self):
        self.alive = True
        self.joined = None

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        self.joined = timeout
        self.alive = False


def test_shutdown_restores_active_settings(tmp_path):
    wf = FakeWorkflow(_result())
    c = _connector(tmp_path, workflow=wf)
    c._set_optimize_result(_result())
    c._optimize_masks = (0x81, 0x00)
    c.shutdown()
    assert wf.calls == [("restore", 0x81, 0x00)]
    assert c._optimize_result is None


def test_shutdown_without_active_settings_touches_nothing(tmp_path):
    wf = FakeWorkflow(_result())
    c = _connector(tmp_path, workflow=wf)
    c.shutdown()
    assert wf.calls == []


def test_shutdown_stops_a_run_and_waits_for_its_restore(tmp_path):
    wf = FakeWorkflow(_result())
    c = _connector(tmp_path, workflow=wf)
    c._optimize_running = True
    c._optimize_thread = _SlowThread()
    c._optimize_worker_result = _result(passed=False, status=OptimizeStatus.NOT_CONVERGED)
    c.shutdown()
    assert c._optimize_stop.is_set() and c._optimize_thread.joined == 30.0
    # The run restored itself; nothing more to do.
    assert wf.calls == []


def test_shutdown_restores_a_pass_the_main_thread_has_not_seen(tmp_path):
    """The worker returned a passed result, but its queued result signal
    never ran before the quit: the settings are on the device anyway."""
    wf = FakeWorkflow(_result())
    c = _connector(tmp_path, workflow=wf)
    c._optimize_masks = (0xFF, 0x00)
    c._optimize_thread = _SlowThread()
    c._optimize_worker_result = _result()
    c.shutdown()
    assert wf.calls == [("restore", 0xFF, 0x00)]


def test_restores_honor_force_laser_fail(tmp_path, monkeypatch):
    """The workflow's laser restore goes through apply_laser_power with the
    app's flag, like every other laser load (the sdk#252 class)."""
    import omotion.laser
    calls = []
    monkeypatch.setattr(omotion.laser, "apply_laser_power",
                        lambda console, force_fault=False, lock=None: calls.append(force_fault) or True)
    c = _connector(tmp_path)
    c._optimize_workflow = None
    c._force_laser_fail = True
    actuator = c._optimize_wf().actuator()
    actuator._restore_laser(actuator._console, lock=None)
    assert calls == [True]


# ---- compiled out of clinical builds ---------------------------------------

def test_connector_import_is_gated_on_the_compiled_constant():
    src = (REPO_ROOT / "motion_connector.py").read_text(encoding="utf-8")
    assert re.search(
        r"if _CLINICAL_BUILD:\n    optimize_laser = None\nelse:\n    try:\n"
        r"        import optimize_laser\n", src)
    assert src.count("import optimize_laser") == 1
    # The SDK's optimizer is reached only through optimize_laser.
    assert "from omotion.OptimizeLaserWorkflow" not in src
    assert "omotion.exposure" not in src
    assert "optimize_laser_workflow" not in src


def test_spec_and_nuitka_leave_it_out_of_clinical_builds():
    spec = (REPO_ROOT / "openwater.spec").read_text(encoding="utf-8")
    assert re.search(r'if _is_clinical:\n    _excludes \+= \["optimize_laser", '
                     r'"omotion\.OptimizeLaserWorkflow", "omotion\.exposure"\]', spec)
    ps1 = (REPO_ROOT / "scripts" / "build_nuitka.ps1").read_text(encoding="utf-8")
    for module in ("optimize_laser", "omotion.OptimizeLaserWorkflow", "omotion.exposure"):
        assert f"--nofollow-import-to={module}" in ps1


def test_optimize_laser_does_not_depend_on_the_connector():
    import ast
    tree = ast.parse((REPO_ROOT / "optimize_laser.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "motion_connector" not in imported
    assert not imported & {"PyQt6", "PySide6"}
