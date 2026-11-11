# utils/single_instance.py
"""
Single-instance application utilities (Windows only).

Provides functionality to ensure only one instance of the application
can run at a time on Windows, preventing multiple instances from being launched.
On non-Windows platforms, this module allows multiple instances.
"""

import sys
import atexit
import logging

# Handle of the named mutex this process holds, or None.
_mutex = None

logger = logging.getLogger("openmotion.bloodflow-app")

_ERROR_ALREADY_EXISTS = 183


def check_single_instance(app_name: str = "OpenWaterBloodflowApp") -> bool:
    """
    Check if another instance is already running (Windows only).

    On Windows, uses a named mutex to detect if another instance is running.
    On non-Windows platforms, always returns True (allows multiple instances).

    Args:
        app_name: Name identifier for the application (used for mutex naming)

    Returns:
        True if this is the first instance (or on non-Windows),
        False if another instance is already running (Windows only).
    """
    global _mutex

    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # Default security attributes; this process is the initial owner.
        _mutex = kernel32.CreateMutexW(None, True, f"Global\\{app_name}")
        if kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(_mutex)
            _mutex = None
            return False
        atexit.register(cleanup_single_instance)
        return True
    except Exception:
        # Fail open: an instance that cannot create the mutex still starts.
        # Logging may not be configured yet; a handler-less logger swallows
        # this safely.
        logger.error("Failed to create single-instance mutex", exc_info=True)
        return True


def cleanup_single_instance():
    """Clean up the single-instance mutex (Windows only)."""
    global _mutex

    if _mutex:
        try:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(_mutex)
            _mutex = None
        except Exception:
            logger.debug("single-instance mutex cleanup failed", exc_info=True)
