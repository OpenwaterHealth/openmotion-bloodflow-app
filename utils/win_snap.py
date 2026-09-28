"""Windows Snap for the frameless main window (issue #642).

``main.qml`` draws its own title bar, so the window is created with
``Qt.FramelessWindowHint``. Qt implements that as a bare ``WS_POPUP`` HWND,
and Windows only offers Snap (drag to an edge or corner, Win+Arrow, snap
layouts via Win+Z) and native edge resizing to windows that carry the
standard frame styles, ``WS_THICKFRAME`` and ``WS_MAXIMIZEBOX`` in particular.

The fix is the usual custom-chrome recipe:

* put ``WS_OVERLAPPEDWINDOW``'s styles back on the HWND, so the shell treats
  it as a normal resizable top-level window;
* answer ``WM_NCCALCSIZE`` so the non-client area is empty and no native
  frame or caption is painted: the client area is the whole window, clamped
  to the monitor's work area while maximized (a maximized ``WS_THICKFRAME``
  window otherwise overhangs the screen by its frame width and covers the
  taskbar);
* answer ``WM_NCHITTEST`` with resize codes on a thin band around the edges,
  so the borders resize natively (and snap-resize against neighbours).

Dragging stays in QML: the header's ``window.startSystemMove()`` hands the
move loop to Windows, which snaps once the styles are present.

Everything here is best-effort. A clinical app must never fail to start over
window chrome, so every failure is logged and swallowed and the window simply
keeps its old (non-snapping) behaviour.
"""

import ctypes
import logging
import sys
from ctypes import wintypes

from PyQt6.QtCore import QAbstractNativeEventFilter

# Handlers are attached to the "openmotion.bloodflow-app" logger in main.py,
# not to the root logger. Match utils/win_taskbar_icon.py.
logger = logging.getLogger("openmotion.bloodflow-app")

GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
_SNAP_STYLES = (WS_CAPTION | WS_SYSMENU | WS_THICKFRAME
                | WS_MINIMIZEBOX | WS_MAXIMIZEBOX)

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084

HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT = 10, 11, 12, 13, 14
HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17

MONITOR_DEFAULTTONEAREST = 2

# Width of the native resize band, in 96-DPI pixels. Thin on purpose: it
# sits on top of the QML content (header buttons, plot edges).
_RESIZE_BORDER_DIP = 6


class _NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", ctypes.c_void_p)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def _user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [
        wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.IsZoomed.argtypes = [wintypes.HWND]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetDpiForWindow.restype = wintypes.UINT
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.MonitorFromRect.restype = wintypes.HMONITOR
    user32.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
    user32.GetMonitorInfoW.argtypes = [
        wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
    return user32


def _signed16(value):
    value &= 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


class _SnapFilter(QAbstractNativeEventFilter):
    def __init__(self, hwnd, user32):
        super().__init__()
        self._hwnd = hwnd
        self._user32 = user32

    def nativeEventFilter(self, eventType, message):
        try:
            if bytes(eventType) != b"windows_generic_MSG":
                return False, 0
            msg = wintypes.MSG.from_address(int(message))
            if (msg.hWnd or 0) != self._hwnd:
                return False, 0
            if msg.message == WM_NCCALCSIZE and msg.wParam:
                return self._on_nccalcsize(msg.lParam)
            if msg.message == WM_NCHITTEST:
                return self._on_nchittest(msg.lParam)
        except Exception:
            logger.exception("Window snap: native event filter failed")
        return False, 0

    def _on_nccalcsize(self, lparam):
        # Returning 0 without touching rgrc[0] makes the client area the
        # whole proposed window rect: no native frame, no caption.
        if self._user32.IsZoomed(self._hwnd):
            params = _NCCALCSIZE_PARAMS.from_address(lparam)
            proposed = params.rgrc[0]
            monitor = self._user32.MonitorFromRect(
                ctypes.byref(proposed), MONITOR_DEFAULTTONEAREST)
            info = _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
            if monitor and self._user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                params.rgrc[0] = info.rcWork
        return True, 0

    def _on_nchittest(self, lparam):
        if self._user32.IsZoomed(self._hwnd):
            return False, 0
        x, y = _signed16(lparam), _signed16(lparam >> 16)
        rect = wintypes.RECT()
        if not self._user32.GetWindowRect(self._hwnd, ctypes.byref(rect)):
            return False, 0
        dpi = self._user32.GetDpiForWindow(self._hwnd) or 96
        band = max(1, round(_RESIZE_BORDER_DIP * dpi / 96))
        left = x < rect.left + band
        right = x >= rect.right - band
        top = y < rect.top + band
        bottom = y >= rect.bottom - band
        code = {
            (True, False, True, False): HTTOPLEFT,
            (False, True, True, False): HTTOPRIGHT,
            (True, False, False, True): HTBOTTOMLEFT,
            (False, True, False, True): HTBOTTOMRIGHT,
            (True, False, False, False): HTLEFT,
            (False, True, False, False): HTRIGHT,
            (False, False, True, False): HTTOP,
            (False, False, False, True): HTBOTTOM,
        }.get((left, right, top, bottom))
        if code is None:
            return False, 0  # interior: Qt answers HTCLIENT
        return True, code


# The filter must outlive the window: Qt holds a raw pointer to it.
_filters = []


def enable_window_snap(app, hwnd) -> bool:
    """Make the frameless top-level window ``hwnd`` snappable on Windows.

    Returns True if the styles were applied and the filter installed.
    """
    if sys.platform != "win32":
        return False
    try:
        hwnd = int(hwnd)
    except (TypeError, ValueError):
        logger.warning("Window snap: unusable HWND %r", hwnd)
        return False
    if not hwnd:
        logger.warning("Window snap: null HWND")
        return False

    try:
        user32 = _user32()
        # Install the filter first so the WM_NCCALCSIZE sent by the
        # SWP_FRAMECHANGED below already sees an empty non-client area.
        snap_filter = _SnapFilter(hwnd, user32)
        app.installNativeEventFilter(snap_filter)
        _filters.append(snap_filter)

        style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
        user32.SetWindowLongPtrW(hwnd, GWL_STYLE, style | _SNAP_STYLES)
        user32.SetWindowPos(
            hwnd, None, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
            | SWP_FRAMECHANGED)
    except Exception:
        logger.exception("Window snap: could not enable")
        return False
    logger.info("Window snap: enabled for HWND 0x%x", hwnd)
    return True
