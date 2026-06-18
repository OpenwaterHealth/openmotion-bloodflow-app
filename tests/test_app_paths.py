import os
import pytest
from pathlib import Path
from utils import app_paths


@pytest.mark.unit
def test_writable_root_honors_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path / "ow"))
    root = app_paths.writable_root()
    assert root == tmp_path / "ow"
    assert root.is_dir()  # created on access


@pytest.mark.unit
def test_local_config_and_data_dir_under_root(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path / "ow"))
    assert app_paths.local_config_path() == tmp_path / "ow" / "app_config.local.json"
    assert app_paths.default_data_dir() == tmp_path / "ow" / "data"
    assert (tmp_path / "ow" / "data").is_dir()


@pytest.mark.unit
def test_dev_root_is_cwd_when_not_frozen(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENWATER_DATA_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    # not frozen in test → root is cwd, behavior unchanged for local dev
    assert app_paths.writable_root() == tmp_path
