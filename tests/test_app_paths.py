import os
import sys

import pytest

from utils import app_paths


@pytest.mark.unit
def test_writable_root_honors_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path / "ow"))
    root = app_paths.writable_root()
    assert root == tmp_path / "ow"
    assert root.is_dir()  # created on access


@pytest.mark.unit
def test_local_config_path_under_root(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path / "ow"))
    expected = tmp_path / "ow" / "app_config.local.json"
    assert app_paths.local_config_path() == expected


@pytest.mark.unit
def test_dev_root_is_cwd_when_not_frozen(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENWATER_DATA_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    # not frozen in test → root is cwd, behavior unchanged for local dev
    assert app_paths.writable_root() == tmp_path


@pytest.mark.unit
def test_frozen_non_portable_uses_program_data(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENWATER_DATA_ROOT", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))

    root = app_paths.writable_root(portable=False)

    assert root == tmp_path / "Openwater"
    assert root.is_dir()


@pytest.mark.unit
def test_frozen_portable_uses_exe_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENWATER_DATA_ROOT", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    exe_dir = tmp_path / "install_dir"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "executable", str(exe_dir / "OpenWaterApp.exe"), raising=False)

    root = app_paths.writable_root(portable=True)

    assert root == exe_dir


@pytest.mark.unit
def test_falls_back_to_documents_when_root_unwritable(monkeypatch):
    """When the resolved root isn't writable (e.g. cwd is "/" on a macOS
    Finder launch) writable_root falls back to ~/Documents/Openwater
    Bloodflow — note the "Openwater" casing."""
    monkeypatch.delenv("OPENWATER_DATA_ROOT", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(os, "access", lambda path, mode: False)

    result = app_paths.writable_root()

    expected = os.path.expanduser(os.path.join("~", "Documents", "Openwater Bloodflow"))
    assert str(result) == expected
    assert "OpenWater" not in str(result)  # casing must be "Openwater"
