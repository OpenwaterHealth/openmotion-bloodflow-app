import os
import sys

import pytest

from utils import app_paths


def _override(monkeypatch, path):
    """Pin (or clear, with None) the in-process data-root override.

    The autouse ``_isolate_writable_root`` fixture already points it at a
    per-test tmp dir; these tests need to control it exactly.
    """
    monkeypatch.setattr(app_paths, "DATA_ROOT_OVERRIDE", path)


def _fake_home(monkeypatch, home):
    """Point the home-folder lookup at a temp dir.

    app_paths asks the shell / passwd database for the home folder rather
    than reading $HOME / %USERPROFILE%, so patching the helper is the only
    hook — and it holds on every platform the suite is collected on.
    """
    monkeypatch.setattr(app_paths, "_home_dir", lambda: home)


@pytest.mark.unit
def test_writable_root_honors_in_process_override(tmp_path, monkeypatch):
    _override(monkeypatch, tmp_path / "ow")
    root = app_paths.writable_root()
    assert root == tmp_path / "ow"
    assert root.is_dir()  # created on access


@pytest.mark.unit
def test_set_data_root_override_round_trips(tmp_path, monkeypatch):
    _override(monkeypatch, None)
    app_paths.set_data_root_override(str(tmp_path / "ow"))
    assert app_paths.writable_root() == tmp_path / "ow"
    app_paths.set_data_root_override(None)
    assert app_paths.DATA_ROOT_OVERRIDE is None


@pytest.mark.unit
def test_local_config_path_under_root(tmp_path, monkeypatch):
    _override(monkeypatch, tmp_path / "ow")
    expected = tmp_path / "ow" / "app_config.local.json"
    assert app_paths.local_config_path() == expected


@pytest.mark.unit
def test_env_vars_are_ignored(tmp_path, monkeypatch):
    """The retired OPENWATER_DATA_ROOT / PROGRAMDATA env vars must not steer
    the root any more: a packaged build has to start identically no matter
    what the host environment carries."""
    _override(monkeypatch, None)
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path / "from_env"))
    monkeypatch.chdir(tmp_path)
    assert app_paths.writable_root() == tmp_path
    assert not (tmp_path / "from_env").exists()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "env_programdata"))
    monkeypatch.setattr(
        app_paths, "_program_data_dir", lambda: tmp_path / "shell_programdata"
    )
    root = app_paths.writable_root(portable=False)
    assert root == tmp_path / "shell_programdata" / "Openwater"
    assert not (tmp_path / "env_programdata").exists()


@pytest.mark.unit
def test_dev_root_is_cwd_when_not_frozen(tmp_path, monkeypatch):
    _override(monkeypatch, None)
    monkeypatch.chdir(tmp_path)
    # not frozen in test → root is cwd, behavior unchanged for local dev
    assert app_paths.writable_root() == tmp_path


@pytest.mark.unit
def test_frozen_non_portable_uses_program_data(tmp_path, monkeypatch):
    _override(monkeypatch, None)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")  # ProgramData is Windows-only
    monkeypatch.setattr(app_paths, "_program_data_dir", lambda: tmp_path)

    root = app_paths.writable_root(portable=False)

    assert root == tmp_path / "Openwater"
    assert root.is_dir()


@pytest.mark.unit
@pytest.mark.skipif(sys.platform != "win32", reason="SHGetKnownFolderPath is Windows-only")
def test_program_data_dir_comes_from_the_shell(monkeypatch):
    """The known-folder lookup answers a real directory (normally
    C:\\ProgramData) even when the env var points somewhere else."""
    monkeypatch.setenv("PROGRAMDATA", r"C:\definitely\not\here")
    got = app_paths._program_data_dir()
    assert got.is_dir()
    assert got.name.lower() == "programdata"


@pytest.mark.unit
def test_program_data_dir_falls_back_when_shell_lookup_fails(monkeypatch):
    monkeypatch.setattr(app_paths, "_known_folder", lambda folder_id: None)
    assert app_paths._program_data_dir() == app_paths.Path(r"C:\ProgramData")


