"""Optimize Laser app-side helpers (optimize_laser.py): the Settings form and
its validation, the run gates, and how results, iterations and the scan
notes read. Pure Python over the SDK's dataclasses; no hardware, no Qt.
"""

import dataclasses
import datetime
import math

import pytest

import optimize_laser as ol
from omotion.OptimizeLaserWorkflow import OptimizeResult
from omotion.exposure.controller import (
    CameraRecord,
    CameraStatus,
    ExposureSettings,
    IterationRecord,
    OptimizeStatus,
)
from omotion.exposure.settings import Bounds, GainMode, Knob, Method, OptimizeLaserSettings

pytestmark = pytest.mark.unit


def test_the_long_exposure_dark_skip_matches_the_sdk():
    """motion_config documents the 2400 us skip for every build, clinical
    included, so it can't import it from an SDK that may predate the
    constant. This keeps the two numbers from drifting apart."""
    import motion_config
    from omotion.OptimizeLaserWorkflow import DARK_SKIP_US
    assert motion_config.DARK_SKIP_HIGH_EXPOSURE_US == DARK_SKIP_US

RESEARCH_ENG = {"clinicalMode": False, "engineeringMode": True}


# ---- Settings form --------------------------------------------------------

class TestForm:
    def test_defaults_round_trip(self):
        settings, errors = ol.from_form(ol.DEFAULT_FORM)
        assert errors == []
        assert settings == ol.DEFAULT_SETTINGS
        assert {**ol.to_form(settings), **ol.SCAN_AFTER_DEFAULTS} == ol.DEFAULT_FORM

    def test_study_defaults(self):
        """Brad, 2026-11-02: 100 DN above background, +/- 10, 20 points;
        near priority (cameras 4 & 5)."""
        s = ol.DEFAULT_SETTINGS
        assert (s.target_mean, s.passing_range, s.in_range_points) == (100.0, 10.0, 20)
        assert s.gain_priority.value == "near"
        assert s.method.value == "fit"

    def test_labels(self):
        assert dict(ol.METHODS) == {
            "fit": "Histogram Fitting & Saturated Pixels",
            "basic": "Simple (image mean from raw histograms)"}
        assert dict(ol.GAIN_PRIORITIES) == {
            "near": "Average of Near (cameras 4 & 5)",
            "far": "Average of Far (cameras 1 & 8)",
            "none": "Average of All Cameras Selected"}

    def test_gains_round_to_the_nearest_sixteenth(self):
        settings, errors = ol.from_form({"gainBounds": [[1.03, 15.97], [1.1, 4.0], [2, 2], [1, 16]]})
        assert errors == []
        assert settings.gain_bounds[0] == Bounds(1.0, 16.0)
        assert settings.gain_bounds[1] == Bounds(1.125, 4.0)
        # Rounding never moves a gain into range: 17x is still an error.
        settings, errors = ol.from_form({"gainBounds": [[1, 17], [1, 16], [1, 16], [1, 16]]})
        assert settings is None and any("hardware maximum" in e for e in errors)

    def test_missing_keys_take_the_defaults(self):
        settings, errors = ol.from_form({"targetMean": 300, "gainMode": "row"})
        assert errors == []
        assert settings.target_mean == 300.0
        assert settings.gain_mode is GainMode.ROW
        assert settings.pulse_width_bounds == OptimizeLaserSettings().pulse_width_bounds

    def test_values_become_the_sdk_enums(self):
        settings, _ = ol.from_form({"method": "basic", "order": "current,gain,pulse_width"})
        assert settings.method is Method.BASIC
        assert settings.order == (Knob.CURRENT, Knob.GAIN, Knob.PULSE_WIDTH)

    def test_fractional_gain_steps_accepted(self):
        settings, errors = ol.from_form({"gainBounds": [[1.0625, 15.9375], [1, 16], [1, 16], [1, 1]]})
        assert errors == []
        assert settings.gain_bounds[0] == Bounds(1.0625, 15.9375)
        assert settings.gain_bounds[3].fixed

    @pytest.mark.parametrize("form, fragment", [
        ({"pulseWidthBounds": [800, 600]}, "above the upper bound"),
        ({"pulseWidthBounds": [100, 2200]}, "above the hardware maximum"),   # 2196 - 100
        ({"pulseWidthBounds": [50, 600]}, "below the hardware minimum"),
        ({"currentBounds": [2500, 6000]}, "above the hardware maximum"),
        ({"gainBounds": [[1, 16]] * 3}, "4 rows"),
        ({"method": "magic"}, "method"),
        ({"order": "gain,gain,current"}, "optimization order"),
        ({"order": ["gain", "pulse_width", "current"]}, "optimization order"),
        ({"targetMean": "200"}, "target mean must be a number"),
        ({"targetMean": float("nan")}, "target mean must be a number"),
        ({"passingRange": True}, "passing range must be a number"),
        ({"targetMean": 1015, "passingRange": 20}, "full scale"),
        ({"framesPerIteration": 1.5}, "whole number"),
        ({"inRangePoints": 0}, "at least 1"),
        ({"bogus": 1}, "unknown Optimize Laser settings"),
    ])
    def test_bad_values_are_errors_not_clamps(self, form, fragment):
        settings, errors = ol.from_form(form)
        assert settings is None
        assert any(fragment in e for e in errors), errors

    def test_non_mapping_rejected(self):
        settings, errors = ol.from_form(["targetMean", 200])
        assert settings is None and errors

    def test_storage_diff_keeps_only_changes(self):
        assert ol.storage_diff(ol.DEFAULT_SETTINGS) == {}
        settings, _ = ol.from_form({"targetMean": 250, "pulseWidthBounds": [100, 1500]})
        assert ol.storage_diff(settings) == {"targetMean": 250.0, "pulseWidthBounds": [100.0, 1500.0]}

    def test_stored_settings(self):
        assert ol.stored_settings({})[0] == ol.DEFAULT_SETTINGS
        assert ol.stored_settings({ol.SETTINGS_KEY: {}})[0] == ol.DEFAULT_SETTINGS
        s, errors = ol.stored_settings({ol.SETTINGS_KEY: {"targetMean": 300}})
        assert errors == [] and s.target_mean == 300
        # A bad save refuses rather than falling back to the defaults.
        s, errors = ol.stored_settings({ol.SETTINGS_KEY: {"pulseWidthBounds": [900, 100]}})
        assert s is None and errors
        s, errors = ol.stored_settings({ol.SETTINGS_KEY: "nonsense"})
        assert s is None and errors

    def test_choices(self):
        c = ol.choices()
        assert len(c["order"]) == 6
        assert {e["value"] for e in c["order"]} >= {"gain,pulse_width,current"}
        assert c["defaults"] == ol.DEFAULT_FORM
        assert c["limits"]["pulseWidthUs"] == [100.0, 2096.0]
        assert c["limits"]["currentMa"] == [2500.0, 5000.0]


