"""Pin the Win32 window-class icon to the app icon (issue #223).

Windows resolves the icon for a taskbar button in this order:

    WM_GETICON(ICON_SMALL2 / ICON_SMALL / ICON_BIG)  ->  GCLP_HICONSM
                                                     ->  GCLP_HICON

Qt answers the ``WM_GETICON`` messages from ``QWindow::setIcon`` /
``QGuiApplication::setWindowIcon``, so the first step normally wins. The catch
is that the shell asks with ``SendMessageTimeout(..., SMTO_ABORTIFHUNG)``: if
the GUI thread is busy when Explorer probes a freshly shown window — a slow
disk, a VM, an RDP session, an antivirus scanning a ~400 MB one-folder dist —
the probe times out and the shell paints the *window-class* icon instead, where
it sticks for the life of that window.

Qt registers those classes with ``LoadImage(hInstance, L"IDI_ICON1", ...)`` and
falls back to ``LoadIcon(NULL, IDI_APPLICATION)`` — the generic Windows icon —
when the lookup fails, which it always does in a PyInstaller build (PyInstaller
publishes the icon group under an integer id, not that name). So the fallback
target is the generic icon, and blowing the probe budget strands it.

``scripts/win_icon_resource.py`` fixes that at build time for frozen builds by
also publishing the group under ``IDI_ICON1``. This module is the belt-and-
braces half: it overwrites the class icon at runtime, which additionally covers
running from source (``python.exe`` has no such resource either) and any future
build path that loses the resource.

Everything here is best-effort. A clinical app must never fail to start over a
cosmetic icon, so every failure is logged and swallowed.
"""

import ctypes
import logging
import sys
from ctypes import wintypes

# Handlers are attached to the "openmotion.bloodflow-app" logger in main.py,
# not to the root logger, so a __name__-based logger would never reach the app
# log file. Match utils/single_instance.py.
logger = logging.getLogger("openmotion.bloodflow-app")

GCLP_HICON = -14
GCLP_HICONSM = -34

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010

SM_CXICON = 11
SM_CXSMICON = 49

# HICONs installed on a window class must outlive it — the class holds a raw
# handle and never takes a reference. Keeping them here (and never calling
# DestroyIcon) is the documented way to do this.
_class_icons = []


def apply_window_class_icon(hwnd, ico_path) -> bool:
    """Set the window-class icon for ``hwnd``'s class to ``ico_path``.

    Affects every window of that class in this process, present and future.
    Returns True if at least one of the two class icons was installed.
    """
    if sys.platform != "win32":
        return False
    try:
        hwnd = int(hwnd)
    except (TypeError, ValueError):
        logger.warning("Window-class icon: unusable HWND %r", hwnd)
        return False
    if not hwnd:
        logger.warning("Window-class icon: null HWND")
        return False

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)

        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [
            wintypes.HMODULE, wintypes.LPCWSTR, wintypes.UINT,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.GetSystemMetrics.restype = ctypes.c_int
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]

        # SetClassLongPtrW only exists on 64-bit; on 32-bit it is a macro for
        # SetClassLongW and the symbol is absent from user32.
        set_class = getattr(user32, "SetClassLongPtrW", None) or user32.SetClassLongW
        set_class.restype = ctypes.c_size_t
        set_class.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_size_t]

        installed = []
        for metric, index, label in (
            (SM_CXICON, GCLP_HICON, "large"),
            (SM_CXSMICON, GCLP_HICONSM, "small"),
        ):
            extent = user32.GetSystemMetrics(metric)
            hicon = user32.LoadImageW(
                None, str(ico_path), IMAGE_ICON, extent, extent, LR_LOADFROMFILE
            )
            if not hicon:
                logger.warning(
                    "Window-class icon: LoadImage(%s, %dpx) failed (err %d)",
                    ico_path, extent, ctypes.get_last_error(),
                )
                continue
            _class_icons.append(hicon)
            set_class(wintypes.HWND(hwnd), index, ctypes.c_size_t(hicon))
            installed.append(f"{label}={extent}px")

        if installed:
            logger.info("Window-class icon set (%s)", ", ".join(installed))
            return True
        logger.warning("Window-class icon NOT set — taskbar may show the "
                       "generic Windows icon on a slow start (#223)")
        return False
    except Exception:
        logger.warning("Window-class icon hardening failed", exc_info=True)
        return False
