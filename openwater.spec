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

# Anchor to the spec's own directory rather than the build-time CWD:
# os.path.abspath("assets/...") silently resolved against wherever PyInstaller
# happened to be invoked from, so a build launched from another directory got a
# nonexistent icon path.
ICON_FILE = os.path.join(SPECPATH, "assets", "images", "favicon.ico")
if not os.path.exists(ICON_FILE):
    raise SystemExit(f"[spec] FATAL: app icon missing: {ICON_FILE}")

datas = []
hidden = []
binaries = []

# --- resource folders ---
# These are hard failures, not silent skips. The source paths below are still
# CWD-relative, so skipping a missing one would quietly produce a build with no
# QML and no assets at all — strictly worse than the wrong-CWD icon bug that
# motivated anchoring ICON_FILE above.
for item in ("main.qml",):
    if not os.path.exists(item):
        raise SystemExit(
            f"[spec] FATAL: {item!r} not found in {os.getcwd()} — "
            f"run PyInstaller from the repo root ({SPECPATH})."
        )
    datas.append((item, "."))
# NOTE: config/ is no longer a data folder — config/app_config.py and
# config/tec_params.py are Python modules that main.py imports, so the
# Analysis below compiles them into the bundle like any other code (#546).
# processing/ was dropped as a data folder in #557 (shipping it put plaintext
# source in the bundle) and the directory itself was removed in #539.
for folder in ("pages", "components", "assets"):
    if not os.path.isdir(folder):
        raise SystemExit(
            f"[spec] FATAL: required resource folder {folder!r} not found in "
            f"{os.getcwd()} — run PyInstaller from the repo root ({SPECPATH})."
        )
    datas.append((folder, folder))
for folder in ("models",):  # optional
    if os.path.isdir(folder):
        datas.append((folder, folder))

# Bundle the replay sample scan (#314) — loaded into the viewer when no
# device is connected at boot. Located at runtime via
# utils.resource_path.resource_path("resources", "sample_scan.csv").
_SAMPLE_SCAN = os.path.join("resources", "sample_scan.csv")
if os.path.exists(_SAMPLE_SCAN):
    datas.append((_SAMPLE_SCAN, "resources"))
else:
    # Optional, so not fatal — but never silent. Dropping it here is how the
    # runtime file goes missing, and the app can only report the absence
    # after the fact (a warning in the boot log, no sample in the viewer).
    print(
        f"[spec] WARNING: {_SAMPLE_SCAN!r} not found in {os.getcwd()} — the "
        "build will ship without a replay sample scan; research builds will "
        "log 'sample scan unavailable' and the offer dialog will do nothing."
    )

# Ensure the icon is explicitly included (also reachable via the `assets` tree
# above; the runtime class-icon hardening in utils/win_taskbar_icon.py loads it
# from disk, so a missing copy would be a silent downgrade).
datas.append((ICON_FILE, "assets/images"))

# --- PyQt6 (keep as before) ---
# include_py_files=False on every collect_all() below: the default (True)
# copies each .py of the package into the bundle as a loose data file next
# to the bytecode the Analysis already compiles into the PYZ. The frozen
# finder resolves the PYZ first, so those copies are never imported - they
# only ship the source as plaintext. 1.5.3-dev.0 carried 100 omotion files
# that way, including the laser_params.py / fpga_model.py modules that had
# just been converted from JSON (#557). Guarded by
# tests/test_pyinstaller_spec.py.
qt_datas, qt_bins, qt_hidden = collect_all("PyQt6", include_py_files=False)
datas   += qt_datas
binaries += qt_bins
hidden  += qt_hidden
hidden  += collect_submodules("PyQt6")

# --- ✅ add omotion explicitly ---
# Put the package's parent directory on pathex. A PEP 660 editable install
# (pip install -e ../openmotion-sdk, the documented dev setup) exposes
# omotion through an import-hook finder that PyInstaller's module analysis
# does not consult, so the Analysis compiled no omotion module into the PYZ
# and the frozen app imported the SDK from the loose .py copies that
# collect_all() used to ship as data. With those gone (#557) the package
# must be reachable as a plain directory. For a regular wheel/git install
# (CI) the directory is site-packages and this changes nothing. The script
# directory stays first on the analysis path, so the app's own packages
# (config/, utils/) cannot be shadowed by anything in the SDK checkout.
import importlib.util as _ilu
_om_spec = _ilu.find_spec("omotion")
if _om_spec is None or not _om_spec.submodule_search_locations:
    raise SystemExit(
        "[spec] FATAL: omotion is not importable in the build environment"
    )
pathex = [os.path.dirname(list(_om_spec.submodule_search_locations)[0])]
print(f"[spec] omotion package resolved under {pathex[0]}")
om_datas, om_bins, om_hidden = collect_all(
    "omotion", include_py_files=False
)
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
    _sc_datas, _sc_bins, _sc_hidden = collect_all(
        "sqlcipher3", include_py_files=False
    )
    datas += _sc_datas
    binaries += _sc_bins
    hidden += _sc_hidden
except Exception:
    pass

# Fail the BUILD, not the field, when a clinical package is missing its crypto.
# PyInstaller can only bundle what is installed in the build environment, so an
# env without these silently produces a clinical app that dies with
# ModuleNotFoundError on first launch. build_and_zip.ps1 does not install
# requirements.txt, so a local build inherits whatever happens to be in the
# conda env - which is exactly how this shipped broken once. Research builds do
# not need them, so only gate on clinicalMode.
import re as _re
try:
    with open(os.path.join(SPECPATH, "config", "app_config.py"),
              encoding="utf-8") as _f:
        _m = _re.search(r"^CLINICAL_MODE = (True|False)$", _f.read(), _re.M)
    _is_clinical = bool(_m and _m.group(1) == "True")
