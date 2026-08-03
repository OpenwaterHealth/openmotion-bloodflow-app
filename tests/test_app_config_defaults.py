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
from utils import config_store

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]


def _patch_config_path(monkeypatch, path):
    """Point config_store.shipped_baseline at ``path`` for config/app_config.json.

    _load_app_config() delegates to config_store.load_app_config(), which has
    its own `from utils.resource_path import resource_path` binding separate
    from main's — patching main.resource_path is a no-op here. Also pin
    OPENWATER_DATA_ROOT to path's tmp dir so the writable-overrides layer
    (app_paths.writable_root(), cwd in dev mode) can't pick up a real
    app_config.local.json left over from running the app locally.
    """
    real_resource_path = config_store.resource_path

    def fake_resource_path(*parts):
        if parts == ("config", "app_config.json"):
            return path
        return real_resource_path(*parts)

    monkeypatch.setattr(config_store, "resource_path", fake_resource_path)
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(path.parent))


def test_missing_config_falls_back_to_defaults(tmp_path, monkeypatch):
    """No app_config.json at all -> pure in-code defaults."""
    _patch_config_path(monkeypatch, tmp_path / "app_config.json")
    cfg = app_main._load_app_config()
    assert cfg["leftMask"] == 0x66
    assert cfg["engineeringMode"] is False


def test_corrupt_config_falls_back_to_defaults(tmp_path, monkeypatch):
    """Corrupt JSON falls back to in-code defaults instead of crashing."""
    config_path = tmp_path / "app_config.json"
    config_path.write_text("{not valid json", encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    cfg = app_main._load_app_config()
    assert cfg["leftMask"] == 0x66
    assert cfg["engineeringMode"] is False


def test_unknown_keys_are_dropped(tmp_path, monkeypatch):
    """Keys not in the in-code defaults are filtered out on load.

    This is what retires removed flags in the field: a persisted config
    that still carries `autoConfigureOnStartup` from an older build loses
    it on load, so no QML/Python code can ever see it again.
    """
    config_path = tmp_path / "app_config.json"
    config_path.write_text(
        json.dumps({"engineeringMode": True, "autoConfigureOnStartup": True}),
        encoding="utf-8",
    )
    _patch_config_path(monkeypatch, config_path)
    cfg = app_main._load_app_config()
    assert cfg["engineeringMode"] is True
    assert "autoConfigureOnStartup" not in cfg


def test_critical_error_config_keys_preserved(tmp_path, monkeypatch):
    """Bug-report + connection-watchdog keys must survive the whitelist.

    `_load_app_config` drops any key not present in the in-code defaults, so
    these keys must be registered there or they silently never reach the
    connector (e.g. `bug_report_smtp` could never enable SMTP send).
    """
    config_path = tmp_path / "app_config.json"
    config_path.write_text(
        json.dumps({
            "support_email": "x@y.z",
            "bug_report_smtp": {"host": "h"},
            "connectionTimeoutSec": 12,
            "requireConsole": True,
            "minSensors": 2,
        }),
        encoding="utf-8",
    )
    _patch_config_path(monkeypatch, config_path)
    cfg = app_main._load_app_config()
    assert cfg["support_email"] == "x@y.z"
    assert cfg["bug_report_smtp"] == {"host": "h"}
    assert cfg["connectionTimeoutSec"] == 12
    assert cfg["minSensors"] == 2
    assert cfg["requireConsole"] is True


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


def test_tec_trip_temp_default_is_registered(tmp_path, monkeypatch):
    """tecTripTempC must be in the in-code defaults whitelist, or
    _load_app_config silently drops it and the connector never pushes the
    configured over-temp trip."""
    # Present in the pure in-code defaults (no file on disk).
    _patch_config_path(monkeypatch, tmp_path / "app_config.json")
    cfg = app_main._load_app_config()
    assert cfg["tecTripTempC"] == 40

    # A value supplied in the file survives the whitelist filter.
    config_path = tmp_path / "app_config.json"
    config_path.write_text(json.dumps({"tecTripTempC": 42}), encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    cfg = app_main._load_app_config()
    assert cfg["tecTripTempC"] == 42


def test_tec_trip_temp_present_in_shipped_config():
    """The shipped config must carry tecTripTempC with a nonzero value so
    field installs push a real over-temp trip on connect — a shipped 0 would
    disable the firmware trip entirely."""
    shipped = app_main.resource_path("config", "app_config.json")
    with open(shipped, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["tecTripTempC"] == 40


def test_connection_timeout_default_is_twelve_seconds(tmp_path, monkeypatch):
    """The startup connection watchdog fires 12 s after launch.

    It owns both the E-104/E-106 warning toast and (research builds only)
    the sample-dataset offer, so 30 s left the user staring at an empty
    scan page. One timer, one timeout — there is no separate offer timer.
    """
    config_path = tmp_path / "app_config.json"
    config_path.write_text(json.dumps({}), encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    cfg = app_main._load_app_config()
    assert cfg["connectionTimeoutSec"] == 12


def test_shipped_config_connection_timeout_is_twelve_seconds():
    """The shipped config overrides the in-code default, so it must agree
    with it — otherwise the field build still waits 30 s."""
    shipped = json.loads(
        (REPO_ROOT / "config" / "app_config.json").read_text(encoding="utf-8"))
    assert shipped["connectionTimeoutSec"] == 12


def test_beta_updates_default_is_registered(tmp_path, monkeypatch):
    """downloadBetaUpdates must be in the in-code defaults whitelist or
    _load_app_config silently drops it and the beta toggle never persists."""
    _patch_config_path(monkeypatch, tmp_path / "app_config.json")
    cfg = app_main._load_app_config()
    assert cfg["downloadBetaUpdates"] is False

    config_path = tmp_path / "app_config.json"
    config_path.write_text(json.dumps({"downloadBetaUpdates": True}), encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    cfg = app_main._load_app_config()
    assert cfg["downloadBetaUpdates"] is True


def test_beta_updates_present_in_shipped_config():
    """Shipped config carries downloadBetaUpdates (default off); the old
    downloadBetaFirmware key is fully retired."""
    shipped = app_main.resource_path("config", "app_config.json")
    with open(shipped, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["downloadBetaUpdates"] is False
    assert "downloadBetaFirmware" not in data
