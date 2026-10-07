# scripts/build_nuitka.ps1 — compile one variant natively with Nuitka (#548).
#
# Produces the same output contract as the PyInstaller path
# (Invoke-VariantBuild in build_common.ps1): a single onefile exe at
#     dist\<variant>\Open-Motion\Open-Motion.exe
# so scripts\package_artifacts.ps1 zips and installs it unchanged.
#
#   powershell -File scripts\build_nuitka.ps1 -Variant research
#   powershell -File scripts\build_nuitka.ps1 -Variant clinical -Version 1.6.0
#   powershell -File scripts\build_nuitka.ps1 -Variant clinical -Service
#
# -Service (clinical only, #706) builds the clinical service tool: a clinical
# build that keeps the engineering-mode unlock, into
# dist\clinical-service\Open-Motion\Open-Motion.exe. package_artifacts.ps1
# never picks it up. In CI it is built only by the windows-build action's
# service-tool input, which keeps it unsigned and zips it (no installer).
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
    [switch]$Service,         # clinical service tool (#706); clinical only
    [switch]$KeepStandalone   # leave build\<variant>-nuitka\main.dist for inspection
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "build_common.ps1")
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if ($Service -and $Variant -ne "clinical") {
    throw "-Service is a clinical build option; a Research build always has the engineering unlock"
}
if (-not $Version) { $Version = (Get-BuildVersion).Full }
$numeric = Get-NumericVersion -Version $Version
if ($numeric -notmatch '^\d+(\.\d+){0,3}$') { $numeric = "0.0.0" }

$outName = if ($Service) { "$Variant-service" } else { $Variant }
$distPath = Join-Path (Join-Path $DistRoot $outName) "Open-Motion"
$workPath = Join-Path $WorkRoot "$outName-nuitka"
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
    # The qt-trim user plugin then drops every QML module and Qt plugin the
    # app does not use, and the Qt libraries only those link (#680).
    "--enable-plugin=pyqt6", "--include-qt-plugins=sensible,qml",
    "--user-plugin=scripts\nuitka_qt_trim.py",
    # #579: sign the app's main.dll inside the payload before onefile packing,
    # so what the bootstrap extracts to %TEMP% at launch is signed too. A no-op
    # without CODESIGN_THUMBPRINT (local builds, dev tags, branch pushes).
    "--user-plugin=scripts\nuitka_sign_payload.py",
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
    # Package data would also carry the Linux and macOS dfu-util builds
    # (extensionless binaries are data to Nuitka) and the libusb static and
    # import libraries (.a, .dll.a, .la) of every platform (#680). None of
    # that runs on Windows. The patterns match destination paths, so they
    # cover the raw win64/win32 copies below too. The dfu-util license and
    # README files stay.
    "--noinclude-data-files=omotion/dfu-util/linux-amd64/*",
    "--noinclude-data-files=omotion/dfu-util/darwin-x86_64/*",
    "--noinclude-data-files=omotion/dfu-util/*.a",
    "--noinclude-data-files=omotion/dfu-util/*.la",
    "--include-raw-dir=$sdkDir\dfu-util\win64=omotion\dfu-util\win64",
    "--include-raw-dir=$sdkDir\dfu-util\win32=omotion\dfu-util\win32",
    "--include-raw-dir=$sdkDir\_vendor=omotion\_vendor",
    "--include-raw-dir=$sdkDir\_vendor\libusb\windows\x64=_vendor\libusb\windows\x64",
    # #669: Nuitka's usb1 config pulls in the libusb1 wheel's own DLL (1.0.28
    # in libusb1 3.3.1). Only the SDK's Linux/macOS hotplug provider imports
    # usb1. Should anything load it here, its loader falls back to the bare
    # libusb-1.0.dll name, which resolves to the SDK copy above. Every libusb
    # that does ship is checked after the compile (scripts\check_libusb.py).
    # Nuitka-Onefile then warns that usb1\libusb-1.0.dll is missing from the
    # distribution folder: that is this exclusion, and expected.
    "--noinclude-dlls=usb1/libusb*",
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
# Nor the engineering-mode unlock (#706), unless this is the service tool:
# the Python module (imported on the same compiled constants) and its QML
# prompt, so a clinical bundle carries neither the check nor the dialog.
if ($Variant -eq "clinical" -and -not $Service) {
    $args += "--nofollow-import-to=engineering_unlock"
    $args += "--noinclude-data-files=components/EngineeringUnlockModal.qml"
}

# Data blobs (the program's constants, the onefile payload) go in as COFF
# objects that Nuitka writes itself (#698). That is MSVC's default, so CI is
# unchanged. With zig, Nuitka's default is C23 #embed. The generated source
# names the blob relatively, so it is the same in every build directory, but
# zig's compile cache records the embedded file by absolute path. A build
# therefore got another build directory's compiled blob back whenever that
# build's blob was still on disk (a -KeepStandalone tree, a build running in
# another worktree), and the exe shipped the other build's constants and
# payload. scripts\check_nuitka_blobs.py below verifies the result.
$prevResourceMode = $env:NUITKA_RESOURCE_MODE
$env:NUITKA_RESOURCE_MODE = "coff_obj"
$orig = Set-BuildVariant -Clinical ($Variant -eq "clinical") -Service $Service.IsPresent
try {
    Write-Host "=== Nuitka ($outName, $Version) -> $distPath ===" -ForegroundColor Cyan
    Invoke-AppPython -CondaEnv $CondaEnv -Arguments $args
    if ($LASTEXITCODE -ne 0) { throw "Nuitka failed for variant '$Variant'" }
} finally {
    Restore-ConfigModule -Text $orig
    $env:NUITKA_RESOURCE_MODE = $prevResourceMode
}

$built = Join-Path $workPath "Open-Motion.exe"
if (-not (Test-Path $built)) { throw "Nuitka output missing: $built" }
# #669: main.dist is exactly what the onefile packed, so check its libusb
# copies before the cleanup below removes it. An SDK pin with older DLLs
# fails the build here.
Invoke-AppPython -CondaEnv $CondaEnv -Arguments @("scripts\check_libusb.py", (Join-Path $workPath "main.dist"))
if ($LASTEXITCODE -ne 0) { throw "libusb older than 1.0.30 in the '$Variant' payload (#669)" }
Move-Item -LiteralPath $built -Destination (Join-Path $distPath "Open-Motion.exe") -Force
# Qt window-class icon (#223): Nuitka, like PyInstaller, publishes the icon
# group under integer id 1, which Qt's LoadImage(L"IDI_ICON1") never finds.
# Add the named group now. Safe on a finished Nuitka onefile: its payload is
# linked into the image rather than appended after it, so UpdateResource
# rewrites the image consistently (verified on a probe build, and the blob
# check below re-reads the finished exe); PyInstaller's appended overlay would
# not survive this.
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

# #698: the finished exe must hold this build's onefile payload, and main.dll
# this build's constants. Checked on the final file, before the cleanup
# below removes the blobs it compares against.
Invoke-AppPython -CondaEnv $CondaEnv -Arguments @("scripts\check_nuitka_blobs.py", $workPath, $target)
if ($LASTEXITCODE -ne 0) { throw "the '$Variant' exe carries another build's data (#698)" }
if (-not $KeepStandalone) {
    foreach ($d in @("main.dist", "main.onefile-build", "main.build")) {
        $p = Join-Path $workPath $d
        if (Test-Path $p) { Remove-Item -Recurse -Force $p }
    }
}

$exe = Get-Item $target
Write-Host ("[nuitka] built {0} ({1:N1} MB)" -f $exe.FullName, ($exe.Length / 1MB)) -ForegroundColor Green
