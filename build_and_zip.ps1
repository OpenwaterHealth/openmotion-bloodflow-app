# build_and_zip.ps1
param(
    [string]$SpecFile = "openwater.spec",
    [string]$AppName = "OpenWaterApp",
    [string]$Entry = "main.py",
    [string]$CondaEnv = "ow-motion",
    [switch]$OpenFolder
)

$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path -Parent $MyInvocation.MyCommand.Definition)

# Resolve Python from current session or from CONDA_PREFIX
$python = $null
try { $python = (Get-Command python -ErrorAction Stop).Source } catch {}
if (-not $python -and $env:CONDA_PREFIX) {
    $cand = Join-Path $env:CONDA_PREFIX "python.exe"
    if (Test-Path $cand) { $python = $cand }
}

if (-not $python) {
    Write-Host "Python not found in PATH. Trying conda run (-n $CondaEnv)..." -ForegroundColor Yellow
    & conda run -n $CondaEnv python -V | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "No Python found. Open Anaconda Prompt and 'conda activate $CondaEnv', or install Python."
    }
    function Invoke-Py { param([string[]]$pyArgs) & conda run -n $CondaEnv python $pyArgs }  # <-- FIXED
} else {
    function Invoke-Py { param([string[]]$pyArgs) & $python $pyArgs }                          # <-- FIXED
}

Write-Host "=== Cleaning old build artifacts ===" -ForegroundColor Cyan
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

Write-Host "=== Ensuring PyInstaller is installed ===" -ForegroundColor Cyan
Invoke-Py @("-m","pip","show","pyinstaller") | Out-Null
if ($LASTEXITCODE -ne 0) {
    Invoke-Py @("-m","pip","install","-U","pyinstaller")
}

# Generate a minimal spec if missing
if (-not (Test-Path $SpecFile)) {
    Write-Host "Spec file '$SpecFile' not found. Generating a basic one..." -ForegroundColor Yellow
    Invoke-Py @("-m","PyInstaller","--name",$AppName,"--noconsole",$Entry)
    $genSpec = "$AppName.spec"
    if ((Test-Path $genSpec) -and ($genSpec -ne $SpecFile)) {
        Move-Item -Force $genSpec $SpecFile
    }
    Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
}

Write-Host "=== Building with PyInstaller ===" -ForegroundColor Cyan

# Determine version from git tags
try {
    $GitVersion = (git describe --tags --dirty --always --long 2>$null).Trim()
    if (-not $GitVersion) { throw "empty" }
} catch {
    $GitVersion = "dev-$(Get-Date -Format 'yyyyMMdd_HHmmss')"
}
Write-Host "Version: $GitVersion" -ForegroundColor Yellow

# Stamp _FALLBACK_VERSION in version.py so the frozen exe uses the right version
$versionFile = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Definition) "version.py"
if (Test-Path $versionFile) {
    (Get-Content $versionFile) -replace '^_FALLBACK_VERSION = .*', "_FALLBACK_VERSION = `"$GitVersion`"" |
        Set-Content $versionFile -Encoding UTF8
    Write-Host "Stamped version.py with $GitVersion" -ForegroundColor Green
}

Invoke-Py @("-m","PyInstaller","-y",$SpecFile)

if (-not (Test-Path "dist\$AppName")) {
    throw "Build failed: dist\$AppName not found. Check your spec name and exe name."
}

$ZipName = "$AppName-$GitVersion.zip"

Write-Host "=== Creating zip: $ZipName ===" -ForegroundColor Cyan
Compress-Archive -Path "dist\$AppName\*" -DestinationPath $ZipName -Force

# Build a second artifact with reducedMode flipped to false, for RUO distribution
$RuoZipName = "$AppName-${GitVersion}_RUO.zip"
$configPath = "dist\$AppName\_internal\config\app_config.json"
if (-not (Test-Path $configPath)) {
    throw "RUO build failed: bundled config not found at $configPath."
}

Write-Host "=== Creating RUO zip: $RuoZipName ===" -ForegroundColor Cyan
$origConfig = Get-Content -Raw $configPath
$ruoConfig = $origConfig -replace '"reducedMode"\s*:\s*true', '"reducedMode": false'
if ($ruoConfig -eq $origConfig) {
    throw "RUO build failed: did not find 'reducedMode: true' in $configPath to flip."
}
Set-Content -Path $configPath -Value $ruoConfig -Encoding UTF8 -NoNewline
try {
    Compress-Archive -Path "dist\$AppName\*" -DestinationPath $RuoZipName -Force
} finally {
    Set-Content -Path $configPath -Value $origConfig -Encoding UTF8 -NoNewline
}

Write-Host "=== Build complete ===" -ForegroundColor Green
Write-Host "ZIP file: $ZipName" -ForegroundColor Green
Write-Host "RUO ZIP file: $RuoZipName" -ForegroundColor Green

if ($OpenFolder) {
    Start-Process explorer.exe "/select,$(Resolve-Path $ZipName)"
}
