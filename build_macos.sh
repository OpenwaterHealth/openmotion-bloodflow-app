#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
#  build_macos.sh — Build OpenWater Bloodflow as a macOS .app + DMG
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── Configuration ─────────────────────────────────────────────────────
APP_NAME="OpenWater Bloodflow"
BUNDLE_ID="com.openwaterhealth.bloodflow"
ICON_SRC="assets/images/favicon.png"
SPEC_FILE="openwater_macos.spec"
DIST_DIR="dist"
BUILD_DIR="build"

# Resolve version from git
VERSION="$(python version.py 2>/dev/null || echo "0.0.0")"
DMG_NAME="OpenWaterBloodflow-${VERSION}-macOS.dmg"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Building ${APP_NAME} v${VERSION} for macOS                 "
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# ── Step 1: Generate .icns icon ───────────────────────────────────────
echo "▸ Step 1/5: Generating macOS icon (.icns) …"
ICONSET_DIR="build/AppIcon.iconset"
ICNS_FILE="build/AppIcon.icns"

mkdir -p "$ICONSET_DIR"

# Generate all required sizes from the source PNG
for SIZE in 16 32 64 128 256 512; do
    sips -z $SIZE $SIZE "$ICON_SRC" --out "$ICONSET_DIR/icon_${SIZE}x${SIZE}.png" >/dev/null 2>&1
done
# Retina variants (@2x)
for SIZE in 16 32 128 256 512; do
    DOUBLE=$((SIZE * 2))
    sips -z $DOUBLE $DOUBLE "$ICON_SRC" --out "$ICONSET_DIR/icon_${SIZE}x${SIZE}@2x.png" >/dev/null 2>&1
done

iconutil -c icns "$ICONSET_DIR" -o "$ICNS_FILE"
echo "  ✓ Created $ICNS_FILE"

# ── Step 2: Ensure PyInstaller is available ───────────────────────────
echo ""
echo "▸ Step 2/5: Checking PyInstaller …"
if ! python -m PyInstaller --version >/dev/null 2>&1; then
    echo "  Installing PyInstaller …"
    pip install pyinstaller >/dev/null
fi
echo "  ✓ PyInstaller $(python -m PyInstaller --version 2>&1)"

# ── Step 3: Build with PyInstaller ────────────────────────────────────
echo ""
echo "▸ Step 3/5: Running PyInstaller …"

# Generate the spec file
cat > "$SPEC_FILE" << 'SPEC_EOF'
# openwater_macos.spec — macOS .app bundle
import os, sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules
from PyInstaller.building.build_main import Analysis, PYZ, EXE, COLLECT, BUNDLE

APP_NAME = "OpenWater Bloodflow"
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

# Ensure icon is bundled
if os.path.exists(ICNS_FILE):
    datas.append((ICNS_FILE, "."))

# ── PyQt6 ──
qt_datas, qt_bins, qt_hidden = collect_all("PyQt6")
datas   += qt_datas
binaries += qt_bins
hidden  += qt_hidden
hidden  += collect_submodules("PyQt6")
hidden  += ["qasync"]

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
        "CFBundleShortVersionString": os.popen("python version.py 2>/dev/null").read().strip() or "0.0.0",
        "CFBundleVersion": os.popen("python version.py 2>/dev/null").read().strip() or "0.0.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
        "NSPrincipalClass": "NSApplication",
        "NSAppleScriptEnabled": False,
        # Permissions for USB device access
        "com.apple.security.device.usb": True,
    },
)
SPEC_EOF

python -m PyInstaller --noconfirm --clean "$SPEC_FILE" 2>&1 | tail -5
echo "  ✓ Built ${DIST_DIR}/${APP_NAME}.app"

# ── Step 4: Code-sign the .app bundle ─────────────────────────────────
echo ""
echo "▸ Step 4/5: Signing .app bundle …"

APP_PATH="${DIST_DIR}/${APP_NAME}.app"

# Strip any quarantine / provenance attributes that would block launch
xattr -cr "$APP_PATH"

# Remove stray build artifacts (e.g. .cpp.o files Qt ships by mistake)
# These are not needed at runtime and cause codesign --deep --strict to fail.
# Remove both the real directories and any symlinks pointing to them.
find "$APP_PATH" -name "objects-RelWithDebInfo" -exec rm -rf {} + 2>/dev/null || true
# Also clean up any broken symlinks left behind
find "$APP_PATH" -type l ! -exec test -e {} \; -delete 2>/dev/null || true

# Sign inside-out: codesign --deep sometimes misses nested .so/.dylib files
# inside framework bundles. We sign every Mach-O binary individually first,
# then seal the top-level .app bundle.
echo "  Signing nested binaries …"
find "$APP_PATH" \( -name "*.so" -o -name "*.dylib" \) -print0 | \
    xargs -0 -n1 codesign --force --sign - 2>/dev/null || true
