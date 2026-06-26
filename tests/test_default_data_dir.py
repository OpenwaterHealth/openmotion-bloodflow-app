"""Unit tests for MotionConnector._default_data_dir().

The installed (frozen) build must write scan data + scans.db to the same
writable %PROGRAMDATA%\\Openwater root that main.py uses for logs, instead of
the (read-only) Program Files install dir. Dev runs stay on the cwd.
"""
import os

import pytest

from utils import app_paths
from motion_connector import MotionConnector

pytestmark = pytest.mark.unit


def test_frozen_build_uses_writable_root(monkeypatch, tmp_path):
    """An installed build resolves to app_paths.writable_root() (ProgramData),
    not the cwd (the read-only Program Files install dir)."""
    import sys
    program_data = tmp_path / "ProgramData_Openwater"
    program_data.mkdir()
    install_dir = tmp_path / "install_dir"
    install_dir.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_paths, "writable_root", lambda: program_data)
    monkeypatch.chdir(install_dir)  # cwd is the install dir; must be ignored

    assert MotionConnector._default_data_dir() == str(program_data)


def test_dev_run_uses_cwd_when_writable(monkeypatch, tmp_path):
    """A dev (non-frozen) run keeps everything under the writable cwd."""
    import sys
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.chdir(tmp_path)

    assert MotionConnector._default_data_dir() == str(tmp_path)


def test_falls_back_to_documents_when_cwd_unwritable(monkeypatch):
    """When the cwd is read-only (e.g. "/" on a macOS Finder launch) the dev
    fallback is ~/Documents/Openwater Bloodflow — note the "Openwater" casing."""
    import sys
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(os, "access", lambda path, mode: False)

    result = MotionConnector._default_data_dir()

    expected = os.path.join(
        os.path.expanduser("~"), "Documents", "Openwater Bloodflow"
    )
    assert result == expected
    assert "OpenWater" not in result  # casing must be "Openwater"
