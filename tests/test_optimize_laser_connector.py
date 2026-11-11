"""Optimize Laser in the connector: the run slot and its gates, the active
settings the SDK keeps (and re-applies itself before every scan), the
restore, and the clinical compile-out. The SDK workflow is a fake and worker threads run inline, so
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
    def __init__(self, result=None, run_error=None, restore_problems=()):
        self.result = result
        self.run_error = run_error
        self.restore_problems = list(restore_problems)
        self.calls = []
        # What the SDK workflow owns: the active result, and the hooks the
        # connector attaches in _optimize_wf.
        self.active = None
        self.on_active_changed = None
        self.gate = None
        self.force_fault = None

    def set_active(self, result, reason=None):
        self.active = result
        if self.on_active_changed is not None:
            self.on_active_changed(reason)

    def run(self, settings, **kw):
        self.calls.append(("run", settings, kw))
        if self.run_error:
            raise self.run_error
        self.set_active(None)
        kw["on_iteration"](self.result.records[-1])
        if self.result.passed:
            self.set_active(self.result)
        return self.result

    def restore_defaults(self, left=None, right=None, *, reason=None):
        self.calls.append(("restore", left, right))
        self.set_active(None, reason)
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
    c._optimize_workflow.on_active_changed = c._optimizeActiveSignal.emit
    return c


def _activate(c, result=None):
    """Make ``result`` the SDK's active settings, as a passed run does."""
    result = result or _result()
    c._optimize_workflow.set_active(result)
    return result


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


@pytest.mark.parametrize("saved, status, seconds", [
    ({"scanAfterOptimize": True, "scanDurationSec": 15}, OptimizeStatus.ON_TARGET, 15),
    ({"scanAfterOptimize": True, "scanDurationSec": 15}, OptimizeStatus.PARTIAL, 15),
    ({"scanAfterOptimize": True, "scanDurationSec": 15}, OptimizeStatus.TOO_DIM_AT_MAX, 0),
    ({"scanDurationSec": 15}, OptimizeStatus.ON_TARGET, 0),
])
def test_the_result_says_whether_a_scan_follows(tmp_path, inline_threads, saved, status, seconds):
    c = _connector(tmp_path, {**RESEARCH_ENG, ol.SETTINGS_KEY: saved},
                   FakeWorkflow(_result(status=status)))
    finished = _record(c.optimizeLaserFinished)
    c.optimizeLaser(0xFF, 0)
    assert finished[0][0] is True and finished[0][2]["autoScanSec"] == seconds


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
            accepted = dataclasses.replace(self.result, accepted=True)
            self.set_active(accepted)
            return accepted
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
    _activate(c)
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

def test_no_active_settings_means_a_stock_scan(tmp_path):
    c = _connector(tmp_path)
    assert c._optimize_result is None and c._optimized_for_scan() is None


def test_active_settings_come_from_the_sdk_workflow(tmp_path):
    c = _connector(tmp_path)
    changed = _record(c.optimizeLaserStateChanged)
    result = _activate(c)
    assert c._optimize_result is result and c._optimized_for_scan() is result
    assert c.optimizeLaserActive and changed


def test_the_connector_attaches_its_policy_to_the_interface_workflow(tmp_path):
    """One workflow, the interface's: the gate, the Force laser fail flag for
    restores, and the state-change hook all go on it."""
    c = _connector(tmp_path)
    sdk_wf = FakeWorkflow(_result())
    c._interface.optimize_laser_workflow = sdk_wf
    c._optimize_workflow = None
    assert c._optimize_wf() is sdk_wf and c._optimize_wf() is sdk_wf

    assert sdk_wf.gate() is None
    c._app_config["engineeringMode"] = False
    assert "engineering mode" in sdk_wf.gate()
    c._app_config["engineeringMode"] = True
    c._app_config["forceLaserFail"] = True
    assert "Force laser fail" in sdk_wf.gate()

    assert sdk_wf.force_fault() is False
    c._force_laser_fail = True
    assert sdk_wf.force_fault() is True


