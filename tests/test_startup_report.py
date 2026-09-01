"""Unit tests for the startup diagnostics report (issue #527).

Covers the pure pieces: file inspection (present/valid/sha256, with the
strict-UTF-8 rule that makes a BOM'd file INVALID exactly like the real
loaders), per-key config provenance marking, and the top-level logger
which must never raise.
"""

import hashlib
import json
import logging

import pytest

from utils import startup_report

pytestmark = pytest.mark.unit


# --- inspect_file ------------------------------------------------------------

def test_inspect_valid_file(tmp_path):
    p = tmp_path / "a.json"
    payload = json.dumps({"x": 1}).encode("utf-8")
    p.write_bytes(payload)
    info = startup_report.inspect_file(p)
    assert info["present"] is True
    assert info["valid"] is True
    assert info["error"] is None
    assert info["size"] == len(payload)
    assert info["sha256"] == hashlib.sha256(payload).hexdigest()


def test_inspect_missing_file(tmp_path):
    info = startup_report.inspect_file(tmp_path / "nope.json")
    assert info["present"] is False
    assert info["valid"] is False
    assert info["error"] == "missing"
    assert info["sha256"] is None


def test_inspect_invalid_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_bytes(b"{not json")
    info = startup_report.inspect_file(p)
    assert info["present"] is True
    assert info["valid"] is False
    assert "invalid JSON" in info["error"]
    # Still fingerprinted — support can match the broken file to a source.
    assert info["sha256"] is not None


def test_inspect_bom_is_invalid(tmp_path):
    """A UTF-8 BOM must report INVALID: config_store/omotion.laser open
    with strict utf-8, so a BOM'd file silently falls back to defaults
    there (the PowerShell Set-Content -Encoding utf8 trap)."""
    p = tmp_path / "bom.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"x": 1}).encode("utf-8"))
    info = startup_report.inspect_file(p)
    assert info["present"] is True
    assert info["valid"] is False


def test_describe_file_states(tmp_path):
    good = tmp_path / "good.json"
    good.write_bytes(b"{}")
    line = startup_report.describe_file("good.json", good)
    assert "OK" in line and "sha256=" in line and str(good) in line

    missing = startup_report.describe_file("gone.json", tmp_path / "gone.json")
    assert "MISSING" in missing

    bad = tmp_path / "bad.json"
    bad.write_bytes(b"nope")
    line = startup_report.describe_file("bad.json", bad)
    assert "INVALID" in line and "sha256=" in line


# --- inventory_files ---------------------------------------------------------

def test_inventory_covers_all_deployed_files(monkeypatch):
    monkeypatch.delenv("OPENWATER_CONFIG_DIR", raising=False)
    inv = dict(startup_report.inventory_files())
    assert set(inv) == {
        "app_config.json",
        "tec_params.json",
        "laser_params.json",
        "laser_params_fault.json",
        "fpga_model.json",
    }
    # In a dev checkout every one of these ships and parses.
    for name, path in inv.items():
        info = startup_report.inspect_file(path)
        assert info["present"], f"{name} missing at {path}"
        assert info["valid"], f"{name} invalid at {path}: {info['error']}"


# --- config provenance -------------------------------------------------------

def test_provenance_markers(monkeypatch):
    monkeypatch.delenv("OPENMOTION_CLINICAL", raising=False)
    monkeypatch.delenv("OPENMOTION_PORTABLE", raising=False)
    defaults = {"a": 1, "b": 2, "c": 3, "clinicalMode": False}
    baseline = {"a": 1, "b": 20, "c": 3, "clinicalMode": True}   # b, clinical shipped
    merged = {"a": 1, "b": 20, "c": 30, "clinicalMode": True}    # c local
    marks = startup_report.config_provenance(merged, baseline, defaults)
    assert marks["a"] == ""
    assert marks["b"] == startup_report.MARK_SHIPPED
    assert marks["c"] == startup_report.MARK_LOCAL
    assert marks["clinicalMode"] == startup_report.MARK_SHIPPED


