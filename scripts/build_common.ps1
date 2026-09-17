# scripts/build_common.ps1
# Shared helpers for the unified build/packaging path. Dot-source this:
#   . (Join-Path $PSScriptRoot build_common.ps1)
# Single source of truth for the build-variant stamp + version derivation +
# zip, so the logic isn't copy-pasted across build_and_zip.ps1,
# package_artifacts.ps1, installer/build_installer.ps1, and
# scripts/build_update_test_bundles.ps1.
#
# Since #546 there is no bundled app_config.json to flip after PyInstaller
# runs: the Clinical/Research split is a compile-time constant
# (CLINICAL_MODE in config/app_config.py) stamped BEFORE PyInstaller, one
# build per variant. portableMode is not stamped at all - a frozen build
# derives it from the installer's HKLM InstallDir marker (installer/app.wxs,
# utils/app_paths.py), so the exe in the portable zip and the one inside the
# installer stay byte-identical.

$script:ConfigModuleRelPath = "config\app_config.py"

function Get-NumericVersion {
    # Extract numeric X.Y.Z from any version string (the MSI requires numeric).
    param([Parameter(Mandatory)][string]$Version)
    if ($Version -match '(\d+)\.(\d+)\.(\d+)') {
        return "$($matches[1]).$($matches[2]).$($matches[3])"
    }
    return "0.0.0"
}

function Get-BuildVersion {
    # Full version string from git, with a dev-timestamp fallback (matches the
    # prior inline behavior in build_and_zip.ps1). .Full names the zips; .Numeric
    # feeds the MSI.
    param()
    $full = $null
    try {
        $full = (git describe --tags --dirty --always --long 2>$null).Trim()
        if (-not $full) { throw "empty" }
    } catch {
        $full = "dev-$(Get-Date -Format 'yyyyMMdd_HHmmss')"
    }
    return [pscustomobject]@{ Full = $full; Numeric = (Get-NumericVersion -Version $full) }
}

function Get-ConfigModulePath {
    # Absolute path of config/app_config.py relative to the repo root.
    param([string]$Root = (Split-Path -Parent $PSScriptRoot))
    return (Join-Path $Root $script:ConfigModuleRelPath)
}

function Set-BuildVariant {
    # Stamp CLINICAL_MODE into config/app_config.py BEFORE PyInstaller runs.
    # Returns the ORIGINAL module text so the caller can restore it
    # (Restore-ConfigModule) in a finally block - the repo value is the
    # Research default and must never be committed flipped.
    param(
        [Parameter(Mandatory)][bool]$Clinical,
        [string]$ModulePath = (Get-ConfigModulePath)
    )
    if (-not (Test-Path $ModulePath)) { throw "config module not found at $ModulePath" }
    $orig = [System.IO.File]::ReadAllText($ModulePath)
    # (?=\r?$): a .NET multiline '$' matches before "\n" only, so a CRLF
    # checkout (the GitHub Windows runner, autocrlf=true) never matched and
    # the first Nuitka CI build died here (#548). The lookahead keeps the
    # original line ending untouched.
    $stamp = '(?m)^CLINICAL_MODE = (True|False)(?=\r?$)'
    if ($orig -notmatch $stamp) {
        throw "CLINICAL_MODE stamp line not found in $ModulePath"
    }
    $val = if ($Clinical) { "True" } else { "False" }
    $new = [regex]::Replace($orig, $stamp, "CLINICAL_MODE = $val")
    [System.IO.File]::WriteAllText($ModulePath, $new, (New-Object System.Text.UTF8Encoding $false))
    return $orig
}

function Set-CompiledConfigValue {
    # Stamp one APP_CONFIG entry in config/app_config.py with a Python
    # literal, e.g. -Key updateApiUrl -PythonValue "'http://localhost:8000/x'".
    # Used only by the update-bundle test build; a release never does this.
    # Returns the ORIGINAL module text for Restore-ConfigModule.
    param(
        [Parameter(Mandatory)][string]$Key,
        [Parameter(Mandatory)][string]$PythonValue,
        [string]$ModulePath = (Get-ConfigModulePath)
    )
    $orig = [System.IO.File]::ReadAllText($ModulePath)
    $pattern = '(?m)^(\s*"' + [regex]::Escape($Key) + '": ).*,(?=\r?$)'
    if ($orig -notmatch $pattern) { throw "config key '$Key' not found in $ModulePath" }
    $new = [regex]::Replace($orig, $pattern, ('${1}' + $PythonValue + ','))
    [System.IO.File]::WriteAllText($ModulePath, $new, (New-Object System.Text.UTF8Encoding $false))
    return $orig
}