class TestScanAfter:
    def test_off_by_default(self):
        assert ol.DEFAULT_FORM["scanAfterOptimize"] is False
        assert ol.DEFAULT_FORM["scanDurationSec"] == 10
        assert ol.scan_after_seconds({}) is None

    def test_saved_with_the_form(self):
        form = {**ol.DEFAULT_FORM, "scanAfterOptimize": True, "scanDurationSec": 30.0}
        settings, errors = ol.from_form(form)
        assert errors == []
        saved = ol.storage_diff(settings, form)
        assert saved == {"scanAfterOptimize": True, "scanDurationSec": 30}
        assert ol.scan_after_seconds({ol.SETTINGS_KEY: saved}) == 30
        # The duration alone doesn't turn it on.
        assert ol.scan_after_seconds({ol.SETTINGS_KEY: {"scanDurationSec": 30}}) is None

    @pytest.mark.parametrize("form, fragment", [
        ({"scanDurationSec": 0}, "outside 1 to 3600"),
        ({"scanDurationSec": 3601}, "outside 1 to 3600"),
        ({"scanDurationSec": 2.5}, "whole number"),
        ({"scanDurationSec": "10"}, "scan duration must be a number"),
        ({"scanAfterOptimize": 1}, "on or off"),
    ])
    def test_bad_values_are_errors(self, form, fragment):
        settings, errors = ol.from_form(form)
        assert settings is None and any(fragment in e for e in errors), errors

    @pytest.mark.parametrize("status, passed, scans", [
        (OptimizeStatus.ON_TARGET, True, True),
        (OptimizeStatus.PARTIAL, True, True),
        (OptimizeStatus.TOO_DIM_AT_MAX, True, False),
        (OptimizeStatus.TOO_BRIGHT_AT_MIN, True, False),
        (OptimizeStatus.SPREAD_TOO_WIDE, True, False),
        (OptimizeStatus.NOT_CONVERGED, False, False),
    ])
    def test_which_results_scan(self, status, passed, scans):
        result = _result(status=status, passed=passed)
        assert ol.scans_after(result) is scans
        assert ol.summary(result, scan_after_sec=15)["autoScanSec"] == (15 if scans else 0)

    def test_an_accepted_run_waits(self):
        accepted = dataclasses.replace(
            _result(status=OptimizeStatus.NOT_CONVERGED), accepted=True)
        assert accepted.passed and not ol.scans_after(accepted)
        assert ol.summary(accepted, scan_after_sec=15)["autoScanSec"] == 0

    def test_toggle_off_never_scans(self):
        assert ol.summary(_result(status=OptimizeStatus.ON_TARGET))["autoScanSec"] == 0


