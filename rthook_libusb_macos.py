# rthook_libusb_macos.py — PyInstaller runtime hook for macOS libusb
# Ensures pyusb can locate the bundled libusb dylib inside the .app bundle.
import os
import sys

if getattr(sys, "frozen", False) and sys.platform == "darwin":
    bundle_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))

    # The real dylib file (not symlinks)
    dylib_path = os.path.join(bundle_dir, "libusb-1.0.0.dylib")
    if os.path.exists(dylib_path):
        # Set environment variable so usb.backend.libusb1 can find it
        os.environ["DYLD_LIBRARY_PATH"] = (
            bundle_dir + ":" + os.environ.get("DYLD_LIBRARY_PATH", "")
        )
        # Pre-load the library
        try:
            import ctypes
            ctypes.CDLL(dylib_path)
        except OSError:
            pass  # Fall back to system libusb
