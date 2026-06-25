"""Unit tests for _load_app_config resolution paths — issue #154.

The startup camera-FPGA auto-flash (`autoConfigureOnStartup`) used to
keep the Start button orange/disabled for ~2 minutes while every camera
FPGA was programmed sequentially — redundant work, since ScanRunner runs
FlashSensorsTask unconditionally on every Start/Check. The flag and the
startup-flash machinery were removed entirely; these tests pin the
config-loader fallback behavior and tombstone the removed flag so it is
never silently reintroduced.

``main._load_app_config`` delegates to ``config_store.load_app_config``,
which merges two on-disk layers over the in-code defaults:
  * the read-only shipped ``config/app_config.json``, resolved via
    ``config_store.resource_path``; and
  * the writable overrides file, resolved via ``app_paths.local_config_path``.
``_isolate_config`` patches *both* of those seams at temp files so the tests
never read the real shipped config or the machine's ``app_config.local.json``
(which in a dev run lives at ``<cwd>/app_config.local.json`` and made these
tests order-dependent).
"""

import json
from pathlib import Path

import pytest

import main as app_main
from utils import app_paths, config_store

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]


def _materialize(path: Path, content) -> None:
    """Write ``content`` to ``path``; ``None`` leaves the file absent.

    ``dict`` is JSON-encoded; ``str`` is written verbatim (corrupt-JSON cases).
    """
    if content is None:
        return
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(json.dumps(content), encoding="utf-8")


def _isolate_config(monkeypatch, tmp_path, shipped=None, overrides=None):
    """Redirect both config_store file seams at temp files under ``tmp_path``.

    ``shipped`` populates the read-only baseline layer; ``overrides`` the
    writable layer. Each may be ``None`` (file absent), a ``dict`` (JSON), or
    a ``str`` (written verbatim, e.g. corrupt JSON).
    """
    shipped_path = tmp_path / "shipped_app_config.json"
    overrides_path = tmp_path / "app_config.local.json"
    _materialize(shipped_path, shipped)
    _materialize(overrides_path, overrides)

    real_resource_path = config_store.resource_path

    def fake_resource_path(*parts):
        if parts == ("config", "app_config.json"):
            return shipped_path
        return real_resource_path(*parts)

    # config_store looks both names up in its own module globals at call time.
    monkeypatch.setattr(config_store, "resource_path", fake_resource_path)
    monkeypatch.setattr(app_paths, "local_config_path", lambda: overrides_path)


def test_missing_config_falls_back_to_defaults(tmp_path, monkeypatch):
    """No config on either layer -> pure in-code defaults."""
    _isolate_config(monkeypatch, tmp_path)
    cfg = app_main._load_app_config()
    assert cfg["leftMask"] == 0x66
    assert cfg["developerMode"] is False


def test_corrupt_config_falls_back_to_defaults(tmp_path, monkeypatch):
    """Corrupt shipped JSON falls back to in-code defaults instead of crashing."""
    _isolate_config(monkeypatch, tmp_path, shipped="{not valid json")
    cfg = app_main._load_app_config()
    assert cfg["leftMask"] == 0x66
    assert cfg["developerMode"] is False


def test_unknown_keys_are_dropped(tmp_path, monkeypatch):
    """Keys not in the in-code defaults are filtered out on load.

    This is what retires removed flags in the field: a persisted overrides
    file that still carries `autoConfigureOnStartup` from an older build loses
    it on load, so no QML/Python code can ever see it again.
    """
    _isolate_config(
        monkeypatch, tmp_path,
        overrides={"developerMode": True, "autoConfigureOnStartup": True},
    )
    cfg = app_main._load_app_config()
    assert cfg["developerMode"] is True
    assert "autoConfigureOnStartup" not in cfg


def test_critical_error_config_keys_preserved(tmp_path, monkeypatch):
    """Bug-report + connection-watchdog keys must survive the whitelist.

    `load_app_config` drops any key not present in the in-code defaults, so
    these keys must be registered there or they silently never reach the
    connector (e.g. `bug_report_smtp` could never enable SMTP send).
    """
    _isolate_config(monkeypatch, tmp_path, overrides={
        "support_email": "x@y.z",
        "bug_report_smtp": {"host": "h"},
        "connectionTimeoutSec": 12,
        "requireConsole": True,
        "minSensors": 2,
    })
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
    load_app_config silently drops it and the connector never pushes the
    configured over-temp trip."""
    # Present in the pure in-code defaults (no files on disk).
    _isolate_config(monkeypatch, tmp_path)
    cfg = app_main._load_app_config()
    assert cfg["tecTripTempC"] == 40

    # A value supplied in the overrides file survives the whitelist filter.
    _isolate_config(monkeypatch, tmp_path, overrides={"tecTripTempC": 42})
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
