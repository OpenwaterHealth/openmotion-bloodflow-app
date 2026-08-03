import json
import importlib
import sys

import pytest


@pytest.mark.unit
def test_load_app_config_applies_local_override(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path))
    (tmp_path / "app_config.local.json").write_text(
        json.dumps({"engineeringMode": True}), encoding="utf-8"
    )
    main = importlib.import_module("main")
    cfg = main._load_app_config()
    assert cfg["engineeringMode"] is True              # override applied over baseline
    # baseline is stashed for the connector (value comes from the shipped
    # config file, so assert presence, not a specific value).
    assert "engineeringMode" in main._APP_CONFIG_BASELINE


# --------------------------------------------------------------------------
# macOS is research-only (SDK refuses the scan-db keystore on darwin)
# --------------------------------------------------------------------------
#
# tmp_path is requested as a fixture so pytest materializes it during setup —
# resolving it *after* sys.platform is faked to "darwin" makes pytest take its
# POSIX branch and call os.getuid(), which does not exist on Windows.

def _config_with(tmp_path, monkeypatch, platform, overrides):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path))
    (tmp_path / "app_config.local.json").write_text(
        json.dumps(overrides), encoding="utf-8"
    )
    monkeypatch.setattr(sys, "platform", platform)
    main = importlib.import_module("main")
    return main, main._load_app_config()


@pytest.mark.unit
def test_macos_forces_research_mode(tmp_path, monkeypatch):
    """clinicalMode drives require_encrypted_db, and the SDK refuses the
    keystore on darwin — so a clinical macOS session cannot start at all."""
    _main, cfg = _config_with(tmp_path, monkeypatch, "darwin", {"clinicalMode": True})
    assert cfg["clinicalMode"] is False


@pytest.mark.unit
def test_macos_forces_research_mode_in_the_baseline_too(tmp_path, monkeypatch):
    # the connector reads the baseline, so leaving it True would still hand a
    # clinical flag to everything downstream of the merged config
    main, _cfg = _config_with(tmp_path, monkeypatch, "darwin", {"clinicalMode": True})
    assert main._APP_CONFIG_BASELINE["clinicalMode"] is False


@pytest.mark.unit
def test_macos_beats_the_clinical_env_override(tmp_path, monkeypatch):
    """OPENMOTION_CLINICAL=1 normally wins over the config. "macOS is never
    clinical" is absolute, so the platform gate has to win over it in turn —
    otherwise the env var just produces a crash instead of a research build."""
    monkeypatch.setenv("OPENMOTION_CLINICAL", "1")
    _main, cfg = _config_with(tmp_path, monkeypatch, "darwin", {})
    assert cfg["clinicalMode"] is False


@pytest.mark.unit
def test_non_macos_still_honors_clinical_mode(tmp_path, monkeypatch):
    # the gate must not leak off darwin — Windows is the clinical platform
    _main, cfg = _config_with(tmp_path, monkeypatch, "win32", {"clinicalMode": True})
    assert cfg["clinicalMode"] is True


@pytest.mark.unit
def test_non_macos_still_honors_the_clinical_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMOTION_CLINICAL", "1")
    _main, cfg = _config_with(tmp_path, monkeypatch, "win32", {"clinicalMode": False})
    assert cfg["clinicalMode"] is True
