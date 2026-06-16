"""Unit tests for the debug-log bundle builder (debug_bundle.py)."""
import os
import zipfile

import pytest

from debug_bundle import build_debug_bundle, WINDOW_HOURS

pytestmark = pytest.mark.unit

_NOW = 1_750_000_000.0  # fixed epoch for deterministic tests


def _write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_window_hours_default_is_48():
    assert WINDOW_HOURS == 48


def test_includes_recent_logs_excludes_old(tmp_path):
    data = tmp_path / "data"
    logs = data / "app-logs"
    for name in ("ow-bloodflowapp-A.log", "ow-bloodflowapp-B.log"):
        _write(logs / name, "log")
        os.utime(logs / name, (_NOW - 3600, _NOW - 3600))   # 1h ago
    old = logs / "ow-bloodflowapp-OLD.log"
    _write(old, "old")
    os.utime(old, (_NOW - 49 * 3600, _NOW - 49 * 3600))     # 49h ago
    _write(data / "app_config.json", '{"k":1}')

    meta = build_debug_bundle(
        str(data), str(tmp_path / "out"), now_epoch=_NOW,
        extra_info={"app_version": "1.2.3", "sdk_version": "9.9"},
    )

    names = zipfile.ZipFile(meta["path"]).namelist()
    assert "app-logs/ow-bloodflowapp-A.log" in names
    assert "app-logs/ow-bloodflowapp-B.log" in names
    assert "app-logs/ow-bloodflowapp-OLD.log" not in names
    assert "app_config.json" in names
    assert "system_info.txt" in names
    assert meta["log_count"] == 2
    assert meta["bytes"] == os.path.getsize(meta["path"])
    base = os.path.basename(meta["path"])
    assert base.startswith("debug-bundle-") and base.endswith(".zip")


def test_empty_window_still_writes_system_info(tmp_path):
    data = tmp_path / "data"
    (data / "app-logs").mkdir(parents=True)
    meta = build_debug_bundle(str(data), str(tmp_path / "out"), now_epoch=_NOW)
    names = zipfile.ZipFile(meta["path"]).namelist()
    assert names == ["system_info.txt"]   # no logs, no config present
    assert meta["log_count"] == 0
    assert meta["file_count"] == 1


def test_system_info_contains_versions_and_host(tmp_path):
    data = tmp_path / "data"
    (data / "app-logs").mkdir(parents=True)
    meta = build_debug_bundle(
        str(data), str(tmp_path / "out"), now_epoch=_NOW,
        extra_info={"app_version": "1.2.3", "sdk_version": "9.9"},
    )
    txt = zipfile.ZipFile(meta["path"]).read("system_info.txt").decode("utf-8")
    assert "app_version: 1.2.3" in txt
    assert "sdk_version: 9.9" in txt
    assert "hostname:" in txt
    assert "generated:" in txt


def test_explicit_config_path_is_used(tmp_path):
    data = tmp_path / "data"
    (data / "app-logs").mkdir(parents=True)
    cfg = tmp_path / "elsewhere" / "app_config.json"
    _write(cfg, '{"x":2}')
    meta = build_debug_bundle(
        str(data), str(tmp_path / "out"), now_epoch=_NOW, config_path=str(cfg),
    )
    names = zipfile.ZipFile(meta["path"]).namelist()
    assert "app_config.json" in names
