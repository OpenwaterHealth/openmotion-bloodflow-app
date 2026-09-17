# rthook_libusb_paths.py
import os
import sys

# Bundle roots to probe for the vendored libusb DLLs: the onefile extraction
# directory holds the payload flat (sys._MEIPASS itself, #547); a onedir build
# keeps it under _internal\. Both are listed so the hook is layout-agnostic.
LAYOUT_ROOTS = ("", "_internal")
VENDOR_DIRS = (
    (),
    ("_vendor", "libusb", "windows", "x64"),
    ("_vendor", "libusb", "windows", "x86"),
    ("omotion", "_vendor", "libusb", "windows", "x64"),
    ("omotion", "_vendor", "libusb", "windows", "x86"),
)

if getattr(sys, "frozen", False) and os.name == "nt":
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    candidates = [
        os.path.join(base, root, *parts) if root else os.path.join(base, *parts)
        for root in LAYOUT_ROOTS
        for parts in VENDOR_DIRS
        if root or parts
    ]
    for p in candidates:
        if os.path.isdir(p):
            try:
                os.add_dll_directory(p)
            except Exception:
                os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")
