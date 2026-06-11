"""Unit tests for _load_app_config resolution paths — issue #154.

The startup camera-FPGA auto-flash (`autoConfigureOnStartup`) used to
keep the Start button orange/disabled for ~2 minutes while every camera
FPGA was programmed sequentially — redundant work, since ScanRunner runs
FlashSensorsTask unconditionally on every Start/Check. The flag and the
startup-flash machinery were removed entirely; these tests pin the
config-loader fallback behavior and tombstone the removed flag so it is
never silently reintroduced.
"""

import json
from pathlib import Path

import pytest

import main as app_main

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]


def _patch_config_path(monkeypatch, path):
    """Point main._load_app_config at ``path`` for config/app_config.json."""
    real_resource_path = app_main.resource_path

    def fake_resource_path(*parts):
        if parts == ("config", "app_config.json"):
            return path
        return real_resource_path(*parts)

    monkeypatch.setattr(app_main, "resource_path", fake_resource_path)


def test_missing_config_falls_back_to_defaults(tmp_path, monkeypatch):
    """No app_config.json at all -> pure in-code defaults."""
    _patch_config_path(monkeypatch, tmp_path / "app_config.json")
    cfg = app_main._load_app_config()
    assert cfg["leftMask"] == 0x66
    assert cfg["developerMode"] is False


def test_corrupt_config_falls_back_to_defaults(tmp_path, monkeypatch):
    """Corrupt JSON falls back to in-code defaults instead of crashing."""
    config_path = tmp_path / "app_config.json"
    config_path.write_text("{not valid json", encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    cfg = app_main._load_app_config()
    assert cfg["leftMask"] == 0x66
    assert cfg["developerMode"] is False


def test_unknown_keys_are_dropped(tmp_path, monkeypatch):
    """Keys not in the in-code defaults are filtered out on load.

    This is what retires removed flags in the field: a persisted config
    that still carries `autoConfigureOnStartup` from an older build loses
    it on load, so no QML/Python code can ever see it again.
    """
    config_path = tmp_path / "app_config.json"
    config_path.write_text(
        json.dumps({"developerMode": True, "autoConfigureOnStartup": True}),
        encoding="utf-8",
    )
    _patch_config_path(monkeypatch, config_path)
    cfg = app_main._load_app_config()
    assert cfg["developerMode"] is True
    assert "autoConfigureOnStartup" not in cfg


def test_auto_configure_flag_is_gone():
    """Tombstone for issue #154: the startup auto-flash flag must not
    come back — not in the in-code defaults, not in the shipped config,
    not in any source file."""
    assert "autoConfigureOnStartup" not in app_main._load_app_config()

    shipped = app_main.resource_path("config", "app_config.json")
    with open(shipped, "r", encoding="utf-8") as f:
        assert "autoConfigureOnStartup" not in json.load(f)

    offenders = []
    for pattern in ("*.py", "*.qml", "*.json"):
        for path in REPO_ROOT.rglob(pattern):
            rel = path.relative_to(REPO_ROOT)
            # Skip this test file, virtualenvs and build output.
            if rel == Path("tests") / "test_app_config_defaults.py":
                continue
            skip_dirs = (
                ".venv", "venv", ".git", ".claude",
                "build", "dist", "__pycache__",
            )
            if any(part in skip_dirs for part in rel.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "autoConfigureOnStartup" in text:
                offenders.append(str(rel))
    assert offenders == []