@pytest.mark.unit
def test_home_dir_ignores_profile_env_vars(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "env_home"))
    monkeypatch.setenv("HOME", str(tmp_path / "env_home"))
    monkeypatch.setattr(
        app_paths, "_known_folder", lambda folder_id: tmp_path / "shell_home"
    )
    assert app_paths._home_dir() == tmp_path / "shell_home"


@pytest.mark.unit
def test_frozen_portable_uses_exe_folder(tmp_path, monkeypatch):
    _override(monkeypatch, None)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")  # portable zips are Windows-only
    exe_dir = tmp_path / "install_dir"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "executable", str(exe_dir / "Open-Motion.exe"), raising=False)

    root = app_paths.writable_root(portable=True)

    assert root == exe_dir


@pytest.mark.unit
def test_frozen_macos_uses_application_support(tmp_path, monkeypatch):
    """A frozen macOS build has no %PROGRAMDATA%; it must land in the standard
    per-user data location, not a directory named after a Windows path."""
    _override(monkeypatch, None)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    _fake_home(monkeypatch, tmp_path)

    root = app_paths.writable_root(portable=False)

    assert root == tmp_path / "Library" / "Application Support" / "Openwater"
    assert root.is_dir()


@pytest.mark.unit
def test_frozen_macos_never_yields_a_windows_path(tmp_path, monkeypatch):
    """Regression: the r'C:\\ProgramData' default was once taken literally off
    Windows — creating a directory actually named 'C:\\ProgramData' relative
    to the cwd (inside the .app bundle, or a hard failure when Finder launches
    with cwd='/').

    Asserted as "no ProgramData component" rather than "no 'C:' substring",
    because tmp_path is itself a C:\\... path when the suite runs on Windows.
    """
    _override(monkeypatch, None)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    _fake_home(monkeypatch, tmp_path)

    for portable in (True, False):
        root = app_paths.writable_root(portable=portable)
        assert "ProgramData" not in str(root)
        assert root == tmp_path / "Library" / "Application Support" / "Openwater"


@pytest.mark.unit
def test_frozen_macos_portable_stays_outside_the_app_bundle(tmp_path, monkeypatch):
    """Writing inside Open-Motion.app invalidates its code signature, so the
    portable layout cannot apply on macOS."""
    _override(monkeypatch, None)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    _fake_home(monkeypatch, tmp_path)
    bundle = tmp_path / "Open-Motion.app" / "Contents" / "MacOS"
    bundle.mkdir(parents=True)
    monkeypatch.setattr(sys, "executable", str(bundle / "Open-Motion"), raising=False)

    root = app_paths.writable_root(portable=True)

    assert ".app" not in str(root)


@pytest.mark.unit
def test_falls_back_to_documents_when_root_mkdir_denied(tmp_path, monkeypatch):
    """The unwritable-root fallback must survive mkdir itself being refused —
    on a read-only parent (Finder launch, cwd='/') mkdir raises before the
    os.access check is ever reached."""
    _override(monkeypatch, None)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    _fake_home(monkeypatch, tmp_path)
    denied = tmp_path / "readonly_cwd"
    denied.mkdir()
    monkeypatch.chdir(denied)

    real_mkdir = app_paths.Path.mkdir

    def fake_mkdir(self, *args, **kwargs):
        if self == denied:
            raise PermissionError(13, "Read-only file system")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(app_paths.Path, "mkdir", fake_mkdir)

    root = app_paths.writable_root()

    assert root == tmp_path / "Documents" / "Open-Motion"
    assert root.is_dir()


@pytest.mark.unit
def test_falls_back_to_documents_when_root_unwritable(tmp_path, monkeypatch):
    """When the resolved root isn't writable (e.g. cwd is "/" on a macOS
    Finder launch) writable_root falls back to ~/Documents/Open-Motion."""
    _override(monkeypatch, None)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    _fake_home(monkeypatch, tmp_path)
    monkeypatch.setattr(os, "access", lambda path, mode: False)

    result = app_paths.writable_root()

    assert result == tmp_path / "Documents" / "Open-Motion"
    assert "OpenWater" not in str(result)  # casing must be "Openwater"