def test_provenance_env_override_wins(monkeypatch):
    monkeypatch.setenv("OPENMOTION_CLINICAL", "1")
    monkeypatch.setenv("OPENMOTION_PORTABLE", "1")
    defaults = {"clinicalMode": False, "portableMode": False}
    # main mutates baseline AND merged for env overrides, so value-diffing
    # alone would mislabel these as [shipped].
    baseline = {"clinicalMode": True, "portableMode": True}
    merged = {"clinicalMode": True, "portableMode": True}
    marks = startup_report.config_provenance(merged, baseline, defaults)
    assert marks["clinicalMode"] == startup_report.MARK_ENV
    assert marks["portableMode"] == startup_report.MARK_ENV


def test_merged_config_block_format(monkeypatch):
    monkeypatch.delenv("OPENMOTION_CLINICAL", raising=False)
    monkeypatch.delenv("OPENMOTION_PORTABLE", raising=False)
    defaults = {"alpha": 1, "beta": None}
    baseline = {"alpha": 2, "beta": None}
    merged = {"alpha": 2, "beta": [1, 2]}
    block = startup_report.merged_config_block(merged, baseline, defaults)
    lines = block.splitlines()
    assert lines[0].strip().startswith("alpha = 2")
    assert startup_report.MARK_SHIPPED in lines[0]
    assert "beta" in lines[1] and "[1, 2]" in lines[1]
    assert startup_report.MARK_LOCAL in lines[1]


# --- log_startup_report ------------------------------------------------------

def _report_logger(caplog):
    log = logging.getLogger("test-startup-report")
    caplog.set_level(logging.INFO, logger="test-startup-report")
    return log


def test_log_startup_report_smoke(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("OPENMOTION_CLINICAL", raising=False)
    monkeypatch.delenv("OPENMOTION_PORTABLE", raising=False)
    log = _report_logger(caplog)
    defaults = {"clinicalMode": False, "portableMode": False, "k": 1}
    startup_report.log_startup_report(log, dict(defaults), dict(defaults), defaults)
    text = caplog.text
    assert "Build variant:  Research" in text
    assert "Install mode:" in text
    assert "dev (running from source)" in text
    # SDK identity: version stamp + the resolved package path (the stamp
    # alone lies on editable installs — the path is the truth-teller).
    import omotion
    from pathlib import Path
    sdk_dir = str(Path(omotion.__file__).resolve().parent)
    assert "SDK:" in text and sdk_dir in text
    assert "Qt runtime:" in text and "PyQt6" in text
    assert "Config overrides file:" in text
    for name in ("app_config.json", "tec_params.json", "laser_params.json",
                 "laser_params_fault.json", "fpga_model.json"):
        assert name in text
    assert "Merged app config" in text


def test_log_startup_report_clinical_variant(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path))
    log = _report_logger(caplog)
    cfg = {"clinicalMode": True, "portableMode": False}
    startup_report.log_startup_report(log, cfg, dict(cfg), dict(cfg))
    assert "Build variant:  Clinical" in caplog.text


def test_log_startup_report_never_raises(tmp_path, monkeypatch, caplog):
    """Logging must not take down the launch: an internal failure becomes
    a warning, and non-JSON-serializable values fall back to repr."""
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path))
    log = _report_logger(caplog)
    caplog.set_level(logging.WARNING, logger="test-startup-report")

    def boom():
        raise RuntimeError("inventory exploded")

    monkeypatch.setattr(startup_report, "inventory_files", boom)
    startup_report.log_startup_report(log, {"k": object()}, {}, {})
    assert "Startup report failed" in caplog.text


def test_fmt_value_falls_back_to_repr():
    # default=str serializes arbitrary objects...
    assert startup_report._fmt_value(object()).startswith('"<object')
    # ...but a circular structure raises even with default=str → repr path.
    d = {}
    d["self"] = d
    assert startup_report._fmt_value(d) == repr(d)