def test_settings_the_gate_will_drop_do_not_shape_the_scan(tmp_path):
    """The SDK asks the same gate when the scan starts and restores the
    defaults, so the app must not add the notes block or turn live contact
    quality off for that scan."""
    c = _connector(tmp_path)
    _activate(c)
    c._app_config["engineeringMode"] = False
    assert c._optimize_result is not None and c._optimized_for_scan() is None


def test_a_gate_drop_by_the_sdk_tells_the_operator(tmp_path):
    c = _connector(tmp_path)
    _activate(c)
    notes = []
    c.notify = lambda text, *a, **k: notes.append(text)
    c._optimize_workflow.set_active(None, "Optimize Laser needs engineering mode.")
    assert not c.optimizeLaserActive
    assert len(notes) == 1 and "needs engineering mode" in notes[0]
    assert "default laser settings" in notes[0]


def test_alt_settings_are_skipped_while_optimized_settings_are_active(tmp_path):
    cfg = {**RESEARCH_ENG, "altCameraSettingsEnabled": True,
           "altCameraExposureUs": 648, "altCameraGains": [16, 4, 2, 1, 1, 2, 4, 16]}
    c = _connector(tmp_path, cfg)
    _activate(c)
    c._sync_alt_scan_settings(0x01, 0)
    c._interface.left.camera_i2c_write.assert_not_called()
    # Control: without them the toggle writes the cameras.
    c._optimize_workflow.set_active(None)
    c._sync_alt_scan_settings(0x01, 0)
    assert c._interface.left.camera_i2c_write.called


def test_check_leaves_the_optimized_trigger_to_the_sdk(tmp_path, inline_threads):
    c = _connector(tmp_path)
    _activate(c)
    c._interface.scan_workflow.last_scan_error = None
    c._interface.contact_quality_workflow.check.return_value = None
    c.runContactQualityCheck(0xFF, 0)
    assert "trigger_config" not in c._interface.contact_quality_workflow.check.call_args.kwargs


def test_check_without_active_settings_passes_no_trigger_config(tmp_path, inline_threads):
    c = _connector(tmp_path)
    c._interface.contact_quality_workflow.check.return_value = None
    c.runContactQualityCheck(0xFF, 0)
    assert "trigger_config" not in c._interface.contact_quality_workflow.check.call_args.kwargs


def test_check_reports_why_the_sdk_refused_its_scan(tmp_path, inline_threads):
    """A refused check scan has no frames, which would read as "no signal"
    on every camera; with optimized settings active the reason is shown."""
    c = _connector(tmp_path)
    _activate(c)
    c._interface.scan_workflow.last_scan_error = "could not be re-applied"
    c._interface.contact_quality_workflow.check.return_value = "an empty result"
    finished = _record(c.contactQualityCheckFinished)
    c.runContactQualityCheck(0xFF, 0)
    assert finished == [(False, "could not be re-applied", [])]
    assert not c._cq_quick_running


def test_scan_refused_by_the_sdk_does_not_start(tmp_path):
    c = _connector(tmp_path)
    c.checkStorageForScan = lambda: True
    _activate(c)
    c._interface.start_scan.return_value = False
    c._interface.scan_workflow.last_scan_error = "could not be re-applied"
    assert c.startCapture("subj", 10, 0x01, 0, False) is False
    assert not c._capture_running


def test_scan_at_optimized_settings_gets_the_notes_block_and_no_live_cq(tmp_path):
    c = _connector(tmp_path)
    c.checkStorageForScan = lambda: True
    _activate(c)
    c._interface.start_scan.return_value = True
    assert c.startCapture("subj", 10, 0x01, 0, False) is True
    req = c._interface.start_scan.call_args.args[0]
    # The trigger config, gains and calibration are the SDK's to add
    # (ScanWorkflow's scan preparer); the request leaves here without them.
    assert req.trigger_config is None and req.camera_gains is None
    assert req.calibration_override is None
    assert c._scan_notes.startswith("--- Optimize Laser settings")
    # 1.6.0 line: live contact quality is ScanRequest.contact_quality (#589).
    assert req.contact_quality is None
    c.stopCapture()


