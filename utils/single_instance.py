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

# Module-level state for lock management
_mutex = None

# Get logger (may not be configured when this module is imported)
logger = logging.getLogger("openmotion.bloodflow-app")


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

    # Only enforce single-instance on Windows
    if sys.platform != "win32":
        return True

    # Use named mutex on Windows
    try:
        import ctypes

        # Create a named mutex
        mutex_name = f"Global\\{app_name}"
        _mutex = ctypes.windll.kernel32.CreateMutexW(
            None,  # Default security attributes
            True,  # Initial owner
            mutex_name,
        )

        # Check if mutex already exists (GetLastError returns ERROR_ALREADY_EXISTS)
        last_error = ctypes.windll.kernel32.GetLastError()

        if last_error == 183:  # ERROR_ALREADY_EXISTS
            # Another instance is running
            ctypes.windll.kernel32.CloseHandle(_mutex)
            _mutex = None
            return False

        # Register cleanup function
        atexit.register(cleanup_single_instance)
        return True

    except Exception as e:
        # Logging may not be configured yet, so use print as fallback
        try:
            logger.error(f"Failed to create mutex: {e}")
        except:
            print(f"Error: Failed to create mutex: {e}")
        # On error, allow the instance to proceed (fail open)
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
            pass
