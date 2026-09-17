# scripts/build_nuitka.ps1 — compile one variant natively with Nuitka (#548).
#
# Produces the same output contract as the PyInstaller path
# (Invoke-VariantBuild in build_common.ps1): a single onefile exe at
#     dist\<variant>\Open-Motion\Open-Motion.exe
# so scripts\package_artifacts.ps1 zips and installs it unchanged.
#
#   powershell -File scripts\build_nuitka.ps1 -Variant research
#   powershell -File scripts\build_nuitka.ps1 -Variant clinical -Version 1.6.0
#
# Why Nuitka: the PyInstaller exe carries the app as bytecode in an archive
# that any .pyc decompiler reads (tracker M-09 / M-14); Nuitka compiles every
# module to C. Onefile still extracts to a per-process temp directory at
# launch exactly like PyInstaller does (see the Packaging section in
# CLAUDE.md); never pass --onefile-tempdir-spec with a static location.
#
# Requirements in the build Python: nuitka, ordered-set, zstandard, and a C
# compiler — `pip install ziglang` is enough (Nuitka 4.x uses zig's clang);
# on the GitHub runner MSVC is picked up automatically.
param(
    [Parameter(Mandatory)][ValidateSet("clinical", "research")][string]$Variant,
    [string]$Version    = "",
    [string]$DistRoot   = "dist",
    [string]$WorkRoot   = "build",
    [string]$CondaEnv   = "ow-motion",
    [int]$Jobs          = 0,
    [switch]$KeepStandalone   # leave build\<variant>-nuitka\main.dist for inspection
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "build_common.ps1")
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $Version) { $Version = (Get-BuildVersion).Full }
$numeric = Get-NumericVersion -Version $Version
if ($numeric -notmatch '^\d+(\.\d+){0,3}$') { $numeric = "0.0.0" }

$distPath = Join-Path (Join-Path $DistRoot $Variant) "Open-Motion"
$workPath = Join-Path $WorkRoot "$Variant-nuitka"
if (Test-Path $distPath) { Remove-Item -Recurse -Force $distPath }
New-Item -ItemType Directory -Force $distPath | Out-Null
if (-not $KeepStandalone -and (Test-Path $workPath)) { Remove-Item -Recurse -Force $workPath }

# The SDK's package directory: an editable install is exposed through a
# PEP 660 finder Nuitka does not consult (same class of problem as #557 for
# PyInstaller), so its parent goes on PYTHONPATH for the compile; a wheel
# install resolves to site-packages and this is a no-op.
$sdkDir = (Invoke-AppPython -CondaEnv $CondaEnv -Arguments @(
    "-c", "import importlib.util as u; print(list(u.find_spec('omotion').submodule_search_locations)[0])"
)) | Select-Object -Last 1
if (-not $sdkDir -or -not (Test-Path $sdkDir)) { throw "omotion is not importable in the build environment" }
$env:PYTHONPATH = Split-Path -Parent $sdkDir
$env:PYTHONIOENCODING = "utf-8"

