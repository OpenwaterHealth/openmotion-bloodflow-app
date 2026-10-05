"""Operator re-authentication against the logged-in Windows account (#703).

Deleting scans and opening or exporting the audit log ask the operator for
their own Windows password instead of a shared one, so the audit trail names
who did it. ``LogonUserW`` with a network logon validates the password
without starting a session (the token is closed straight away); Windows'
account-lockout policy and logon auditing apply, and the app stores nothing.

Only Windows is supported. :func:`supported` is False elsewhere and the
caller picks the fallback (macOS builds are Research-only).
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys

logger = logging.getLogger("openmotion.bloodflow-app.operator_auth")

_LOGON32_LOGON_NETWORK = 3
_LOGON32_PROVIDER_DEFAULT = 0
_NAME_SAM_COMPATIBLE = 2


def supported() -> bool:
    """True where :func:`verify_password` can check an OS account."""
    return sys.platform == "win32"


def _windows_account() -> str | None:
    """``DOMAIN\\user`` of the process owner, from the OS (never the
    environment)."""
    from ctypes import wintypes
    try:
        secur32 = ctypes.WinDLL("secur32")
        size = wintypes.ULONG(0)
        secur32.GetUserNameExW(_NAME_SAM_COMPATIBLE, None, ctypes.byref(size))
        if size.value:
            buf = ctypes.create_unicode_buffer(size.value)
            if secur32.GetUserNameExW(_NAME_SAM_COMPATIBLE, buf, ctypes.byref(size)):
                return buf.value
    except Exception:
        logger.warning("GetUserNameExW failed", exc_info=True)
    try:
        advapi32 = ctypes.WinDLL("advapi32")
        size = wintypes.DWORD(257)
        buf = ctypes.create_unicode_buffer(size.value)
        if advapi32.GetUserNameW(buf, ctypes.byref(size)):
            return f".\\{buf.value}"
    except Exception:
        logger.warning("GetUserNameW failed", exc_info=True)
    return None


def current_account() -> str:
    """The logged-in operator: ``DOMAIN\\user`` on Windows, the passwd
    name elsewhere, ``"unknown"`` if the OS won't say."""
    if supported():
        return _windows_account() or "unknown"
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return "unknown"


def _logon(user: str, domain: str, password: str) -> bool:
    """One ``LogonUserW`` network logon; True iff Windows accepts it."""
    from ctypes import wintypes
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32")
    advapi32.LogonUserW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.LogonUserW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    ok = advapi32.LogonUserW(
        user, domain, password,
        _LOGON32_LOGON_NETWORK, _LOGON32_PROVIDER_DEFAULT, ctypes.byref(token),
    )
    if ok:
        kernel32.CloseHandle(token)
    else:
        logger.info("LogonUserW refused the credential (error %d)",
                    ctypes.get_last_error())
    return bool(ok)


def verify_password(password) -> bool:
    """True iff ``password`` is the logged-in Windows account's password.

    Always False off Windows, for an empty password, or when the account
    can't be determined. Never raises.
    """
    if not supported() or not isinstance(password, str) or not password:
        return False
    account = _windows_account()
    if not account:
        return False
    domain, _, user = account.rpartition("\\")
    try:
        return _logon(user, domain or ".", password)
    except Exception:
        logger.warning("LogonUserW call failed", exc_info=True)
        return False