except Exception:
    _is_clinical = False
print(f"[spec] building the {'Clinical' if _is_clinical else 'Research'} "
      "variant (CLINICAL_MODE stamp in config/app_config.py)")
if _is_clinical:
    _missing = []
    for _mod in ("keyring", "sqlcipher3"):
        try:
            __import__(_mod)
        except ImportError:
            _missing.append(_mod)
    if _missing:
        raise SystemExit(
            "\n*** BUILD ABORTED: clinicalMode=true but these encryption packages "
            f"are not installed in the build environment: {', '.join(_missing)}.\n"
            "    PyInstaller cannot bundle what is not installed, so this build "
            "would produce a clinical app that fails on first launch.\n"
            "    Fix:  pip install -r requirements.txt\n"
        )

# Optional: if you also have a separate 'libusb' wheel installed, this won't hurt
try:
    binaries += collect_dynamic_libs("libusb")
except Exception:
    pass

# ---------- MIRROR omotion vendored libusb under <bundle>\_vendor ----------
# Some builds only carry the vendored files inside omotion\_vendor\... within
# the bundle. We duplicate those files to _vendor\... at the bundle root (the
# onefile extraction directory; _internal\ in a onedir build) so the wheel's
# _dll_dir() and rthook_libusb_paths.py can find them.
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

# Mixed-Qt guards, plus the app self-updater for a clinical build (#543,
# tracker M-02): motion_connector only imports app_updater when the stamped
# CLINICAL_MODE is False, and excluding it here keeps the module out of the
# clinical PYZ entirely rather than merely unused. Guarded by
# tests/test_updater_compiled_out.py.
_excludes = ["PySide6", "shiboken6", "PySide2", "PyQt5"]
if _is_clinical:
    _excludes.append("app_updater")

a = Analysis(
    [ENTRY],
    pathex=pathex,                  # SDK parent dir; see the omotion block
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=_excludes,
    runtime_hooks=runtime_hooks,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# ---------- Qt window-class icon (issue #223) ----------
# Qt sets the Win32 *window-class* icon with LoadImage(hInst, L"IDI_ICON1", ...)
# and falls back to the generic IDI_APPLICATION when that name isn't found.
# PyInstaller publishes the icon group under the *integer* id 1, which a name
# lookup never matches — so the class icon is generic in every PyInstaller+Qt
# build. The shell falls back to the class icon whenever its WM_GETICON probe
# of a freshly shown window times out (SMTO_ABORTIFHUNG), which is why the
# generic taskbar icon showed up intermittently and only on slower machines.
#
# The hook makes PyInstaller publish a second, *named* group alongside its own.
# It must be installed before EXE() runs: EXE.assemble() embeds resources
# before appending the PKG archive and before fixing up the PE checksum, and
# rewriting resources after that point truncates the appended archive and
# produces an exe that cannot start.
if sys.platform == "win32":
    sys.path.insert(0, os.path.join(SPECPATH, "scripts"))
    from win_icon_resource import install_pyinstaller_hook, has_named_group_icon
    install_pyinstaller_hook()

# ---------- one-file packaging (#547) ----------
# Every shipped byte (the bytecode archive, Qt and native DLLs, QML, assets,
# the sample scan) is appended to this one PE, so the Authenticode signature
# installer/sign.ps1 applies covers all of it and any modification is
# detectable with signtool or the file's Digital Signatures tab (tracker
# V-04, V-05, R-13). No COLLECT step: the build output is the exe alone.
#
# Residual risk, recorded rather than hidden: at every launch the
# bootloader extracts the payload into a per-process, randomly named
# directory under %TEMP% and deletes it on exit. Nothing verifies the
# extracted copies before they are loaded (PyInstaller has no such check),
# so a process that can write that directory in the interval between
# extraction and load is outside what signing defends against. Every
# self-extracting bundler shares that window, Nuitka's onefile included
# (#548); the on-disk artifact is what the signature protects.
exe_gui = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=APP_NAME,
    console=False,
    icon=ICON_FILE,
    upx=False,   # safer for DLLs on Windows
)

# Verify on the artifact that actually ships. This also catches a warm build/
# tree: PyInstaller's EXE cache compares the icon *path*, not its contents, so
# a cached exe can silently carry stale resources — fail loudly instead.
if sys.platform == "win32":
    _dist_exe = os.path.join(DISTPATH, APP_NAME + ".exe")
    if not has_named_group_icon(_dist_exe):
        raise SystemExit(
            f"[spec] FATAL: {_dist_exe} has no 'IDI_ICON1' icon resource — Qt "
            f"would register its window classes with the generic Windows icon "
            f"(#223). Rebuild with --clean."
        )
    print(f"[spec] verified 'IDI_ICON1' window-class icon in {_dist_exe}")
    # A onefile build ships as exactly one file. A leftover onedir tree from
    # an earlier build in the same dist directory would be zipped and
    # harvested into the MSI next to the exe, so refuse to call this a build.
    _leftover = os.path.join(DISTPATH, "_internal")
    if os.path.isdir(_leftover):
        raise SystemExit(
            f"[spec] FATAL: {_leftover} exists next to the onefile exe — a "
            f"stale onedir build; delete the dist directory and rebuild."
        )
    print(f"[spec] onefile build: {_dist_exe} "
          f"({os.path.getsize(_dist_exe) / 1e6:.1f} MB)")
