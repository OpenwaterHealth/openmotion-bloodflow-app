# scripts/build_common.ps1
# Shared helpers for the unified build/packaging path. Dot-source this:
#   . (Join-Path $PSScriptRoot build_common.ps1)
# Single source of truth for the reducedMode flip + version derivation + zip,
# so the logic isn't copy-pasted across build_and_zip.ps1, package_artifacts.ps1,
# installer/build_installer.ps1, and scripts/build_update_test_bundles.ps1.

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

function Set-ReducedMode {
    # BOM-safe flip of "reducedMode" in a bundled app_config.json. Returns the
    # ORIGINAL file text so the caller can restore it. Writes UTF-8 WITHOUT BOM —
    # the app's json.load fails silently on a BOM (memory: config_edit_bom_breaks_load).
    param(
        [Parameter(Mandatory)][string]$ConfigPath,
        [Parameter(Mandatory)][bool]$Reduced
    )
    if (-not (Test-Path $ConfigPath)) { throw "config not found at $ConfigPath" }
    $orig = [System.IO.File]::ReadAllText($ConfigPath)
    if ($orig -notmatch '"reducedMode"\s*:\s*(true|false)') {
        throw "reducedMode key not found in $ConfigPath"
    }
    $val = if ($Reduced) { "true" } else { "false" }
    $new = [regex]::Replace($orig, '("reducedMode"\s*:\s*)(true|false)', "`${1}$val")
    [System.IO.File]::WriteAllText($ConfigPath, $new, (New-Object System.Text.UTF8Encoding $false))
    return $orig
}

function Set-PortableMode {
    # BOM-safe flip of "portableMode" in a bundled app_config.json. Portable
    # zips keep all writable state (config overrides, logs, scan data) next
    # to the exe; installers scatter it to %PROGRAMDATA% instead — see
    # utils/app_paths.py:writable_root. Does NOT return the original text —
    # callers should already hold that from an earlier Set-ReducedMode call
    # (or capture it themselves) since Restore-ConfigText only needs to run
    # once per artifact regardless of how many flags got flipped.
    param(
        [Parameter(Mandatory)][string]$ConfigPath,
        [Parameter(Mandatory)][bool]$Portable
    )
    if (-not (Test-Path $ConfigPath)) { throw "config not found at $ConfigPath" }
    $orig = [System.IO.File]::ReadAllText($ConfigPath)
    if ($orig -notmatch '"portableMode"\s*:\s*(true|false)') {
        throw "portableMode key not found in $ConfigPath"
    }
    $val = if ($Portable) { "true" } else { "false" }
    $new = [regex]::Replace($orig, '("portableMode"\s*:\s*)(true|false)', "`${1}$val")
    [System.IO.File]::WriteAllText($ConfigPath, $new, (New-Object System.Text.UTF8Encoding $false))
    return $orig
}

function Restore-ConfigText {
    # Write back the original text captured by Set-ReducedMode (no BOM).
    param(
        [Parameter(Mandatory)][string]$ConfigPath,
        [Parameter(Mandatory)][string]$Text
    )
    [System.IO.File]::WriteAllText($ConfigPath, $Text, (New-Object System.Text.UTF8Encoding $false))
}

function New-PortableZip {
    # Zip the whole dist tree so the archive contains a top-level OpenWaterApp\
    # folder (the released structure — matches CI's `Compress-Archive dist\*`).
    # $DistDir is dist\OpenWaterApp; zip its PARENT's contents.
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
