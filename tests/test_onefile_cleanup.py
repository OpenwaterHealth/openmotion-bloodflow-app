"""utils.onefile_cleanup: the next launch removes the %TEMP% extraction
folders a killed onefile launcher left behind (#727).

The sweep must delete only folders that are Open-Motion's, whose launcher is
gone, and that no process runs from. The in-use test drives a real process
holding a DLL, because the obvious probe (renaming the folder) succeeds on
Windows while DLLs inside it are loaded.
"""

import importlib
import inspect
import logging
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from utils import onefile_cleanup as oc

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows process semantics")


def _payload(root: Path, name: str, *, markers=True, files=True) -> Path:
    """A folder shaped like an extracted Open-Motion payload."""
    d = root / name
    d.mkdir()
    if markers:
        for sub in ("omotion", "pages", "components", "PyQt6"):
            (d / sub).mkdir()
    if files:
        (d / "main.dll").write_bytes(b"\0" * 1000)
        (d / "main.qml").write_bytes(b"import QtQuick\n")
        (d / "pages" / "BloodFlow.qml").write_bytes(b"Item {}\n")
        (d / "omotion" / "__init__.py").write_text("")
    return d


def _never_alive(_pid, _created):
    return False


# --------------------------------------------------------------------------
# Folder names
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name, pid", [
    ("onefile_49916_513843_hZSSkaEG9H8", 49916),    # the folder in #727
    ("onefile_26680_829023_2p9cWIQn-xw", 26680),    # '-' in the random part
    ("onefile_4_0_a_b", 4),
])
def test_default_nuitka_folder_names_match(name, pid):
    m = oc.ONEFILE_RE.match(name)
    assert m and int(m["pid"]) == pid
    assert not oc.STALE_RE.match(name)
    assert oc.STALE_RE.match(name + oc.STALE_SUFFIX)
    assert not oc.ONEFILE_RE.match(name + oc.STALE_SUFFIX)


@pytest.mark.parametrize("name", [
    "onefile_abc_1_x", "onefile_1_x", "_MEI123456", "Open-Motion", "onefile_1_2_x.tmp",
])
def test_other_names_do_not_match(name):
    assert not oc.ONEFILE_RE.match(name)
    assert not oc.STALE_RE.match(name)


# --------------------------------------------------------------------------
# sweep()
# --------------------------------------------------------------------------

def test_sweep_removes_only_orphaned_open_motion_folders(tmp_path):
    own = _payload(tmp_path, "onefile_100_1_own")
    dead = _payload(tmp_path, "onefile_200_2_dead")
    live = _payload(tmp_path, "onefile_300_3_live")
    foreign = _payload(tmp_path, "onefile_400_4_foreign", markers=False, files=False)
    (foreign / "python313.dll").write_bytes(b"x")
    # A launcher cleanup cut short: only the empty directory tree is left.
    partial = _payload(tmp_path, "onefile_500_5_partial", files=False)
    # An earlier sweep's interrupted removal: no markers needed any more.
    leftover = tmp_path / ("onefile_600_6_left" + oc.STALE_SUFFIX)
    (leftover / "PyQt6").mkdir(parents=True)
    (leftover / "PyQt6" / "Qt6Core.dll").write_bytes(b"\0" * 500)
    (tmp_path / "onefile_700_7_file").write_text("a file, not a folder")
    (tmp_path / "unrelated").mkdir()
    readonly = dead / "pages" / "BloodFlow.qml"
    os.chmod(readonly, stat.S_IREAD)

    result = oc.sweep(tmp_path, own.name, is_launcher_alive=lambda pid, _c: pid == 300)

    # Reported under the names Nuitka gave them, not the interim rename.
    assert sorted(result.removed) == sorted([
        dead.name, partial.name, "onefile_600_6_left",
    ])
    assert result.skipped == [(live.name, "launcher running")]
    assert result.incomplete == []
    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == sorted([
        own.name, live.name, foreign.name, "onefile_700_7_file", "unrelated",
    ])
    assert (own / "main.dll").exists() and (live / "main.dll").exists()
    # main.dll + main.qml + BloodFlow.qml + __init__.py, plus the leftover DLL
    assert result.freed_bytes == 1000 + len("import QtQuick\n") + len("Item {}\n") + 500


def test_sweep_never_touches_its_own_folder_even_if_launcher_looks_dead(tmp_path):
    own = _payload(tmp_path, "onefile_100_1_own")
    result = oc.sweep(tmp_path, own.name, is_launcher_alive=_never_alive)
    assert result.removed == [] and (own / "main.dll").exists()


def test_sweep_asks_about_the_pid_in_the_name_and_the_folder_creation_time(tmp_path):
    _payload(tmp_path, "onefile_4242_1_x")
    seen = []
    before = time.time()
    oc.sweep(tmp_path, "onefile_1_1_own",
             is_launcher_alive=lambda pid, created: seen.append((pid, created)) or True)
    assert [pid for pid, _ in seen] == [4242]
    assert abs(seen[0][1] - before) < 60