# ---- gates ----------------------------------------------------------------

class TestGates:
    def test_research_with_engineering_may_run(self):
        assert ol.refusal(RESEARCH_ENG) is None

    @pytest.mark.parametrize("cfg, fragment", [
        ({"clinicalMode": True, "engineeringMode": True}, "clinical"),
        ({"clinicalMode": False, "engineeringMode": False}, "engineering mode"),
        ({**RESEARCH_ENG, "forceLaserFail": True}, "Force laser fail"),
        ({**RESEARCH_ENG, "altCameraSettingsEnabled": True}, "alternative"),
        ({**RESEARCH_ENG, "altLaserPulseWidthEnabled": True}, "alternative"),
    ])
    def test_refusals(self, cfg, fragment):
        assert fragment in ol.refusal(cfg)

    def test_clinical_build_constant_refuses_even_with_research_config(self):
        assert "clinical" in ol.refusal(RESEARCH_ENG, clinical_build=True)

    def test_active_settings_survive_the_alt_toggles(self):
        # The toggles are overridden while optimized settings are active,
        # not a reason to drop them.
        assert ol.active_problem({**RESEARCH_ENG, "altCameraSettingsEnabled": True}) is None
        assert ol.active_problem({**RESEARCH_ENG, "engineeringMode": False})


# ---- results --------------------------------------------------------------

def _cam(gain, signal, status, on_target):
    return CameraRecord(gain=gain, gain_code=int(round(gain * 16)), dark_dn=128.0,
                        mean_raw=None if signal is None else signal + 128.0, signal=signal,
                        kind="fit", sat_frac=0.0, points=1, points_in_range=int(on_target),
                        on_target=on_target, desired_factor=1.0, status=status)


def _result(status=OptimizeStatus.PARTIAL, passed=True, pw=1901.0, current=5000.0):
    cams = {
        (0, 0): _cam(16.0, 200.7, CameraStatus.ON_TARGET, True),
        (0, 2): _cam(1.0, 240.9, CameraStatus.TOO_BRIGHT, False),
        (0, 3): _cam(1.0, 479.9, CameraStatus.TOO_BRIGHT, False),
        (1, 7): _cam(12.8125, 193.4, CameraStatus.ON_TARGET, True),
    }
    gains = {k: c.gain for k, c in cams.items()}
    settings = ExposureSettings(gain_codes={k: c.gain_code for k, c in cams.items()},
                                gains=gains, pulse_width_us=pw, current_ma=current)
    record = IterationRecord(iteration=3, settings=settings, cameras=cams,
                             next_settings=settings, done=True, status=status)
    return OptimizeResult(
        config=OptimizeLaserSettings(), status=status,
        settings=settings if passed else None, exposure_us=2196.0,
        cameras=tuple(sorted(cams)), records=(record,),
        csv_path="C:/data/optimize-laser/optimize-20261028_120643.csv",
        restored=not passed)