$args = @(
    "-m", "nuitka",
    "--standalone", "--onefile", "--deployment",
    "--output-dir=$workPath", "--output-filename=Open-Motion.exe",
    # Qt plugins: the "sensible" set plus qml (the QtQuick / Controls module
    # tree and the qml plugin dir). Without an explicit qml the plugin only
    # warns that the bundled QML "is unlikely to work"; "all" doubled the size.
    "--enable-plugin=pyqt6", "--include-qt-plugins=sensible,qml",
    "--windows-console-mode=disable",
    "--windows-icon-from-ico=assets\images\favicon.ico",
    "--company-name=Openwater", "--product-name=Open-Motion",
    "--file-version=$numeric", "--product-version=$numeric",
    "--file-description=Open-Motion blood flow monitor ($Variant)",
    # app resources (same set openwater.spec bundles)
    "--include-data-files=main.qml=main.qml",
    "--include-data-dir=pages=pages",
    "--include-data-dir=components=components",
    "--include-data-dir=assets=assets",
    "--include-data-files=resources\sample_scan.csv=resources\sample_scan.csv",
    # the SDK: modules compiled, package data copied. Its vendored binaries
    # (libusb DLLs, dfu-util exes) are NOT data to Nuitka - --include-data-dir
    # silently drops .dll/.exe - so those trees go in raw: only the Windows
    # dfu-util directories (the SDK picks win64/win32 by architecture; the
    # Linux/macOS binaries and the dfuse-pack.py helper are dead weight on
    # Windows and a raw copy would ship that .py as plaintext). The last
    # entry mirrors libusb to the bundle root where the libusb1 wheel and
    # utils/libusb_paths.py look for it (same as openwater.spec).
    "--include-package=omotion", "--include-package-data=omotion",
    "--include-raw-dir=$sdkDir\dfu-util\win64=omotion\dfu-util\win64",
    "--include-raw-dir=$sdkDir\dfu-util\win32=omotion\dfu-util\win32",
    "--include-raw-dir=$sdkDir\_vendor=omotion\_vendor",
    "--include-raw-dir=$sdkDir\_vendor\libusb\windows\x64=_vendor\libusb\windows\x64",
    # imports PyInstaller needed as hidden imports (entry points / lazy)
    "--include-package=keyring.backends", "--include-package=win32ctypes",
    "--include-package=sqlcipher3",
    "--include-module=usb.backend.libusb1", "--include-package=serial",
    "--report=$workPath\nuitka-report.xml",
    "--assume-yes-for-downloads",
    "main.py"
)
if ($Jobs -gt 0) { $args += "--jobs=$Jobs" }
# A clinical build must not carry the self-updater (#543, tracker M-02).
# motion_connector imports app_updater only when the compiled CLINICAL_MODE
# is False, but Nuitka follows the import statically regardless of the
# branch, so tell it not to; openwater.spec does the same with excludes=.
if ($Variant -eq "clinical") { $args += "--nofollow-import-to=app_updater" }

$orig = Set-BuildVariant -Clinical ($Variant -eq "clinical")
try {
    Write-Host "=== Nuitka ($Variant, $Version) -> $distPath ===" -ForegroundColor Cyan
    Invoke-AppPython -CondaEnv $CondaEnv -Arguments $args
    if ($LASTEXITCODE -ne 0) { throw "Nuitka failed for variant '$Variant'" }
} finally {
    Restore-ConfigModule -Text $orig
}

$built = Join-Path $workPath "Open-Motion.exe"
if (-not (Test-Path $built)) { throw "Nuitka output missing: $built" }
Move-Item -LiteralPath $built -Destination (Join-Path $distPath "Open-Motion.exe") -Force
if (-not $KeepStandalone) {
    foreach ($d in @("main.dist", "main.onefile-build", "main.build")) {
        $p = Join-Path $workPath $d
        if (Test-Path $p) { Remove-Item -Recurse -Force $p }
    }
}
# Qt window-class icon (#223): Nuitka, like PyInstaller, publishes the icon
# group under integer id 1, which Qt's LoadImage(L"IDI_ICON1") never finds.
# Add the named group now. Safe on a finished Nuitka onefile: its payload is
# a PE resource, so UpdateResource rewrites the image consistently (verified
# on a probe build); PyInstaller's appended overlay would not survive this.
$target = Join-Path $distPath "Open-Motion.exe"
$iconCode = @(
    "import sys; sys.path.insert(0, r'$(Join-Path $root 'scripts')')",
    "from win_icon_resource import add_named_group_icon, has_named_group_icon",
    "add_named_group_icon(r'$target', r'$(Join-Path $root 'assets\images\favicon.ico')')",
    "ok = has_named_group_icon(r'$target')",
    "print('[nuitka] IDI_ICON1 window-class icon:', ok)",
    "sys.exit(0 if ok else 1)"
) -join "; "
Invoke-AppPython -CondaEnv $CondaEnv -Arguments @("-c", $iconCode)
if ($LASTEXITCODE -ne 0) { throw "named icon group missing on $target (#223)" }

$exe = Get-Item $target
Write-Host ("[nuitka] built {0} ({1:N1} MB)" -f $exe.FullName, ($exe.Length / 1MB)) -ForegroundColor Green
