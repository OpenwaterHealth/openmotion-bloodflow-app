# installer/sign.ps1 — Authenticode-sign the given files, or skip if no cert.
#
# CODESIGN_THUMBPRINT selects a code-signing cert already present in the
# machine's cert store. In CI that is the SSL.com EV cert, loaded into
# Cert:\CurrentUser\My by eSigner CKA (the key itself stays in SSL.com's
# cloud HSM) — see the "eSigner CKA" step in release-build.yml and
# docs/SIGNING.md.
param([Parameter(Mandatory = $true)][string[]]$Files)

$thumb = $env:CODESIGN_THUMBPRINT
if (-not $thumb) {
    Write-Host "signing skipped (CODESIGN_THUMBPRINT not set)" -ForegroundColor Yellow
    return
}

# signtool is not on PATH on the GitHub runners — fall back to the newest
# Windows Kits copy.
$signtool = (Get-Command signtool.exe -ErrorAction SilentlyContinue).Source
if (-not $signtool) {
    $kits = "${env:ProgramFiles(x86)}\Windows Kits\10\bin"
    $signtool = Get-ChildItem "$kits\10.0.*\x64\signtool.exe" -ErrorAction SilentlyContinue |
        Sort-Object { [version]$_.Directory.Parent.Name } -Descending |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $signtool) { throw "signtool.exe not found (PATH or Windows Kits)" }

foreach ($f in $Files) {
    # eSigner signings are metered: skip files already validly signed by this
    # exact cert. This is what keeps the shared dist exe at ONE signing per
    # release even though multiple packaging calls each request it (the
    # production tag build's clinical/research passes, and a later
    # signed-installers.yml dispatch repacking the same zip). A file signed
    # by a different cert (or with a broken chain) is re-signed as before.
    $existing = Get-AuthenticodeSignature $f
    if ($existing.Status -eq 'Valid' -and $existing.SignerCertificate.Thumbprint -eq $thumb) {
        Write-Host "already signed by $thumb — skipping $f" -ForegroundColor DarkGray
        continue
    }
    Write-Host "signing $f" -ForegroundColor Cyan
    # Both the eSigner cloud signing call and the timestamp server can flake
    # transiently, so retry before failing the build.
    $attempts = 3
    for ($i = 1; $i -le $attempts; $i++) {
        & $signtool sign /sha1 $thumb /fd SHA256 `
            /tr http://ts.ssl.com /td SHA256 $f
        if ($LASTEXITCODE -eq 0) { break }
        if ($i -eq $attempts) { throw "signtool failed for $f after $attempts attempts" }
        Write-Host "signtool exit $LASTEXITCODE -- retrying ($i/$attempts)" -ForegroundColor Yellow
        Start-Sleep -Seconds 10
    }
}