def test_stock_scans_keep_the_live_contact_quality_monitor(tmp_path):
    c = _connector(tmp_path)
    c.checkStorageForScan = lambda: True
    c._interface.start_scan.return_value = True
    assert c.startCapture("subj", 10, 0x01, 0, False) is True
    req = c._interface.start_scan.call_args.args[0]
    assert isinstance(req.contact_quality, motion_connector.CQSettings)
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
    assert c.setOptimizeLaserSettings(
        {**ol.DEFAULT_FORM, "scanAfterOptimize": True, "scanDurationSec": 25}) == []
    assert c._app_config[ol.SETTINGS_KEY] == {"scanAfterOptimize": True, "scanDurationSec": 25}


def test_restore_clears_the_settings(tmp_path, inline_threads):
    wf = FakeWorkflow(_result())
    c = _connector(tmp_path, workflow=wf)
    _activate(c)
    restored = _record(c.laserDefaultsRestored)
    assert c.restoreDefaultLaserSettings() is True
    # No masks: the SDK restores the active run's cameras.
    assert wf.calls == [("restore", None, None)]
    assert restored[0][0] is True and not c.optimizeLaserActive


def test_restore_without_a_run_reports_a_laser_problem(tmp_path, inline_threads):
    wf = FakeWorkflow(_result(), restore_problems=["laser power restore failed: x"])
    c = _connector(tmp_path, workflow=wf)
    restored = _record(c.laserDefaultsRestored)
    c.restoreDefaultLaserSettings()
    assert wf.calls == [("restore", None, None)]
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
    _activate(c)
    c.shutdown()
    assert wf.calls == [("restore", None, None)]
    assert c._optimize_result is None


def test_shutdown_without_active_settings_touches_nothing(tmp_path):
    wf = FakeWorkflow(_result())
    c = _connector(tmp_path, workflow=wf)
    c.shutdown()
    assert wf.calls == []


def test_quit_after_a_restore_does_not_restore_again(tmp_path, inline_threads):
    wf = FakeWorkflow(_result())
    c = _connector(tmp_path, workflow=wf)
    c.optimizeLaser(0xFF, 0)
    assert c.optimizeLaserActive is True
    assert c.restoreDefaultLaserSettings() is True
    assert c.optimizeLaserActive is False
    wf.calls.clear()
    c.shutdown()
    assert wf.calls == []


def test_quit_with_a_finished_run_still_restores(tmp_path, inline_threads):
    wf = FakeWorkflow(_result())
    c = _connector(tmp_path, workflow=wf)
    c.optimizeLaser(0xFF, 0)
    wf.calls.clear()
    c.shutdown()
    assert wf.calls == [("restore", None, None)]


def test_shutdown_stops_a_run_and_waits_for_its_restore(tmp_path):
    wf = FakeWorkflow(_result())
    c = _connector(tmp_path, workflow=wf)
    c._optimize_running = True
    c._optimize_thread = _SlowThread()
    c.shutdown()
    assert c._optimize_stop.is_set() and c._optimize_thread.joined == 30.0
    # The run restored itself; nothing more to do.
    assert wf.calls == []


def test_shutdown_restores_a_pass_the_main_thread_has_not_seen(tmp_path):
    """The worker returned a passed result, but its queued result signal
    never ran before the quit. The SDK made the settings active on the
    worker thread, so the shutdown sees them anyway."""
    wf = FakeWorkflow(_result())
    c = _connector(tmp_path, workflow=wf)
    c._optimize_thread = _SlowThread()
    wf.active = _result()               # set by the worker; no slot has run
    c.shutdown()
    assert wf.calls == [("restore", None, None)]


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
