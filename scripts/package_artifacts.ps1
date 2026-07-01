# scripts/package_artifacts.ps1
# Produce the 4 release artifacts (Clinical/Research x Portable/Installer) from a
# single already-built PyInstaller dist. PyInstaller is NOT run here — the caller
# (build_and_zip.ps1 or CI) runs it once; this packages it.
#
#   powershell -File scripts\package_artifacts.ps1                 # all 4 (needs WiX)
#   powershell -File scripts\package_artifacts.ps1 -SkipInstaller  # 2 portable zips
#   powershell -File scripts\package_artifacts.ps1 -Version 1.4.0-dev.0
param(
    [string]$DistDir    = "dist\Open-Motion",
    [string]$Version    = "",
    [string[]]$Variants = @("clinical", "research"),
    [switch]$SkipInstaller,
    [string]$OutDir     = ""
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

# -- validate the dist --
if (-not (Test-Path $DistDir)) { throw "PyInstaller output '$DistDir' not found; run PyInstaller first" }
if (-not (Test-Path (Join-Path $DistDir 'Open-Motion.exe'))) {
    throw "Open-Motion.exe not found under $DistDir; PyInstaller build looks incomplete"
}
$cfgPath = Join-Path $DistDir "_internal\config\app_config.json"

# -- WiX gate: skip installers (zips still build) when the toolchain is absent --
$buildInstallers = -not $SkipInstaller
if ($buildInstallers -and -not (Test-WixAvailable)) {
    Write-Host "installers skipped — WiX not found (install WiX 5.0.2 or pass -SkipInstaller to silence)" -ForegroundColor Yellow
    $buildInstallers = $false
}

# -- per-variant suffix + clinicalMode value --
$variantMap = @{
    clinical = @{ Suffix = "";          Clinical = $true  }
    research = @{ Suffix = "_Research"; Clinical = $false }
}

$produced = @()
foreach ($variant in $Variants) {
    if (-not $variantMap.ContainsKey($variant)) { throw "unknown variant '$variant' (expected clinical|research)" }
    $m = $variantMap[$variant]

    $orig = Set-ClinicalMode -ConfigPath $cfgPath -Clinical $m.Clinical
    try {
        # portable zip (full version) — portableMode:true so it keeps all
        # writable state next to the exe, matching the old un-installed layout.
        # build_installer.ps1 below independently forces portableMode:false
        # onto the same file before harvesting it for the MSI.
        [void](Set-PortableMode -ConfigPath $cfgPath -Portable $true)
        $zip = Join-Path $OutDir "Open-Motion-$verFull$($m.Suffix).zip"
        Write-Host "=== Portable ($variant): $zip ===" -ForegroundColor Cyan
        New-PortableZip -DistDir $DistDir -OutZip $zip
        $produced += $zip

        # installer (numeric version)
        if ($buildInstallers) {
            Write-Host "=== Installer ($variant) ===" -ForegroundColor Cyan
            & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "installer\build_installer.ps1") `
                -Variant $variant -DistDir $DistDir -Version $verNumeric
            if ($LASTEXITCODE -ne 0) { throw "build_installer failed for $variant" }
            $produced += (Join-Path $root "build\installer\Openwater-Setup-$verNumeric$($m.Suffix).exe")
        }
    } finally {
        Restore-ConfigText -ConfigPath $cfgPath -Text $orig
    }
}

# -- post-build assertions --
foreach ($a in $produced) {
    if (-not (Test-Path $a)) { throw "expected artifact missing: $a" }
    if ($a.EndsWith(".zip") -and (Get-Item $a).Length -lt 5MB) {
        throw "portable zip suspiciously small (<5MB): $a"
    }
}

Write-Host "=== Packaging complete — $($produced.Count) artifact(s) ===" -ForegroundColor Green
$produced | ForEach-Object { Write-Host "  $_" -ForegroundColor Green }
