# openwater_macos.spec — macOS .app bundle
import os, sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules
from PyInstaller.building.build_main import Analysis, PYZ, EXE, COLLECT, BUNDLE

APP_NAME = "Open-Motion"
ENTRY = "main.py"
ICNS_FILE = "build/AppIcon.icns"

datas = []
hidden = []
binaries = []

# ── Application resources ──
for item in ("main.qml",):
    if os.path.exists(item):
        datas.append((item, "."))
for folder in ("pages", "components", "assets", "models", "config"):
    if os.path.isdir(folder):
        datas.append((folder, folder))

# Bundle the replay sample scan (#314) — loaded into the viewer when no device
# is connected at boot. Located at runtime via
# utils.resource_path.resource_path("resources", "sample_scan.csv").
#
# A targeted file entry rather than adding "resources" to the folder loop
# above: that directory also holds the Windows DFU driver payload
# (OpenMotionDriver-x64.zip), which is dead weight inside a .app.
_SAMPLE_SCAN = os.path.join("resources", "sample_scan.csv")
if os.path.exists(_SAMPLE_SCAN):
    datas.append((_SAMPLE_SCAN, "resources"))
else:
    # Optional, so not fatal — but never silent. Dropping it here is how the
    # runtime file goes missing, and the app can only report the absence after
    # the fact (a warning in the boot log, no sample in the viewer). macOS is
    # research-only (see the darwin clamp in main.py), so the offer dialog is
    # always reachable there and a missing file is always user-visible.
    print(
        f"[spec] WARNING: {_SAMPLE_SCAN!r} not found in {os.getcwd()} — the "
        "build will ship without a replay sample scan; the no-device offer "
        "dialog will appear and then do nothing."
    )

# Ensure icon is bundled
if os.path.exists(ICNS_FILE):
    datas.append((ICNS_FILE, "."))

# ── PyQt6 ──
qt_datas, qt_bins, qt_hidden = collect_all("PyQt6")
datas   += qt_datas
binaries += qt_bins
hidden  += qt_hidden
hidden  += collect_submodules("PyQt6")

# ── omotion SDK ──
om_datas, om_bins, om_hidden = collect_all("omotion")
datas   += om_datas
binaries += om_bins
hidden  += om_hidden

# ── Serial / USB / Network ──
hidden += [
    "serial", "serial.tools", "serial.tools.list_ports",
    "usb", "usb.core", "usb.util", "usb.backend.libusb1",
    "requests", "urllib3", "charset_normalizer", "certifi", "idna",
    "base58", "pandas", "numpy", "matplotlib", "crcmod",
]

# ── Bundle Homebrew libusb (matching current arch) ──
import glob, platform
if platform.machine() == "arm64":
    _libusb_dir = "/opt/homebrew/lib"
else:
    _libusb_dir = "/usr/local/lib"
_libusb = os.path.join(_libusb_dir, "libusb-1.0.0.dylib")
if os.path.exists(_libusb):
    binaries.append((_libusb, "."))

# ── Runtime hook for libusb on macOS ──
runtime_hooks = ["rthook_libusb_macos.py"]

a = Analysis(
    [ENTRY],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["PySide6", "shiboken6", "PySide2", "PyQt5"],
    runtime_hooks=runtime_hooks,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,
    icon=ICNS_FILE,
    upx=False,
)

coll = COLLECT(
    exe,
    a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False,
    name=APP_NAME,
)

app = BUNDLE(
    coll,
    name=f"{APP_NAME}.app",
    icon=ICNS_FILE,
    bundle_identifier="com.openwaterhealth.bloodflow",
    info_plist={
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": os.environ.get("OPENMOTION_VERSION")
            or os.popen("python version.py 2>/dev/null").read().strip() or "0.0.0",
        "CFBundleVersion": os.environ.get("OPENMOTION_VERSION")
            or os.popen("python version.py 2>/dev/null").read().strip() or "0.0.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
        "NSPrincipalClass": "NSApplication",
        "NSAppleScriptEnabled": False,
        # Permissions for USB device access
        "com.apple.security.device.usb": True,
    },
)