function Restore-ConfigModule {
    # Write back the original text captured by Set-BuildVariant /
    # Set-CompiledConfigValue (UTF-8, no BOM).
    param(
        [Parameter(Mandatory)][string]$Text,
        [string]$ModulePath = (Get-ConfigModulePath)
    )
    [System.IO.File]::WriteAllText($ModulePath, $Text, (New-Object System.Text.UTF8Encoding $false))
}

function Get-AppPython {
    # Resolve the Python that builds the app: PATH, then CONDA_PREFIX, else
    # $null (callers fall back to `conda run -n <env>`).
    param()
    try { return (Get-Command python -ErrorAction Stop).Source } catch {}
    if ($env:CONDA_PREFIX) {
        $cand = Join-Path $env:CONDA_PREFIX "python.exe"
        if (Test-Path $cand) { return $cand }
    }
    return $null
}

function Invoke-AppPython {
    # Run python with the given argument list via Get-AppPython or conda run.
    param(
        [Parameter(Mandatory)][string[]]$Arguments,
        [string]$CondaEnv = "ow-motion"
    )
    $py = Get-AppPython
    if ($py) { & $py @Arguments } else { & conda run -n $CondaEnv python @Arguments }
}

function Invoke-VariantBuild {
    # One PyInstaller build for one variant: stamp CLINICAL_MODE, build the
    # onefile exe into dist\<variant>\Open-Motion\Open-Motion.exe (work dir
    # build\<variant>), restore the stamp. The Open-Motion\ folder is kept so
    # the portable zip and the MSI harvest have the same shape as before
    # #547; it now holds exactly one file.
    param(
        [Parameter(Mandatory)][ValidateSet("clinical", "research")][string]$Variant,
        [string]$SpecFile = "openwater.spec",
        [string]$DistRoot = "dist",
        [string]$WorkRoot = "build",
        [string]$CondaEnv = "ow-motion"
    )
    $orig = Set-BuildVariant -Clinical ($Variant -eq "clinical")
    try {
        $distPath = Join-Path (Join-Path $DistRoot $Variant) "Open-Motion"
        $workPath = Join-Path $WorkRoot $Variant
        # Start from an empty dist directory: a stale onedir tree (_internal\)
        # from an older build would otherwise ride along into the zip and the
        # MSI next to the onefile exe (the spec refuses that too).
        if (Test-Path $distPath) { Remove-Item -Recurse -Force $distPath }
        Write-Host "=== PyInstaller ($Variant) -> $distPath ===" -ForegroundColor Cyan
        Invoke-AppPython -CondaEnv $CondaEnv -Arguments @(
            "-m", "PyInstaller", "-y", $SpecFile,
            "--distpath", $distPath, "--workpath", $workPath
        )
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed for variant '$Variant'" }
        $exe = Join-Path $distPath "Open-Motion.exe"
        if (-not (Test-Path $exe)) { throw "PyInstaller output missing: $exe" }
        return $distPath
    } finally {
        Restore-ConfigModule -Text $orig
    }
}

function New-PortableZip {
    # Zip a variant's dist tree so the archive contains a top-level Open-Motion\
    # folder (the released structure - matches CI's `Compress-Archive dist\*`).
    # $DistDir is dist\<variant>\Open-Motion; zip its PARENT's contents. Since
    # #547 the folder holds the single onefile exe; it is kept so a portable
    # user's logs\ and data\ land beside the exe instead of loose in Downloads.
    param(
        [Parameter(Mandatory)][string]$DistDir,
        [Parameter(Mandatory)][string]$OutZip
    )
    $parent = Split-Path -Parent $DistDir
    Compress-Archive -Path (Join-Path $parent '*') -DestinationPath $OutZip -Force
}

function Test-WixAvailable {
    return [bool](Get-Command wix -ErrorAction SilentlyContinue)
}
