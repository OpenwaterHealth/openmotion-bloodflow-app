# scripts/build_update_test_bundles.ps1
# Build the two Research bundles for the in-app-update end-to-end test:
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

function Build-ResearchBundle([string]$ver, [string]$apiUrl) {
    (Get-Content version.py) -replace '^_FALLBACK_VERSION = .*', "_FALLBACK_VERSION = `"$ver`"" |
        Set-Content version.py -Encoding UTF8
    . (Join-Path $PSScriptRoot "build_common.ps1")
    # Research variant + optional updateApiUrl, both compiled in (#546): stamp
    # config/app_config.py BEFORE PyInstaller, restore it afterwards. There is
    # no bundled JSON to edit after the build any more.
    $origModule = Set-BuildVariant -Clinical $false
    try {
        if ($apiUrl) {
            [void](Set-CompiledConfigValue -Key "updateApiUrl" -PythonValue "'$apiUrl'")
        }
        & conda run -n $CondaEnv python -m PyInstaller -y openwater.spec --distpath dist\research --workpath build\research
        if (-not (Test-Path "dist\research\Open-Motion\Open-Motion.exe")) { throw "dist missing after PyInstaller" }
        & powershell -NoProfile -ExecutionPolicy Bypass -File installer\build_installer.ps1 -Variant research -DistDir "dist\research\Open-Motion" -Version $ver
        if ($LASTEXITCODE -ne 0) { throw "build_installer failed for $ver" }
    } finally {
        Restore-ConfigModule -Text $origModule
        git checkout -- version.py
    }
}

Write-Host "=== OLD 1.3.0  (update check -> $ServerUrl) ===" -ForegroundColor Green
Build-ResearchBundle "1.3.0" $ServerUrl
Write-Host "=== NEW 1.3.1  (upgrade target) ===" -ForegroundColor Green
Build-ResearchBundle "1.3.1" ""

Write-Host "=== Artifacts (build\installer) ===" -ForegroundColor Green
Get-ChildItem "build\installer\Open-Motion-Research-Setup-1.3.*.exe" |
    Select-Object Name, @{N='MB';E={[math]::Round($_.Length/1MB,1)}} | Format-Table -AutoSize
