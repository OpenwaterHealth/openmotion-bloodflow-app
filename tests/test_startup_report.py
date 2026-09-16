"""Unit tests for the startup diagnostics report (issue #527, reshaped by #546).

There is no config file to fingerprint any more; the report names the
compiled config (with a values fingerprint), the settings store, and marks
every key that deviates from the compiled value with its source.
"""

import logging

import pytest

from config import app_config as compiled
from utils import startup_report

pytestmark = pytest.mark.unit


# --- config provenance -------------------------------------------------------

def test_provenance_markers():
    baseline = {"a": 1, "b": 2, "c": 3, "d": 4}
    merged = {"a": 1, "b": 20, "c": 30, "d": 40}
    marks = startup_report.config_provenance(
        merged, baseline, dev_keys={"c"}, saved_keys={"b"})
    assert marks["a"] == ""
    assert marks["b"] == startup_report.MARK_SAVED
    assert marks["c"] == startup_report.MARK_DEV
    assert marks["d"] == startup_report.MARK_SAVED   # deviates from compiled → saved


def test_provenance_dev_flag_wins_over_value_diff():
    baseline = {"clinicalMode": True}
    merged = {"clinicalMode": True}
    marks = startup_report.config_provenance(merged, baseline, dev_keys={"clinicalMode"})
    assert marks["clinicalMode"] == startup_report.MARK_DEV


def test_provenance_ignores_env_vars(monkeypatch):
    """The retired OPENMOTION_* env vars are not a config source: with no
    dev_keys a value at its baseline reads as compiled, whatever the
    process environment says."""
    monkeypatch.setenv("OPENMOTION_CLINICAL", "1")
    baseline = {"clinicalMode": True}
    marks = startup_report.config_provenance(dict(baseline), baseline)
    assert marks["clinicalMode"] == ""


def test_merged_config_block_format():
    baseline = {"bfiMax": 10.0, "engineeringMode": False}
    merged = {"bfiMax": 5.0, "engineeringMode": True}
    block = startup_report.merged_config_block(merged, baseline, dev_keys={"engineeringMode"})
    lines = block.splitlines()
    assert lines[0].strip().startswith("bfiMax") and "= 5.0" in lines[0]
    assert "<preference>" in lines[0] and startup_report.MARK_SAVED in lines[0]
    assert lines[1].strip().startswith("engineeringMode") and "= true" in lines[1]
    assert "<session>" in lines[1] and startup_report.MARK_DEV in lines[1]


def test_compiled_fingerprint_is_stable_and_tracks_values(monkeypatch):
    a = startup_report.compiled_fingerprint()
    assert len(a) == 64 and a == startup_report.compiled_fingerprint()
    monkeypatch.setitem(compiled.APP_CONFIG, "bfiMax", 11.0)
    assert startup_report.compiled_fingerprint() != a


# --- log_startup_report ------------------------------------------------------

def _report_logger(caplog):
    log = logging.getLogger("test-startup-report")
    caplog.set_level(logging.INFO, logger="test-startup-report")
    return log


def test_log_startup_report_smoke(caplog):
    log = _report_logger(caplog)
    cfg = {"clinicalMode": False, "portableMode": False, "bfiMax": 10.0}
    startup_report.log_startup_report(
        log, dict(cfg), dict(cfg), settings_path="x/scans.db", settings_enabled=True)
    text = caplog.text
    assert "Build variant:  Research" in text
    assert "Install mode:" in text and "dev (running from source)" in text
    assert "Config:         compiled (config/app_config.py)" in text
    assert "values sha256=" in text
    import omotion
    from pathlib import Path
    sdk_dir = str(Path(omotion.__file__).resolve().parent)
    assert "SDK:" in text and sdk_dir in text
    assert "Qt runtime:" in text and "PyQt6" in text
    assert "Settings store: x/scans.db (open, 0 saved preference(s))" in text
    assert "Effective app config" in text
    assert "app_config.json" not in text


def test_log_startup_report_clinical_variant_and_unavailable_store(caplog):
    log = _report_logger(caplog)
    cfg = {"clinicalMode": True, "portableMode": False}
    startup_report.log_startup_report(
        log, cfg, dict(cfg), settings_path="y", settings_enabled=False)
    assert "Build variant:  Clinical" in caplog.text
    assert "UNAVAILABLE, preferences are session-only" in caplog.text


def test_log_startup_report_marks_dev_and_saved_keys(caplog):
    log = _report_logger(caplog)
    baseline = {"clinicalMode": False, "portableMode": False, "bfiMax": 10.0}
    merged = {"clinicalMode": True, "portableMode": False, "bfiMax": 5.0}
    startup_report.log_startup_report(
        log, merged, baseline, dev_keys={"clinicalMode"}, saved_keys={"bfiMax"})
    lines = caplog.text.splitlines()
    clinical = next(l for l in lines if l.strip().startswith("clinicalMode"))
    bfi = next(l for l in lines if l.strip().startswith("bfiMax"))
    assert startup_report.MARK_DEV in clinical
    assert startup_report.MARK_SAVED in bfi
    assert "app_config.local.json" not in caplog.text


def test_log_startup_report_never_raises(monkeypatch, caplog):
    """Logging must not take down the launch: an internal failure becomes
    a warning, and non-JSON-serializable values fall back to repr."""
    log = _report_logger(caplog)
    caplog.set_level(logging.WARNING, logger="test-startup-report")

    def boom(*a, **k):
        raise RuntimeError("fingerprint exploded")

    monkeypatch.setattr(startup_report, "compiled_fingerprint", boom)
    startup_report.log_startup_report(log, {"k": object()}, {})
    assert "Startup report failed" in caplog.text


def test_fmt_value_falls_back_to_repr():
    assert startup_report._fmt_value(object()).startswith('"<object')
    d = {}
    d["self"] = d
    assert startup_report._fmt_value(d) == repr(d)
