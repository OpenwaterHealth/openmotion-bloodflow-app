# rthook_libusb_paths.py
import os
import sys

if getattr(sys, "frozen", False) and os.name == "nt":
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    candidates = [
        os.path.join(base, "_internal"),
        os.path.join(base, "_internal", "_vendor", "libusb", "windows", "x64"),
        os.path.join(base, "_internal", "_vendor", "libusb", "windows", "x86"),
        os.path.join(
            base, "_internal", "omotion", "_vendor", "libusb", "windows", "x64"
        ),
        os.path.join(
            base, "_internal", "omotion", "_vendor", "libusb", "windows", "x86"
        ),
    ]
    for p in candidates:
        if os.path.isdir(p):
            try:
                os.add_dll_directory(p)
            except Exception:
                os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")
