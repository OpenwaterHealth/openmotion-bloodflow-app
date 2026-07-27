# openwater.spec — drop-in replacement
import os
import sys
import struct
from PyInstaller.utils.hooks import (
    collect_all, collect_dynamic_libs, collect_submodules
)
from PyInstaller.building.build_main import Analysis, PYZ, EXE, COLLECT

APP_NAME = "Open-Motion"
ENTRY = "main.py"
ICON_FILE = os.path.abspath("assets/images/favicon.ico")

datas = []
hidden = []
binaries = []

# --- your existing resource folders (keep what you already had) ---
for item in ("main.qml",):
    if os.path.exists(item):
        datas.append((item, "."))
for folder in ("pages", "components", "assets", "models", "config", "models", "processing"):
    if os.path.isdir(folder):
        datas.append((folder, folder))

# Bundle the replay sample scan (#314) — loaded into the viewer when no
# device is connected at boot. Located at runtime via
# utils.resource_path.resource_path("resources", "sample_scan.csv").
_SAMPLE_SCAN = os.path.join("resources", "sample_scan.csv")
if os.path.exists(_SAMPLE_SCAN):
    datas.append((_SAMPLE_SCAN, "resources"))

# Ensure the icon is explicitly included
if os.path.exists(ICON_FILE):
    datas.append((ICON_FILE, "assets/images"))

# --- PyQt6 (keep as before) ---
qt_datas, qt_bins, qt_hidden = collect_all("PyQt6")
datas   += qt_datas
binaries += qt_bins
hidden  += qt_hidden
hidden  += collect_submodules("PyQt6")

# --- ✅ add omotion explicitly ---
om_datas, om_bins, om_hidden = collect_all("omotion")
datas   += om_datas
binaries += om_bins
hidden  += om_hidden

# --- force include pyserial / pyusb dependency ---
hidden += [
    "serial",
    "serial.tools",
    "serial.tools.list_ports",
    "usb",
    "usb.core",
    "usb.util",
    "usb.backend.libusb1",
]

# --- force include omotion's + our own third-party deps ---
# collect_all("omotion") above only walks omotion's own submodules/data, not its
# third-party deps, and PyInstaller's static analysis doesn't trace into them
# either - same class of gap as the serial/usb block above. requests backs
# omotion.firmware_update; crcmod backs omotion's i2c packet framing; base58
# is imported directly by motion_connector.py.
hidden += [
    "requests",
    "urllib3",
    "certifi",
    "charset_normalizer",
    "idna",
    "crcmod",
    "base58",
]

# --- scan-DB encryption stack (clinical builds) ---
# Belt-and-braces, NOT a fix for a known break: a frozen build was verified to
# pick these up already, because PyInstaller's bytecode scan finds the lazy
# `import sqlcipher3` / `import keyring` inside omotion.db_open/db_key, and
# PyInstaller ships its own keyring hook for the entry-point-discovered
# backends. Listing them explicitly means a refactor of those import sites (or
# a hook change) cannot silently produce a clinical build that crashes on the
# first scan or, worse, cannot open its own encrypted database.
hidden += [
    "sqlcipher3",
    "sqlcipher3.dbapi2",
    "keyring",
    "keyring.backends",
    "keyring.backends.Windows",          # WinVaultKeyring - found via entry points
    "win32ctypes.core",                  # backs the Windows keyring backend
    "win32ctypes.pywin32.win32cred",
    "win32ctypes.pywin32.pywintypes",
]
try:
    # sqlcipher3 is a single native extension with OpenSSL statically linked,
    # so there are no side-car DLLs to chase - collect_all still future-proofs
    # against that changing.
    _sc_datas, _sc_bins, _sc_hidden = collect_all("sqlcipher3")
    datas += _sc_datas
    binaries += _sc_bins
    hidden += _sc_hidden
except Exception:
    pass

# Optional: if you also have a separate 'libusb' wheel installed, this won't hurt
try:
    binaries += collect_dynamic_libs("libusb")
except Exception:
    pass

# ---------- MIRROR omotion vendored libusb under _internal\_vendor ----------
# Some builds only carry the vendored files inside _internal\omotion\_vendor\...
# We duplicate those files to _internal\_vendor\... so the wheel's _dll_dir() can find them.
def _norm(p): return p.replace("/", os.sep).replace("\\", os.sep)

arch = "x64" if 8 * struct.calcsize("P") == 64 else "x86"
needle = _norm(os.path.join("omotion", "_vendor", "libusb", "windows"))
dst_base_vendor = _norm(os.path.join("_vendor", "libusb", "windows"))

def _mirror_vendor_from_collected(collected_list):
    """Look through (src, dst) entries; if dst contains omotion\\_vendor\\libusb\\windows\\<arch>,
       add duplicates into _internal\\_vendor\\libusb\\windows\\<arch>."""
    added = 0
    for src, dst in list(collected_list):  # iterate over a snapshot
        ndst = _norm(dst)
        if needle in ndst:
            # Extract arch subdir if present
            parts = ndst.split(os.sep)
            try:
                idx = parts.index("windows")
                arch_part = parts[idx + 1] if idx + 1 < len(parts) else arch
            except ValueError:
                arch_part = arch

            target_dir = os.path.join(dst_base_vendor, arch_part)
            # Add as a binary to ensure it lands under _internal
            binaries.append((src, target_dir))
            added += 1
            
    print(f"[spec] Mirrored {added} vendored libusb file(s) to {dst_base_vendor}\\<arch>")

# Mirror from both omotion datas and bins (some wheels mark them as datas)
_mirror_vendor_from_collected(om_datas)
_mirror_vendor_from_collected(om_bins)
# ---------------------------------------------------------------------------

# Optionally add a runtime hook to put these dirs on the DLL path for Windows
runtime_hooks = ["rthook_libusb_paths.py"]

a = Analysis(
    [ENTRY],
    pathex=[],                      # you can leave this empty now
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=['PySide6','shiboken6','PySide2','PyQt5'],  # avoid mixed Qt
    runtime_hooks=runtime_hooks,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe_gui = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,
    icon=ICON_FILE,
    upx=False   # safer for DLLs on Windows
)

coll = COLLECT(
    exe_gui,
    a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, upx_exclude=[],
    name=APP_NAME
)
