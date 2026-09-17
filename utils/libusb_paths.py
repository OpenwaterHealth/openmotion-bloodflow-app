"""Put the vendored libusb DLL directories on the Windows DLL search path.

The SDK ships libusb-1.0.dll under ``omotion/_vendor/libusb/windows/<arch>``
and the build mirrors it to ``_vendor/libusb/windows/<arch>`` at the bundle
root (where the ``libusb1`` wheel's own lookup expects it). Neither location
is on the DLL search path of a frozen process by itself.

PyInstaller ran this from ``rthook_libusb_paths.py`` before the app started.
Nuitka has no runtime hooks (#548), so ``main.py`` calls it first thing when
frozen; the PyInstaller hook now calls the same function, so the two
bundlers share one implementation. Idempotent and never raises: a missing
directory is skipped, and if ``os.add_dll_directory`` is unavailable the
directory is prepended to PATH instead.
"""

from __future__ import annotations

import os
from pathlib import Path

# Bundle roots to probe: the onefile extraction directory holds the payload
# flat (PyInstaller onefile and Nuitka), a PyInstaller onedir build keeps it
# under _internal\. Both are listed so the lookup is layout-agnostic.
LAYOUT_ROOTS = ("", "_internal")
VENDOR_DIRS = (
    (),
    ("_vendor", "libusb", "windows", "x64"),
    ("_vendor", "libusb", "windows", "x86"),
    ("omotion", "_vendor", "libusb", "windows", "x64"),
    ("omotion", "_vendor", "libusb", "windows", "x86"),
)


def candidate_dirs(base: str | os.PathLike) -> list[str]:
    base = os.fspath(base)
    out = []
    for root in LAYOUT_ROOTS:
        for parts in VENDOR_DIRS:
            if not root and not parts:
                continue
            out.append(os.path.join(base, root, *parts) if root else os.path.join(base, *parts))
    return out


def register_vendored_libusb(base: str | os.PathLike) -> list[str]:
    """Add every existing candidate directory under ``base`` to the DLL
    search path. Returns the directories that were registered."""
    if os.name != "nt":
        return []
    registered = []
    for p in candidate_dirs(base):
        if not os.path.isdir(p):
            continue
        try:
            os.add_dll_directory(p)
        except Exception:
            os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")
        registered.append(p)
    return registered


__all__ = ["LAYOUT_ROOTS", "VENDOR_DIRS", "candidate_dirs", "register_vendored_libusb", "Path"]