find "$APP_PATH/Contents/Frameworks" -name "*.framework" -print0 | \
    xargs -0 -n1 codesign --force --sign - 2>/dev/null || true
# Sign the main executable and the top-level bundle
codesign --force --sign - "$APP_PATH/Contents/MacOS/${APP_NAME}"
codesign --force --sign - "$APP_PATH"
echo "  ✓ Signed (ad-hoc)"

# ── Step 5: Create DMG ────────────────────────────────────────────────
echo ""
echo "▸ Step 5/5: Creating DMG …"
DMG_STAGING="${BUILD_DIR}/dmg-staging"
DMG_TEMP="${BUILD_DIR}/${DMG_NAME}"
DMG_FINAL="${DIST_DIR}/${DMG_NAME}"

# Verify the .app exists
if [ ! -d "$APP_PATH" ]; then
    echo "  ✗ Error: $APP_PATH not found. PyInstaller build may have failed."
    exit 1
fi

# Clean and prepare staging area
rm -rf "$DMG_STAGING"
mkdir -p "$DMG_STAGING"

# Copy .app into staging and strip quarantine xattrs so the DMG copy is clean
cp -R "$APP_PATH" "$DMG_STAGING/"
xattr -cr "$DMG_STAGING/${APP_NAME}.app" 2>/dev/null || true

# Create Applications symlink
ln -s /Applications "$DMG_STAGING/Applications"

# Create a background image with instructions
# Using a simple approach — create a .DS_Store-friendly layout
mkdir -p "$DMG_STAGING/.background"
python3 - "$DMG_STAGING/.background/background.png" << 'PYEOF'
import sys
try:
    from PIL import Image, ImageDraw, ImageFont
    width, height = 660, 400
    img = Image.new("RGB", (width, height), "#1a1a2e")
    draw = ImageDraw.Draw(img)
    # Gradient-like header bar
    for y in range(80):
        shade = int(40 + y * 0.5)
        draw.line([(0, y), (width, y)], fill=(shade, shade, shade + 20))
    # Title
    draw.text((width // 2, 30), "OpenWater Bloodflow", fill="white", anchor="mt")
    # Arrow hint
    draw.text((width // 2, height // 2 + 40), "Drag app to Applications →", fill="#aaaacc", anchor="mm")
    img.save(sys.argv[1])
except ImportError:
    # Pillow not available — skip background image (DMG still works fine)
    pass
PYEOF

# Remove any previous DMG
rm -f "$DMG_TEMP" "$DMG_FINAL"

# Create a temporary read-write DMG
hdiutil create \
    -volname "$APP_NAME" \
    -srcfolder "$DMG_STAGING" \
    -ov \
    -format UDRW \
    "$DMG_TEMP" \
    >/dev/null 2>&1

# Mount it to set window properties
MOUNT_POINT="/Volumes/${APP_NAME}"
# Detach if already mounted
hdiutil detach "$MOUNT_POINT" >/dev/null 2>&1 || true
hdiutil attach "$DMG_TEMP" -mountpoint "$MOUNT_POINT" >/dev/null 2>&1

# Use AppleScript to set Finder window appearance
osascript << APPLESCRIPT
tell application "Finder"
    tell disk "$APP_NAME"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set bounds of container window to {100, 100, 760, 520}
        set viewOptions to the icon view options of container window
        set arrangement of viewOptions to not arranged
        set icon size of viewOptions to 100
        try
            set background picture of viewOptions to file ".background:background.png"
        end try
        set position of item "${APP_NAME}.app" of container window to {170, 200}
        set position of item "Applications" of container window to {490, 200}
        close
        open
        update without registering applications
        delay 1
        close
    end tell
end tell
APPLESCRIPT

# Unmount
sync
hdiutil detach "$MOUNT_POINT" >/dev/null 2>&1

# Convert to compressed read-only DMG
hdiutil convert "$DMG_TEMP" \
    -format UDZO \
    -imagekey zlib-level=9 \
    -o "$DMG_FINAL" \
    >/dev/null 2>&1

# Clean up temp DMG
rm -f "$DMG_TEMP"

DMG_SIZE=$(du -h "$DMG_FINAL" | cut -f1)
echo "  ✓ Created ${DMG_FINAL} (${DMG_SIZE})"

# ── Done ──────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  ✓ Build complete!                                          "
echo "║                                                              "
echo "║  App:  ${DIST_DIR}/${APP_NAME}.app                          "
echo "║  DMG:  ${DMG_FINAL}                                         "
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# Open the DMG so the user can see it
open "$DMG_FINAL"
