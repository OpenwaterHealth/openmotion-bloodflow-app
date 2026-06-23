# scripts/build_update_test_bundles.ps1
# Build the two RUO bundles for the in-app-update end-to-end test:
#   OLD 1.3.0 -> the build under test; its update check points at -ServerUrl
#   NEW 1.3.1 -> the upgrade target the fake-release server hands out
#
# Prereqs (see docs / memory project_local_build_test_env):
#   * conda env with PyQt6 + omotion (clean SDK!) + PyInstaller  (default: pylib)
#   * WiX 5.0.2 + .NET 8 (the script wires DOTNET_ROOT if installed user-scope)
#   * a CLEAN SDK: this bundles whatever `omotion` resolves to — the script
#     prints which SDK it built against; make sure it's a clean next.
#
# Usage:
#   powershell -File scripts\build_update_test_bundles.ps1
#   powershell -File scripts\build_update_test_bundles.ps1 -ServerUrl http://127.0.0.1:8077/releases/latest
param(
    [string]$ServerUrl = "http://127.0.0.1:8077/releases/latest",
    [string]$CondaEnv  = "pylib"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
Set-Location $root

# WiX/.NET toolchain (user-scope install location)
$dotnetDir = "$env:LOCALAPPDATA\Microsoft\dotnet"
if (Test-Path $dotnetDir) {
    $env:DOTNET_ROOT = $dotnetDir
    $env:Path = "$dotnetDir;$env:USERPROFILE\.dotnet\tools;$env:Path"
}

$sdk = (& conda run -n $CondaEnv python -c "import omotion; print(omotion.__file__)").Trim()
Write-Host "Building against SDK: $sdk" -ForegroundColor Cyan

function Build-RuoBundle([string]$ver, [string]$apiUrl) {
    (Get-Content version.py) -replace '^_FALLBACK_VERSION = .*', "_FALLBACK_VERSION = `"$ver`"" |
        Set-Content version.py -Encoding UTF8
    try {
        & conda run -n $CondaEnv python -m PyInstaller -y openwater.spec
        if (-not (Test-Path "dist\OpenWaterApp\OpenWaterApp.exe")) { throw "dist missing after PyInstaller" }
        # RUO (reducedMode:false) + optional updateApiUrl injection, via JSON (no BOM)
        $py = "import json; p='dist/OpenWaterApp/_internal/config/app_config.json'; " +
              "d=json.load(open(p,encoding='utf-8')); d['reducedMode']=False; "
        if ($apiUrl) { $py += "d['updateApiUrl']='$apiUrl'; " }
        $py += "json.dump(d, open(p,'w',encoding='utf-8'), indent=2)"
        & conda run -n $CondaEnv python -c $py
        & powershell -NoProfile -ExecutionPolicy Bypass -File installer\build_installer.ps1 -Variant ruo -Version $ver
        if ($LASTEXITCODE -ne 0) { throw "build_installer failed for $ver" }
    } finally {
        git checkout -- version.py
    }
}

Write-Host "=== OLD 1.3.0  (update check -> $ServerUrl) ===" -ForegroundColor Green
Build-RuoBundle "1.3.0" $ServerUrl
Write-Host "=== NEW 1.3.1  (upgrade target) ===" -ForegroundColor Green
Build-RuoBundle "1.3.1" ""

Write-Host "=== Artifacts (build\installer) ===" -ForegroundColor Green
Get-ChildItem "build\installer\Openwater-Setup-1.3.*_RUO.exe" |
    Select-Object Name, @{N='MB';E={[math]::Round($_.Length/1MB,1)}} | Format-Table -AutoSize
