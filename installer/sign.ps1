# installer/sign.ps1 — Authenticode-sign the given files, or skip if no cert.
param([Parameter(Mandatory = $true)][string[]]$Files)

$thumb = $env:CODESIGN_THUMBPRINT
if (-not $thumb) {
    Write-Host "signing skipped (CODESIGN_THUMBPRINT not set)" -ForegroundColor Yellow
    return
}

foreach ($f in $Files) {
    Write-Host "signing $f" -ForegroundColor Cyan
    & signtool sign /sha1 $thumb /fd SHA256 `
        /tr http://timestamp.digicert.com /td SHA256 $f
    if ($LASTEXITCODE -ne 0) { throw "signtool failed for $f" }
}
