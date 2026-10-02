"""Fail a Windows build whose payload ships a libusb older than 1.0.30 (#669).

libusb 1.0.30 fixes denial-of-service bugs that a malicious USB device can
trigger with malformed descriptors. Every libusb-1.0.dll the app ships comes
from the SDK: ``omotion/_vendor`` (loaded by pyusb, mirrored to the bundle
root's ``_vendor``) and ``omotion/dfu-util/win*`` (loaded by dfu-util.exe).
An rc build whose ``sdk-version.txt`` still pins an SDK release with older
DLLs therefore fails here instead of shipping them.

The libusb1 wheel's own DLL (1.0.28 in libusb1 3.3.1, 1.0.29 in 3.4.0) is
kept out of the bundle by both build paths. Only the SDK's Linux/macOS
hotplug provider imports ``usb1``. If anything imported it on Windows, its
loader would fall back to the bare ``libusb-1.0.dll`` name, which resolves to
the SDK's copy through the DLL directories ``main.py`` registers.

Both Windows builders check the exact payload:

* ``openwater.spec`` passes its Analysis TOC to :func:`check`.
* ``scripts/build_nuitka.ps1`` runs ``python scripts/check_libusb.py <main.dist>``
  on the standalone tree before packing it into the onefile.

The macOS .app bundles Homebrew's libusb instead. The spec in
``build_macos.sh`` refuses one older than MIN_VERSION
(:func:`homebrew_libusb_version`).
"""

from __future__ import annotations

import os
import re
import struct
import sys
from collections.abc import Iterable
from pathlib import Path

MIN_VERSION = (1, 0, 30)
DLL_NAME = "libusb-1.0.dll"
_FIXED_FILE_INFO_SIGNATURE = struct.pack("<I", 0xFEEF04BD)
_CELLAR_RE = re.compile(r"/Cellar/libusb/(\d+)\.(\d+)\.(\d+)")


def pe_file_version(path: str | Path) -> tuple[int, int, int, int] | None:
    """FileVersion from a PE's VS_FIXEDFILEINFO, or None if it has none.

    Reads the bytes rather than calling the Windows version API, so it works
    for x86 and x64 DLLs alike and on any host.
    """
    data = Path(path).read_bytes()
    i = data.find(_FIXED_FILE_INFO_SIGNATURE)
    if i < 0:
        return None
    ms, ls = struct.unpack_from("<II", data, i + 8)
    return ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF


def homebrew_libusb_version(dylib: str | Path) -> tuple[int, int, int] | None:
    """libusb version of a Homebrew dylib, or None if it isn't a Cellar file.

    The dylib's own current_version is libtool's (4.0.0), not the release, so
    the version comes from the Cellar path the symlink resolves to, e.g.
    /opt/homebrew/Cellar/libusb/1.0.30/lib/libusb-1.0.0.dylib.
    """
    m = _CELLAR_RE.search(os.path.realpath(dylib).replace("\\", "/"))
    return tuple(int(g) for g in m.groups()) if m else None


def check(entries: Iterable[tuple[str, str | Path]]) -> list[str]:
    """Check every libusb-1.0.dll among ``(bundle path, source file)`` pairs.

    Returns one error per DLL older than MIN_VERSION or without a version
    resource, and an error if the payload holds no libusb-1.0.dll at all
    (pyusb cannot reach a sensor without one). An empty list means pass.
    """
    errors = []
    found = 0
    for dest, src in entries:
        if Path(str(dest).replace("\\", "/")).name.lower() != DLL_NAME:
            continue
        found += 1
        version = pe_file_version(src)
        if version is None or version[:3] < MIN_VERSION:
            shown = ".".join(map(str, version)) if version else "no version resource"
            errors.append(
                f"{dest} is libusb {shown}; the build needs >= "
                f"{'.'.join(map(str, MIN_VERSION))} (source: {src})"
            )
        else:
            print(f"[libusb] {dest}: {'.'.join(map(str, version))}")
    if not found:
        errors.append(f"no {DLL_NAME} in the payload")
    return errors


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not Path(args[0]).is_dir():
        print("usage: python scripts/check_libusb.py <payload directory>")
        return 2
    root = Path(args[0])
    errors = check(
        (p.relative_to(root).as_posix(), p) for p in root.rglob("*") if p.is_file()
    )
    for error in errors:
        print(f"::error ::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