def test_missing_temp_root_is_not_an_error(tmp_path):
    result = oc.sweep(tmp_path / "gone", "onefile_1_1_own", is_launcher_alive=_never_alive)
    assert result.removed == [] and result.skipped == []


@windows_only
def test_folder_with_a_loaded_dll_is_left_intact(tmp_path):
    """The orphaned app keeps running after its launcher is killed (#727).
    Its folder must survive a sweep untouched, QML and all, and go once the
    app exits. A rename of the folder would succeed here, which is why the
    probe deletes main.dll instead."""
    import _ctypes

    folder = _payload(tmp_path, "onefile_200_2_inuse")
    shutil.copyfile(_ctypes.__file__, folder / "main.dll")
    child = subprocess.Popen(
        [sys.executable, "-I", "-c",
         "import ctypes, sys, time; ctypes.WinDLL(sys.argv[1]); print('ready', flush=True); time.sleep(60)",
         str(folder / "main.dll")],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        # Renaming is no test of "in use": Windows allows it right now.
        probe = folder.with_name(folder.name + ".probe")
        folder.rename(probe)
        probe.rename(folder)

        result = oc.sweep(tmp_path, "onefile_1_1_own", is_launcher_alive=_never_alive)
        assert result.removed == []
        assert result.skipped == [(folder.name, "in use")]
        assert (folder / "main.dll").exists()
        assert (folder / "main.qml").exists()
        assert (folder / "pages" / "BloodFlow.qml").exists()
    finally:
        child.kill()
        child.wait()

    result = oc.sweep(tmp_path, "onefile_1_1_own", is_launcher_alive=_never_alive)
    assert result.removed == [folder.name]
    assert not any(tmp_path.iterdir())


# --------------------------------------------------------------------------
# launcher_alive()
# --------------------------------------------------------------------------

@windows_only
def test_launcher_alive_for_a_running_process():
    assert oc.launcher_alive(os.getpid(), time.time()) is True


@windows_only
def test_launcher_alive_false_once_the_process_exited():
    child = subprocess.Popen([sys.executable, "-I", "-c", "pass"])
    child.wait()
    assert oc.launcher_alive(child.pid, time.time()) is False


@windows_only
def test_launcher_alive_false_for_a_recycled_pid():
    """A process that started after the folder was created cannot be the
    launcher that created it: Windows handed it a reused pid."""
    ten_days_ago = time.time() - 10 * 24 * 3600
    assert oc.launcher_alive(os.getpid(), ten_days_ago) is False


# --------------------------------------------------------------------------
# Which builds sweep at all
# --------------------------------------------------------------------------

@pytest.mark.parametrize("platform, bundler, folder, expected", [
    ("win32", "nuitka", "onefile_12_34_abc", True),
    ("win32", "nuitka", "main.dist", False),          # standalone, not onefile
    ("win32", "pyinstaller", "_MEI12345", False),
    ("win32", "source", "openmotion-bloodflow-app", False),
    ("darwin", "nuitka", "onefile_12_34_abc", False),
])
def test_own_extraction_dir(monkeypatch, tmp_path, platform, bundler, folder, expected):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(oc, "bundler", lambda: bundler)
    monkeypatch.setattr(oc, "bundle_dir", lambda: tmp_path / folder)
    assert oc.own_extraction_dir() == (tmp_path / folder if expected else None)


def test_source_run_starts_no_sweep():
    assert oc.start_background_sweep() is None


def test_background_sweep_removes_and_logs(monkeypatch, tmp_path, caplog):
    own = _payload(tmp_path, "onefile_100_1_own")
    dead = _payload(tmp_path, "onefile_200_2_dead")
    monkeypatch.setattr(oc, "own_extraction_dir", lambda: own)
    monkeypatch.setattr(oc, "launcher_alive", _never_alive)

    with caplog.at_level(logging.INFO, logger=oc.logger.name):
        thread = oc.start_background_sweep()
        thread.join(timeout=30)

    assert thread.daemon and not thread.is_alive()
    assert not dead.exists() and own.exists()
    assert any("Removed 1 stale onefile extraction folder" in r.getMessage()
               for r in caplog.records)


# --------------------------------------------------------------------------
# Assumptions about the build and the startup order
# --------------------------------------------------------------------------

def test_marker_dirs_and_folder_naming_match_the_nuitka_build():
    """The sweep recognizes our folders by directories the Nuitka build
    bundles, and by Nuitka's default folder name. A build change to either
    would silently turn the cleanup off."""
    script = (REPO_ROOT / "scripts" / "build_nuitka.ps1").read_text(encoding="utf-8")
    assert "--include-data-dir=pages=pages" in script
    assert "--include-data-dir=components=components" in script
    assert "--include-package=omotion" in script
    assert "=omotion\\" in script         # raw SDK dirs land under omotion\
    assert set(oc._MARKER_DIRS) == {"omotion", "pages", "components"}
    assert "--onefile-tempdir-spec" not in script.split("$args = @(", 1)[1]


def test_main_sweeps_after_the_single_instance_check():
    main = importlib.import_module("main")
    src = inspect.getsource(main.main)
    assert 0 < src.index("check_single_instance()") < src.index("start_background_sweep()")