class TestResults:
    def test_iteration_row(self):
        row = ol.iteration_row(_result().records[-1])
        assert row["iteration"] == 3
        assert row["pulseWidthUs"] == 1901.0
        assert row["emittedPulseWidthUs"] == pytest.approx(1883.7)
        assert row["done"] is True and row["status"] == "partial"
        assert [c["camera"] for c in row["cameras"]] == ["L1", "L3", "L4", "R8"]
        assert row["cameras"][0] == {
            "camera": "L1", "side": "left", "position": 1, "gain": 16.0, "gainCode": 256,
            "mean": 328.7, "signal": 200.7, "dark": 128.0, "sat": 0.0, "onTarget": True,
            "status": "on_target"}

    def test_partial_summary_alerts_and_proceeds(self):
        s = ol.summary(_result())
        assert s["passed"] is True and s["status"] == "partial"
        assert s["alerts"] == ["L3, L4: too bright at minimum gain"]
        assert "Scans can go ahead" in s["message"]
        assert s["gains"] == {"L1": 16.0, "L3": 1.0, "L4": 1.0, "R8": 12.8125}
        assert s["csvName"] == "optimize-20261028_120643.csv"
        assert s["pulseWidthUs"] == 1901.0 and s["currentMa"] == 5000.0

    def test_on_target_message(self):
        assert "until you restore the defaults" in ol.summary(
            _result(status=OptimizeStatus.ON_TARGET))["message"]

    def test_failed_run_says_restored(self):
        s = ol.summary(_result(status=OptimizeStatus.NOT_CONVERGED, passed=False))
        assert s["passed"] is False
        assert s["message"].endswith("Defaults restored.")
        assert s["alerts"] == [] and s["gains"] == {}
        # The last iteration still shows what was tried.
        assert s["pulseWidthUs"] == 1901.0

    def test_error_and_cancel_messages(self):
        base = _result(passed=False)
        err = dataclasses.replace(base, status=None, error="scan refused")
        assert ol.message(err) == "Optimize Laser stopped: scan refused. Defaults restored."
        canceled = dataclasses.replace(base, status=None, canceled=True)
        assert ol.message(canceled) == "Optimize Laser canceled. Defaults restored."

    def test_notes_block(self):
        text = ol.notes_block(_result(), left_mask=0x0D, right_mask=0x81,
                              finished_at=datetime.datetime(2026, 10, 28, 12, 6, 57))
        assert text.startswith("--- Optimize Laser settings")
        assert "Optimized 2026-10-28 12:06:57: partial, 1 iteration(s), method fit" in text
        assert "Target: 200 +/- 20 DN image mean above background" in text
        assert "Exposure: 2196 us" in text
        assert "Laser pulse width: 1901 us commanded, ~1884 us emitted" in text
        assert "Laser drive current: 5000 mA" in text
        assert "Camera gains (left): 1: 16x, 3: 1x, 4: 1x" in text
        assert "Camera gains (right): 8: 12.8125x" in text
        assert "Dark-frame skip: 2400 us" in text
        assert "BVI I_max: 400 DN" in text
        assert "Live contact-quality warnings: off at these settings" in text
        assert "Optimize Laser log: optimize-20261028_120643.csv" in text
        assert "Not on target: L3, L4: too bright at minimum gain" in text
        # The scan uses R1, which the run did not optimize.
        assert text.endswith("Not optimized (sensor firmware settings): R1")

    def test_notes_with_the_scan_footer_have_one_separator(self):
        """The connector appends the block + a blank line; the scan footer
        adds its own "---" (bench, 2026-10-28: the block's closing rule
        doubled it)."""
        block = ol.notes_block(_result(), left_mask=0x0D, right_mask=0x80,
                               finished_at=datetime.datetime(2026, 10, 28, 13, 2, 3))
        notes = (block + "\n\n").strip() + "\n---\nScan completed — duration: 00:00:10"
        assert notes.splitlines().count("---") == 1

    def test_accepted_run(self):
        accepted = dataclasses.replace(
            _result(status=OptimizeStatus.NOT_CONVERGED), accepted=True)
        assert accepted.passed
        s = ol.summary(accepted, run_seconds=41.27)
        assert s["passed"] and s["accepted"] and s["runSeconds"] == 41.3
        assert s["message"].startswith("Did not settle within 1 iteration(s); the last settings "
                                       "were accepted")
        assert s["alerts"] == ["L3, L4: too bright at minimum gain"]
        text = ol.notes_block(accepted, left_mask=0x0D, right_mask=0x80,
                              finished_at=datetime.datetime(2026, 11, 2, 9, 0, 0))
        assert "not_converged (accepted without settling), 1 iteration(s)" in text

    def test_not_converged_question(self):
        q = ol.not_converged_question(_result().records[-1], ol.DEFAULT_SETTINGS)
        assert q["iteration"] == 3 and len(q["cameras"]) == 4
        assert q["question"] == (
            "The cameras have not settled within 10 DN of the 100 DN target (20 in-range "
            "point(s) in a row) after 3 iteration(s). Keep iterating, or accept the "
            "current settings?")

    def test_current_values_while_optimized(self):
        v = ol.current_values(_result())
        assert v["source"] == "optimized"
        # Row 1 = cameras 1 & 8 (L1 16x, R8 12.8125x); row 2 (2 & 7) wasn't run.
        assert v["gainBounds"] == ["12.8125–16×", "not optimized", "1×", "1×"]
        assert v["pulseWidthBounds"] == "1901 µs" and v["currentBounds"] == "5000 mA"
        assert v["exposure"] == "2196 µs"

    def test_current_values_at_the_defaults(self):
        v = ol.current_values(None)
        assert v["source"] == "default"
        assert v["gainBounds"] == ["16×", "4×", "2×", "1×"]
        assert v["pulseWidthBounds"] == "console default"
        assert v["exposure"] == "648 µs"
        # A failed run's preflight read the console's own values.
        from omotion.exposure.actuator import LaserBaseline
        base = LaserBaseline("1.8.1", 529.92, 5000.0, 5500.0, {})
        v = ol.current_values(_result(passed=False), base)
        assert (v["pulseWidthBounds"], v["currentBounds"]) == ("530 µs", "5000 mA")

    def test_unoptimized_cameras(self):
        r = _result()
        assert ol.unoptimized_cameras(r, 0x0D, 0x80) == []
        assert ol.unoptimized_cameras(r, 0xFF, 0x00) == ["L2", "L5", "L6", "L7", "L8"]

    @pytest.mark.parametrize("problems, ok, fragment", [
        ([], True, "Laser and camera defaults restored."),
        (["camera writes failed: left cam 1"], True, "reconfigured at the next Start"),
        (["laser power restore failed: EE_PULSE_WIDTH_UL"], False, "Power-cycle the console"),
        # A held 2400 us skip under the restored RATE_LL is a failed restore.
        (["trigger config reset failed: the console rejected the stock trigger config"],
         False, "Power-cycle the console"),
    ])
    def test_restore_message(self, problems, ok, fragment):
        got_ok, text = ol.restore_message(problems)
        assert got_ok is ok and fragment in text

    def test_csv_path(self, tmp_path):
        p = ol.csv_path(str(tmp_path), datetime.datetime(2026, 10, 28, 12, 6, 43))
        assert p == str(tmp_path / "optimize-laser" / "optimize-20261028_120643.csv")

    def test_emitted_pulse(self):
        assert math.isclose(ol.emitted_pulse_width_us(500.0), 482.7)
