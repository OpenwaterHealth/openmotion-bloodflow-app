# installer/build_installer.ps1 - build the app MSI + Burn bundle for one variant.
param(
    [ValidateSet("clinical", "research")][string]$Variant = "clinical",
    [string]$DistDir = "dist\Open-Motion",
    [string]$Version = "",      # override the numeric X.Y.Z (else derived from git)
    [string]$FullVersion = ""   # full semver for the bundle filename, e.g. 1.4.0-dev.2 (else $Version)
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
Set-Location $root

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

# -- stage the per-variant UI mode into the harvested config --
# The MSI harvests dist\Open-Motion as-is, so clinical/Research must be
# written into the bundled config BEFORE wix runs. Mirrors the per-variant
# flip in scripts/package_artifacts.ps1: clinical keeps clinicalMode=true
# (reduced clinical UI), Research sets it false (full research UI). We set it
# explicitly per variant
# so repeated builds against one dist are always correct regardless of prior
# state — including when scripts/package_artifacts.ps1 already staged the
# same value before invoking us (the re-apply is an idempotent no-op, not a
# bug). This self-contained flip is also what lets build_installer.ps1 be run
# standalone.
# Set-ClinicalMode writes UTF-8 *without* BOM — the app's json.load fails
# silently on a BOM and falls back to defaults.
$cfgPath = Join-Path $DistAbs "_internal\config\app_config.json"
. (Join-Path $root "scripts\build_common.ps1")
$clinical = ($Variant -ne "research")   # clinical => clinicalMode true; research => false
[void](Set-ClinicalMode -ConfigPath $cfgPath -Clinical $clinical)
Write-Host "Staged clinicalMode=$clinical into bundled config" -ForegroundColor Green

# Installed (MSI) apps always scatter writable state to %PROGRAMDATA% —
# portableMode is a portable-zip-only concept. Force it off here too so this
# script stays correct when run standalone, not just via package_artifacts.ps1.
[void](Set-PortableMode -ConfigPath $cfgPath -Portable $false)
Write-Host "Staged portableMode=false into bundled config" -ForegroundColor Green

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

# sign the app MSI (skippable)
powershell -NoProfile -File installer\sign.ps1 -Files $appMsi

# -- build the Burn bundle --
# -bindpath installer so the custom BA ThemeFile/LocalizationFile payloads
# (bundle-theme.xml/.wxl) resolve; WiX searches bind paths (default cwd=repo
# root), NOT the .wxs directory, for payload source files.
wix build installer\bundle.wxs -o $bundleExe -ext WixToolset.BootstrapperApplications.wixext `
    -bindpath installer `
    -d "ProductName=$($g.ProductName)" `
    -d "Version=$version" `
    -d "BundleUpgradeCode=$($g.BundleUpgradeCode)" `
    -d "DriverMsi=$driverMsi" `
    -d "AppMsi=$appMsi"
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
