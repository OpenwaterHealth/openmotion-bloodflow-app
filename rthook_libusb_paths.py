# rthook_libusb_paths.py — PyInstaller runtime hook.
# Runs before main.py in a PyInstaller build and puts the vendored libusb DLL
# directories on the DLL search path. The implementation is shared with the
# Nuitka build, which has no runtime hooks and calls the same function from
# main.py instead (#548); keep this file a thin shim.
import os
import sys

if getattr(sys, "frozen", False) and os.name == "nt":
    from utils.libusb_paths import register_vendored_libusb

    register_vendored_libusb(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
