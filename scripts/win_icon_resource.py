#!/usr/bin/env python3
"""Publish the frozen exe's icon group under the name Qt looks for.

Qt's ``QWindowsContext::registerWindowClass`` sets the Win32 *window-class*
icon with ``LoadImage(hInstance, L"IDI_ICON1", ...)`` and falls back to
``LoadIcon(NULL, IDI_APPLICATION)`` — the generic Windows icon — when that
lookup fails. (The literal is in qwindows.dll; Qt's own qmake/CMake RC
template names the icon ``IDI_ICON1``, which is why Qt looks for it.)

PyInstaller writes the icon group under the *integer* resource id 1
(``PyInstaller/utils/win32/icon.py::CopyIcons_FromIco`` →
``UpdateResource(hdst, RT_GROUP_ICON, i + 1, data)``). A name lookup never
matches an integer id, so **every** PyInstaller-frozen Qt app registers its
window classes with the generic Windows icon.

That is normally invisible, because Qt also sets a per-window icon via
``WM_SETICON`` and the shell prefers it. But the shell asks for the
per-window icon with ``SendMessageTimeout(..., SMTO_ABORTIFHUNG)``: if the
GUI thread is busy when Explorer probes a freshly shown window, the shell
gives up and paints the *window-class* icon instead — the generic one — and
it sticks for the life of that window. That is the intermittent,
machine-dependent taskbar failure in issue #223: it only bites on machines
slow enough to blow Explorer's probe budget during startup.

The fix is to write a *second* ``RT_GROUP_ICON`` directory, named
``IDI_ICON1``, pointing at the ``RT_ICON`` images PyInstaller already wrote.
The integer group #1 is left untouched, so the icon Explorer shows for the
.exe file itself — and every shortcut that inherits it — is unchanged.

Build-time entry point is :func:`install_pyinstaller_hook`, called from
``openwater.spec``. It patches ``CopyIcons`` rather than post-processing the
finished exe on purpose: PyInstaller embeds resources *before* appending the
PKG archive and *before* computing the PE checksum (``EXE.assemble``
steps 1-3), so rewriting resources afterwards would rewrite a PE whose
overlay and checksum are already final.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

RT_ICON = 3
RT_GROUP_ICON = 14

#: The resource name Qt hard-codes in QWindowsContext::registerWindowClass.
QT_ICON_NAME = "IDI_ICON1"

_LOAD_LIBRARY_AS_DATAFILE = 0x0002
_LOAD_LIBRARY_AS_IMAGE_RESOURCE = 0x0020
_IMAGE_ICON = 1
_LR_DEFAULTSIZE = 0x0040


def add_named_group_icon(exe_path, ico_path, name=QT_ICON_NAME, first_icon_id=1):
    """Alias an already-embedded icon group under the string ``name``.

    Assumes PyInstaller has just written the ``RT_ICON`` images with
    consecutive ids starting at ``first_icon_id`` (it does — see
    ``CopyIcons_FromIco``). Only the group *directory* is duplicated, so
    this costs a few dozen bytes, not a second copy of the images.

    Returns the number of bytes written.
    """
    # Imported lazily: only the build machine has PyInstaller, and the
    # runtime/test paths in this module must not require it.
    from PyInstaller.compat import win32api
    from PyInstaller.utils.win32.icon import IconFile

    icon_file = IconFile(str(ico_path))
    data = icon_file.grp_icon_dir() + icon_file.grp_icondir_entries(first_icon_id)

    handle = win32api.BeginUpdateResource(str(exe_path), 0)
    win32api.UpdateResource(handle, RT_GROUP_ICON, name, data)
    win32api.EndUpdateResource(handle, 0)
    return len(data)


def has_named_group_icon(exe_path, name=QT_ICON_NAME) -> bool:
    """True if Qt's exact class-icon lookup would succeed against this exe.

    Deliberately replicates ``LoadImage(hInst, L"IDI_ICON1", IMAGE_ICON, 0,
    0, LR_DEFAULTSIZE)`` rather than merely enumerating resource names, so a
    pass here means Qt itself will find the icon.
    """
    if sys.platform != "win32":
        return False

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    kernel32.LoadLibraryExW.restype = wintypes.HMODULE
    kernel32.LoadLibraryExW.argtypes = [
        wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD
    ]
    kernel32.FreeLibrary.argtypes = [wintypes.HMODULE]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.LoadImageW.argtypes = [
        wintypes.HMODULE, wintypes.LPCWSTR, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.DestroyIcon.argtypes = [wintypes.HANDLE]

    hmod = kernel32.LoadLibraryExW(
        os.path.abspath(str(exe_path)),
        None,
        _LOAD_LIBRARY_AS_DATAFILE | _LOAD_LIBRARY_AS_IMAGE_RESOURCE,
    )
    if not hmod:
        return False
    try:
        hicon = user32.LoadImageW(
            hmod, str(name), _IMAGE_ICON, 0, 0, _LR_DEFAULTSIZE
        )
        if hicon:
            user32.DestroyIcon(hicon)
            return True
        return False
    finally:
        kernel32.FreeLibrary(hmod)


def group_icon_names(exe_path):
    """List the exe's RT_GROUP_ICON resource names, for diagnostics/tests.

    Returns a list of ``("INT", n)`` / ``("STR", "NAME")`` tuples. Used to
    assert that adding the named group did not disturb PyInstaller's
    integer group #1, which is what Explorer shows for the file itself.
    """
    if sys.platform != "win32":
        return []

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LoadLibraryExW.restype = wintypes.HMODULE
    kernel32.LoadLibraryExW.argtypes = [
        wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD
    ]
    kernel32.FreeLibrary.argtypes = [wintypes.HMODULE]

    # Both the type and the name are passed as raw pointers, because an
    # integer resource id is MAKEINTRESOURCE-encoded *into* the pointer
    # value. Declaring them as LPCWSTR makes ctypes try to marshal those
    # ints as strings and crash.
    ENUMRESNAMEPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p,
    )
    kernel32.EnumResourceNamesW.restype = wintypes.BOOL
    kernel32.EnumResourceNamesW.argtypes = [
        wintypes.HMODULE, ctypes.c_void_p, ENUMRESNAMEPROC, ctypes.c_void_p
    ]

    found = []

    def _collect(_hmod, _typ, name_ptr, _param):
        as_int = name_ptr or 0
        if as_int < 0x10000:
            found.append(("INT", as_int))
        else:
            found.append(("STR", ctypes.wstring_at(as_int)))
        return True

    hmod = kernel32.LoadLibraryExW(
        os.path.abspath(str(exe_path)),
        None,
        _LOAD_LIBRARY_AS_DATAFILE | _LOAD_LIBRARY_AS_IMAGE_RESOURCE,
    )
    if not hmod:
        return []
    try:
        kernel32.EnumResourceNamesW(
            hmod, ctypes.c_void_p(RT_GROUP_ICON), ENUMRESNAMEPROC(_collect), None
        )
    finally:
        kernel32.FreeLibrary(hmod)
    return found


def install_pyinstaller_hook(name=QT_ICON_NAME):
    """Make PyInstaller also publish the icon group under ``name``.

    Wraps ``PyInstaller.utils.win32.icon.CopyIcons``, which ``EXE.assemble``
    calls in its resource-embedding step — before the PKG archive is
    appended and before the PE checksum is fixed up. Call this from the spec
    *before* constructing ``EXE(...)``.

    Returns True if the hook was installed.
    """
    if sys.platform != "win32":
        return False

    from PyInstaller.utils.win32 import icon as _pyi_icon

    if getattr(_pyi_icon.CopyIcons, "_openwater_named_group", False):
        return True  # already hooked (spec re-entry)

    _original = _pyi_icon.CopyIcons

    def _copy_icons_with_named_group(dstpath, srcpath):
        _original(dstpath, srcpath)
        # Only the plain single-.ico form is aliased. The multi-icon and
        # "file.exe,N" forms don't have a predictable RT_ICON id layout, and
        # this app doesn't use them — better to skip than to write a group
        # pointing at ids that may not exist.
        sources = [srcpath] if isinstance(srcpath, (str, os.PathLike)) else list(srcpath)
        if len(sources) != 1 or not str(sources[0]).lower().endswith(".ico"):
            print(
                "[icon] not a single .ico source — skipping the %s alias; "
                "Qt will fall back to the generic window-class icon" % name
            )
            return
        written = add_named_group_icon(dstpath, sources[0], name=name)
        print(
            "[icon] published RT_GROUP_ICON %r (%d bytes) so Qt's "
            "window-class lookup resolves (#223)" % (name, written)
        )

    _copy_icons_with_named_group._openwater_named_group = True
    _pyi_icon.CopyIcons = _copy_icons_with_named_group
    return True


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "check":
        exe = sys.argv[2]
        ok = has_named_group_icon(exe)
        print(f"{exe}")
        print(f"  RT_GROUP_ICON names : {group_icon_names(exe)}")
        print(f"  {QT_ICON_NAME:<19} : {'FOUND' if ok else 'MISSING'}")
        sys.exit(0 if ok else 1)
    if len(sys.argv) >= 3:
        print("wrote", add_named_group_icon(sys.argv[1], sys.argv[2]), "bytes")
        sys.exit(0)
    print(__doc__)
    print("usage: win_icon_resource.py check <exe>")
    print("       win_icon_resource.py <exe> <ico>")
    sys.exit(2)
