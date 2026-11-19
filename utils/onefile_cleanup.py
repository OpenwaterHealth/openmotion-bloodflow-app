"""Remove onefile extraction folders a killed launcher left behind (#727).

A Nuitka onefile build runs as two processes. The launcher unpacks the app
into ``%TEMP%\\onefile_<launcher pid>_<time>_<random>`` and starts a child,
the app itself, which runs ``main.dll`` from that folder. The launcher
deletes the folder after the child exits; nothing else ever does. If the
launcher dies first (End process tree, a forced sign-out, power loss, the
HIL harness closing the app), the folder is orphaned: ~200 MB per
occurrence, in a directory a clinical operator never looks at, until
``minFreeDiskMb`` (#506) starts refusing scans for a reason they cannot see.
The app cannot delete its own folder on the way out, because its DLLs are
mapped from it until the process is gone. Nuitka's child-side parent
watchdog does not help either: it is compiled out of the DLL-mode child
(``MainProgram.c``, ``!_NUITKA_DLL_MODE``), which is why the app keeps
running after its launcher is killed.

So the next launch cleans up. :func:`start_background_sweep` looks at the
siblings of our own extraction folder and removes one only when all three
hold:

1. **It is ours.** It holds the app's data directories (``_MARKER_DIRS``).
   Every Nuitka onefile program on the machine shares the ``onefile_``
   naming, and other programs' folders are not ours to delete.
2. **Its launcher is gone.** No process has the pid in its name, or the
   process that does started after the folder was created (Windows reuses
   pids).
3. **Nothing runs from it.** ``main.dll`` is deleted first, and Windows
   refuses to delete a DLL any process has loaded. If that fails, nothing
   else in the folder is touched. Renaming the folder is no such test: a
   folder whose DLLs are loaded can still be renamed (checked on Windows
   11), so a rename-first probe would let us strip QML files out from
   under a running orphan.

A folder that passed is renamed with ``STALE_SUFFIX`` before removal, so a
removal interrupted halfway (the app exiting, an antivirus lock) is finished
by a later launch even after its marker directories are gone.

The sweep runs after the single-instance mutex is held, so no other
instance of this app is running: check 3 is the guard for anything else.
Only the Windows Nuitka onefile build has the problem, and only when the
extraction folder uses Nuitka's default name; everything else
(source runs, PyInstaller, macOS) is a no-op.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from utils.frozen import bundle_dir, bundler

logger = logging.getLogger("openmotion.bloodflow-app.onefile-cleanup")

# Nuitka 4.x's default --onefile-tempdir-spec on Windows is
# {TEMP}\onefile_{PID}_{TIME_US}_{RANDOM}; scripts/build_nuitka.ps1 keeps it.
ONEFILE_RE = re.compile(r"^onefile_(?P<pid>\d+)_\d+_[A-Za-z0-9_-]+$")
STALE_SUFFIX = ".open-motion-stale"
STALE_RE = re.compile(r"^onefile_\d+_\d+_[A-Za-z0-9_-]+" + re.escape(STALE_SUFFIX) + r"$")

# Directories only an Open-Motion payload has (build_nuitka.ps1 bundles
# them). Directories rather than files: when the launcher's own cleanup
# is cut short it leaves the empty directory tree behind.
_MARKER_DIRS = ("omotion", "pages", "components")

# The DLL the app runs from. It stays loaded for the process's lifetime,
# so "can it be deleted?" is the in-use test.
_IN_USE_PROBE = "main.dll"

# A launcher creates its folder after it starts. A process with the same
# pid that started later than this after the folder appeared is a
# different process that was handed a recycled pid.
_PID_REUSE_SLACK_S = 2.0


@dataclass
class SweepResult:
    removed: list[str] = field(default_factory=list)
    freed_bytes: int = 0
    # (folder name, reason) for folders that are ours but were left alone.
    skipped: list[tuple[str, str]] = field(default_factory=list)
    # Folders a removal was attempted on that still exist afterwards.
    incomplete: list[str] = field(default_factory=list)


def own_extraction_dir() -> Optional[Path]:
    """This process's onefile extraction folder, or None when there is none.

    None for source runs, PyInstaller and non-Windows builds, and for a
    Nuitka build whose folder does not have the default ``onefile_`` name.
    """
    if sys.platform != "win32" or bundler() != "nuitka":
        return None
    own = bundle_dir()
    return own if ONEFILE_RE.match(own.name) else None


def _created(path: Path) -> float:
    st = path.stat()
    return getattr(st, "st_birthtime", st.st_ctime)


def launcher_alive(pid: int, folder_created: float) -> bool:
    """True if the launcher that created a folder at ``folder_created``
    (epoch seconds) may still be running as ``pid``.

    Errs towards True: a process we may not query is treated as running.
    """
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)

    process_query_limited_information = 0x1000
    error_invalid_parameter = 87    # OpenProcess: no process has this pid
    still_active = 259

    handle = k32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() != error_invalid_parameter
    try:
        code = wintypes.DWORD()
        if k32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != still_active:
            return False
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return True
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        started = (ticks - 116444736000000000) / 1e7     # FILETIME -> epoch s
        return started <= folder_created + _PID_REUSE_SLACK_S
    finally:
        k32.CloseHandle(handle)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(os.path, "isjunction", lambda _: False)(path))


def _size(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def _rmtree(path: Path) -> None:
    """Remove a tree, clearing read-only attributes on the way; never raises."""
    def retry_writable(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry_writable)
    else:
        shutil.rmtree(path, onerror=retry_writable)


def _remove(path: Path, name: str, result: SweepResult, size: Optional[int] = None) -> None:
    """Remove ``path``, recorded under the folder's original ``name``."""
    if size is None:
        size = _size(path)
    _rmtree(path)
    if path.exists():
        result.incomplete.append(name)
        result.freed_bytes += max(0, size - _size(path))
    else:
        result.removed.append(name)
        result.freed_bytes += size


