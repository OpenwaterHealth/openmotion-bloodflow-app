# installer/build_installer.ps1 - build the app MSI + Burn bundle for one variant.
param(
    [ValidateSet("clinical", "research")][string]$Variant = "clinical",
    [string]$DistDir = "",       # default: dist\<variant>\Open-Motion (per-variant PyInstaller output, #546)
    [string]$Version = "",      # override the numeric X.Y.Z (else derived from git)
    [string]$FullVersion = ""   # full semver for the bundle filename, e.g. 1.4.0-dev.2 (else $Version)
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
Set-Location $root
if (-not $DistDir) { $DistDir = "dist\$Variant\Open-Motion" }

# -- constant, never-changing GUIDs (distinct per variant so they never
#    cross-upgrade). Generated once with [guid]::NewGuid(). --
$guids = @{
    clinical = @{
        ProductName       = "Open-Motion"
        UpgradeCode       = "3d5dec27-6f62-484b-85f0-1b7b07076022"
        BundleUpgradeCode = "2e60deaa-8959-4f57-912b-71f60fc6ad5a"
        FileBase          = "Open-Motion"
    }
    research = @{
        ProductName       = "Open-Motion Research"
        UpgradeCode       = "81f5e9b0-36d8-4aeb-8457-461fe4fe6c2f"
        BundleUpgradeCode = "e363d244-2161-4a28-a855-835643b14a10"
        FileBase          = "Open-Motion-Research"
    }
}
$g = $guids[$Variant]

# -- numeric X.Y.Z: explicit -Version override, else from the git tag/describe --
if ($Version) {
    $version = $Version
} else {
    try {
        $desc = (git describe --tags --always 2>$null).Trim()
    } catch { $desc = "" }
    if ($desc -match '(\d+)\.(\d+)\.(\d+)') {
        $version = "$($matches[1]).$($matches[2]).$($matches[3])"
    } else {
        $version = "0.0.0"
    }
}
# -- full semver for the bundle filename (MSI/Burn internals stay numeric) --
if (-not $FullVersion) { $FullVersion = $version }
Write-Host "Variant=$Variant  Version=$version  Bundle=$FullVersion  Product='$($g.ProductName)'" -ForegroundColor Green

# -- extract the driver MSI from the bundled zip --
$drvZip = "resources\OpenMotionDriver-x64.zip"
$drvDir = "build\driver"
Remove-Item -Recurse -Force $drvDir -ErrorAction SilentlyContinue
Expand-Archive -Path $drvZip -DestinationPath $drvDir -Force
$driverMsi = Join-Path $drvDir "OpenMotionDriver-x64.msi"
if (-not (Test-Path $driverMsi)) { throw "driver MSI not found in $drvZip" }

# -- output names --
$outDir = "build\installer"
New-Item -ItemType Directory -Force $outDir | Out-Null
$appMsi    = Join-Path $outDir "$($g.FileBase).msi"
$bundleExe = Join-Path $outDir "$($g.FileBase)-Setup-$FullVersion.exe"

# -- build the app MSI --
# Resolve the PyInstaller output to an ABSOLUTE path. WiX resolves the
# <Files Include> harvest glob relative to the .wxs file's own directory
# (installer\), NOT the cwd, so a relative SourceDir silently harvests
# nothing (WIX8601 is only a warning, so the build would otherwise "succeed"
# with an app-less MSI). Fail hard if the build output is not there.
if (-not (Test-Path $DistDir)) { throw "PyInstaller output '$DistDir' not found; run PyInstaller first" }
$DistAbs = (Resolve-Path -LiteralPath $DistDir).Path
if (-not (Test-Path (Join-Path $DistAbs 'Open-Motion.exe'))) {
    throw "Open-Motion.exe not found under $DistAbs; PyInstaller build looks incomplete"
}

# -- variant / layout sanity (#546, #547) --
# The MSI harvests the per-variant PyInstaller output as-is. Since #547 that
# is the single onefile Open-Motion.exe: the Clinical/Research split is
# compiled into it (CLINICAL_MODE stamped before PyInstaller ran), and
# portableMode is derived at launch from the HKLM InstallDir marker app.wxs
# writes below — an installed exe keeps its writable state per user under
# %LOCALAPPDATA%\Openwater (#581), the byte-identical exe in the portable zip keeps it next to
# itself. A leftover onedir tree (_internal\) from an older build would be
# harvested next to the exe and ship loose, unsigned files again, so refuse
# it; likewise an obviously mismatched dist path.
if ((Test-Path (Join-Path $DistAbs "_internal"))) {
    throw "dist at $DistAbs carries a pre-#547 onedir _internal\ tree; delete the dist directory and rebuild"
}
$distFiles = @(Get-ChildItem -LiteralPath $DistAbs -File)
if ($distFiles.Count -ne 1 -or $distFiles[0].Name -ne 'Open-Motion.exe') {
    throw "dist at $DistAbs must contain exactly Open-Motion.exe (onefile, #547); found: $($distFiles.Name -join ', ')"
}
if ($DistAbs -notmatch "[\\/]$Variant[\\/]Open-Motion$") {
    Write-Host "WARNING: DistDir '$DistAbs' does not look like the '$Variant' variant's build output (expected ...\$Variant\Open-Motion)" -ForegroundColor Yellow
}

wix build installer\app.wxs -o $appMsi `
    -d "ProductName=$($g.ProductName)" `
    -d "Version=$version" `
    -d "UpgradeCode=$($g.UpgradeCode)" `
    -d "SourceDir=$DistAbs"
if ($LASTEXITCODE -ne 0) { throw "app MSI build failed" }
# Guard against the silent empty-harvest: a real app MSI is far larger than this.
if ((Get-Item $appMsi).Length -lt 1MB) {
    throw "app MSI is only $((Get-Item $appMsi).Length) bytes; file harvesting from $DistAbs produced an empty package"
}

# The app MSI is deliberately NOT Authenticode-signed (#569). eSigner signings
# are metered, and this was the one signature nothing in the shipped flow
# checks: the MSI is never a release asset (only the Setup bundle is), Burn
# runs it from its already-elevated engine so it raises no UAC prompt of its
# own, and Burn verifies it by the hash in the bundle manifest, which sits
# under the engine signature below. Accepted trade-off: a managed PC whose
# AppLocker / WDAC policy allows Windows Installer packages by publisher would
# block it; if a site needs that, re-add
#   powershell -NoProfile -File installer\sign.ps1 -Files $appMsi
# here, before the bundle is built (the bundle records the MSI's hash).

# -- build the Burn bundle --
# -bindpath installer so the custom BA ThemeFile/LocalizationFile payloads
# (bundle-theme.xml/.wxl) resolve; WiX searches bind paths (default cwd=repo
# root), NOT the .wxs directory, for payload source files.
#
# IconFile/LogoFile are passed as ABSOLUTE paths for the same reason SourceDir
# is above: bind-path resolution is not relative to the .wxs directory, and a
# relative path that silently resolves nowhere is the failure mode this script
# has been bitten by before. Both assets are committed; regenerate them with
# scripts/make_app_icon.py. (Issue #402.)
$iconFile = Join-Path $root "assets\images\favicon.ico"
$logoFile = Join-Path $root "assets\images\installer-logo.png"
foreach ($asset in @($iconFile, $logoFile)) {
    if (-not (Test-Path $asset)) { throw "installer branding asset not found: $asset" }
}

wix build installer\bundle.wxs -o $bundleExe -ext WixToolset.BootstrapperApplications.wixext `
    -bindpath installer `
    -d "ProductName=$($g.ProductName)" `
    -d "Version=$version" `
    -d "BundleUpgradeCode=$($g.BundleUpgradeCode)" `
    -d "DriverMsi=$driverMsi" `
    -d "AppMsi=$appMsi" `
    -d "IconFile=$iconFile" `
    -d "LogoFile=$logoFile"
if ($LASTEXITCODE -ne 0) { throw "bundle build failed" }

# -- sign the bundle (insignia detach -> sign engine -> reattach -> sign) --
if ($env:CODESIGN_THUMBPRINT) {
    $engine = Join-Path $outDir "engine.exe"
    wix burn detach $bundleExe -engine $engine
    powershell -NoProfile -File installer\sign.ps1 -Files $engine
    wix burn reattach $bundleExe -engine $engine -o $bundleExe
    powershell -NoProfile -File installer\sign.ps1 -Files $bundleExe
} else {
    Write-Host "bundle signing skipped (no cert)" -ForegroundColor Yellow
}

Write-Host "Built $bundleExe" -ForegroundColor Green
