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

    with zipfile.ZipFile(meta["path"]) as zf:
        names = zf.namelist()
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
    with zipfile.ZipFile(meta["path"]) as zf:
        names = zf.namelist()
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
    with zipfile.ZipFile(meta["path"]) as zf:
        txt = zf.read("system_info.txt").decode("utf-8")
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
    with zipfile.ZipFile(meta["path"]) as zf:
        names = zf.namelist()
    assert "app_config.json" in names


def test_missing_app_logs_dir_still_produces_zip(tmp_path):
    # No app-logs/ directory at all — must still produce a valid zip
    # containing just system_info.txt, no exception.
    data = tmp_path / "data"
    data.mkdir()
    meta = build_debug_bundle(str(data), str(tmp_path / "out"), now_epoch=_NOW)
    with zipfile.ZipFile(meta["path"]) as zf:
        names = zf.namelist()
    assert names == ["system_info.txt"]
    assert meta["log_count"] == 0


def test_unreadable_log_is_skipped_not_fatal(tmp_path, monkeypatch):
    # A log that passes the mtime filter but fails zf.write must be
    # skipped (counted in log_count, absent from the zip) rather than
    # aborting the bundle.
    data = tmp_path / "data"
    logs = data / "app-logs"
    _write(logs / "ow-bloodflowapp-good.log", "ok")
    os.utime(logs / "ow-bloodflowapp-good.log", (_NOW - 3600, _NOW - 3600))
    _write(logs / "ow-bloodflowapp-bad.log", "bad")
    os.utime(logs / "ow-bloodflowapp-bad.log", (_NOW - 3600, _NOW - 3600))

    real_write = zipfile.ZipFile.write

    def flaky_write(self, filename, arcname=None, *a, **k):
        if str(filename).endswith("ow-bloodflowapp-bad.log"):
            raise OSError("simulated read failure")
        return real_write(self, filename, arcname, *a, **k)

    monkeypatch.setattr(zipfile.ZipFile, "write", flaky_write)
    meta = build_debug_bundle(str(data), str(tmp_path / "out"), now_epoch=_NOW)
    with zipfile.ZipFile(meta["path"]) as zf:
        names = zf.namelist()
    assert "app-logs/ow-bloodflowapp-good.log" in names
    assert "app-logs/ow-bloodflowapp-bad.log" not in names
    assert "system_info.txt" in names
    assert meta["log_count"] == 2          # both matched the window
    assert meta["file_count"] == 2         # good log + system_info only