def sweep(
    temp_root: Path,
    own_name: str,
    *,
    is_launcher_alive: Optional[Callable[[int, float], bool]] = None,
) -> SweepResult:
    """Remove the orphaned Open-Motion extraction folders in ``temp_root``.

    ``own_name`` is this process's own folder, which is never touched. Never
    raises for a single folder; a failure leaves it for a later launch.
    ``is_launcher_alive`` defaults to :func:`launcher_alive`.
    """
    is_launcher_alive = is_launcher_alive or launcher_alive
    result = SweepResult()
    try:
        entries = sorted(temp_root.iterdir())
    except OSError as exc:
        logger.warning("Could not list %s for stale extraction folders: %s", temp_root, exc)
        return result

    for path in entries:
        name = path.name
        if name == own_name:
            continue
        try:
            if _is_link(path) or not path.is_dir():
                continue
            if STALE_RE.match(name):
                _remove(path, name[:-len(STALE_SUFFIX)], result)
                continue
            match = ONEFILE_RE.match(name)
            if not match or not all((path / d).is_dir() for d in _MARKER_DIRS):
                continue
            if is_launcher_alive(int(match["pid"]), _created(path)):
                result.skipped.append((name, "launcher running"))
                continue
            size = _size(path)
            probe = path / _IN_USE_PROBE
            if probe.exists():
                try:
                    probe.unlink()
                except OSError:
                    result.skipped.append((name, "in use"))
                    continue
            target = path.with_name(name + STALE_SUFFIX)
            try:
                path.rename(target)
            except OSError:
                target = path
            _remove(target, name, result, size)
        except OSError as exc:
            result.skipped.append((name, f"error: {exc}"))
    return result


def _run(own: Path) -> None:
    try:
        result = sweep(own.parent, own.name)
    except Exception:
        logger.exception("Stale extraction folder cleanup failed")
        return
    if result.removed or result.incomplete:
        logger.info(
            "Removed %d stale onefile extraction folder(s) from %s, %.0f MB freed: %s",
            len(result.removed), own.parent, result.freed_bytes / (1024 * 1024),
            ", ".join(result.removed) or "-",
        )
    if result.incomplete:
        logger.warning(
            "Could not fully remove %s; the next launch retries",
            ", ".join(result.incomplete),
        )
    for name, reason in result.skipped:
        logger.info("Left extraction folder %s in place (%s)", name, reason)


def start_background_sweep() -> Optional[threading.Thread]:
    """Clean up orphaned extraction folders off the GUI thread.

    Returns the started daemon thread, or None when this build has no
    onefile extraction folder. Call it only while holding the
    single-instance mutex.
    """
    own = own_extraction_dir()
    if own is None:
        return None
    thread = threading.Thread(
        target=_run, args=(own,), name="onefile-cleanup", daemon=True
    )
    thread.start()
    return thread
