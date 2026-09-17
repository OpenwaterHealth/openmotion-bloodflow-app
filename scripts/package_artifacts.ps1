# scripts/package_artifacts.ps1
# Build and package the release artifacts (Clinical/Research x Portable/Installer).
#
# Since #546 each variant is its own PyInstaller build (CLINICAL_MODE is a
# compile-time constant stamped into config/app_config.py before PyInstaller
# runs), so this script runs PyInstaller once per requested variant into
# dist\<variant>\Open-Motion and packages that. Portable zip and installer
# share the same exe: portableMode is derived at launch from the installer's
# HKLM marker, not stamped.
#
#   powershell -File scripts\package_artifacts.ps1                  # all 4 (needs WiX)
#   powershell -File scripts\package_artifacts.ps1 -SkipInstaller   # 2 portable zips
#   powershell -File scripts\package_artifacts.ps1 -Version 1.4.0-dev.0
#   powershell -File scripts\package_artifacts.ps1 -SkipBuild       # dist\<variant>\ already built (CI)
param(
    [string]$Version    = "",
    [string[]]$Variants = @("clinical", "research"),
    [switch]$SkipInstaller,
    [switch]$SkipBuild,
    [string]$DistRoot   = "dist",
    [string]$OutDir     = "",
    [string]$CondaEnv   = "ow-motion"
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "build_common.ps1")
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not $OutDir) { $OutDir = $root }

# -- resolve version (Full for zips, Numeric for the MSI) --
if ($Version) {
    $verFull    = $Version
    $verNumeric = Get-NumericVersion -Version $Version
} else {
    $v = Get-BuildVersion
    $verFull    = $v.Full
    $verNumeric = $v.Numeric
}
Write-Host "Packaging version: Full=$verFull Numeric=$verNumeric" -ForegroundColor Yellow

# -- WiX gate: skip installers (zips still build) when the toolchain is absent --
$buildInstallers = -not $SkipInstaller
if ($buildInstallers -and -not (Test-WixAvailable)) {
    Write-Host "installers skipped - WiX not found (install WiX 5.0.2 or pass -SkipInstaller to silence)" -ForegroundColor Yellow
    $buildInstallers = $false
}

# -- per-variant file base --
$variantMap = @{
    clinical = @{ FileBase = "Open-Motion" }
    research = @{ FileBase = "Open-Motion-Research" }
}

$produced = @()
foreach ($variant in $Variants) {
    if (-not $variantMap.ContainsKey($variant)) { throw "unknown variant '$variant' (expected clinical|research)" }
    $m = $variantMap[$variant]
    $distDir = Join-Path (Join-Path $DistRoot $variant) "Open-Motion"

    if (-not $SkipBuild) {
        [void](Invoke-VariantBuild -Variant $variant -DistRoot $DistRoot -CondaEnv $CondaEnv)
    }
    if (-not (Test-Path (Join-Path $distDir 'Open-Motion.exe'))) {
        throw "Open-Motion.exe not found under $distDir; build the '$variant' variant first (or drop -SkipBuild)"
    }

    # -- Authenticode-sign this variant's exe in the dist (no-op without a
    #    cert, #443). Since #547 the exe is the whole onefile payload, so this
    #    one signature covers every shipped byte of the portable zip and of
    #    what the MSI harvests. The MSI and Setup bundle are signed separately
    #    by installer/build_installer.ps1. --
    & powershell -NoProfile -File (Join-Path $root "installer\sign.ps1") `
        -Files (Join-Path $distDir "Open-Motion.exe")
    if ($LASTEXITCODE -ne 0) { throw "signing Open-Motion.exe failed for $variant" }

    # portable zip (full version)
    $zip = Join-Path $OutDir "$($m.FileBase)-$verFull.zip"
    Write-Host "=== Portable ($variant): $zip ===" -ForegroundColor Cyan
    New-PortableZip -DistDir $distDir -OutZip $zip
    $produced += $zip

    # installer (numeric version inside the MSI/Burn metadata, full
    # version in the bundle filename)
    if ($buildInstallers) {
        Write-Host "=== Installer ($variant) ===" -ForegroundColor Cyan
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "installer\build_installer.ps1") `
            -Variant $variant -DistDir $distDir -Version $verNumeric -FullVersion $verFull
        if ($LASTEXITCODE -ne 0) { throw "build_installer failed for $variant" }
        $produced += (Join-Path $root "build\installer\$($m.FileBase)-Setup-$verFull.exe")
    }
}

# -- post-build assertions --
foreach ($a in $produced) {
    if (-not (Test-Path $a)) { throw "expected artifact missing: $a" }
    if ($a.EndsWith(".zip") -and (Get-Item $a).Length -lt 5MB) {
        throw "portable zip suspiciously small (<5MB): $a"
    }
}

Write-Host "=== Packaging complete - $($produced.Count) artifact(s) ===" -ForegroundColor Green
$produced | ForEach-Object { Write-Host "  $_" -ForegroundColor Green }
