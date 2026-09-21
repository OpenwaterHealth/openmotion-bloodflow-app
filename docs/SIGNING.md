# Code signing (SSL.com EV via eSigner cloud)

Release artifacts are Authenticode-signed with Openwater's SSL.com **EV
code-signing certificate**. Post-2023 CA/B Forum rules forbid EV keys from
existing as an exportable PFX, so the private key lives in SSL.com's
**eSigner** cloud HSM and never touches the runner. CI uses **eSigner CKA**
(SSL.com's Cloud Key Adapter): it registers a Windows CNG key provider and
loads the cert into `Cert:\CurrentUser\My`, so native `signtool` signs
against a store thumbprint while each signature is actually performed in
SSL.com's cloud.

**Every signing is metered.** Do not add signing to more build paths, and
do not trigger a signed `workflow_dispatch`, without asking first.

## What gets signed, and where

| Artifact | Signed by |
|---|---|
| `Open-Motion.exe`, the onefile build (#547) that is the whole portable zip and the whole MSI payload | `scripts/package_artifacts.ps1` → `installer/sign.ps1`, once per variant |
| `main.dll` **inside** the onefile payload (#579): the compiled app itself (Nuitka 4.x builds the program as a DLL that the onefile bootstrap loads; there is no inner exe), signed between Nuitka's standalone folder and the onefile pack, so what the bootstrap extracts to `%TEMP%` at launch is signed too (one signing per variant). Third-party payload files are not signed by us. | `scripts/nuitka_sign_payload.py` (Nuitka user plugin) → `sign.ps1` |
| Burn Setup bundles: the engine signed detached, then the reattached bundle (two signings per variant) | `installer/build_installer.ps1` → `sign.ps1` |
| WinUSB driver catalogs + `OpenMotionDriver-x64.msi` | `openmotion-sdk` repo, `driver-msi.yml` (sdk#216) — the signed zip is then vendored here as `resources/OpenMotionDriver-x64.zip` |

That is **four signings per variant, and one variant per signed build**
(payload `main.dll`, exe, engine, bundle; it was three before #579)
(#573: Research is built here, Clinical in the private repo): exe, engine,
bundle. The engine and the bundle are separate signatures because Burn
extracts and caches the engine on its own for elevation, repair and
uninstall, so WiX requires detach → sign engine → reattach → sign bundle
(<https://docs.firegiant.com/wix/tools/signing/>).

**Not signed, on purpose: the app MSI** (`Open-Motion.msi` /
`Open-Motion-Research.msi`, #569). It is never a release asset (only the
Setup bundle ships, and the updater verifies only the bundle), Burn runs it
from its already-elevated engine so it never raises its own UAC prompt, and
Burn verifies it by the hash in the bundle manifest, which sits under the
engine signature. Accepted trade-off: a managed PC whose AppLocker / WDAC
policy allows Windows Installer packages *by publisher* would block the
unsigned MSI. If a site needs that, re-add the one `sign.ps1` call after the
MSI build in `installer/build_installer.ps1` (before the bundle build, which
records the MSI's hash); it costs two more signings per signed build.

Everything funnels through `installer/sign.ps1`, which is driven by one
environment variable: `CODESIGN_THUMBPRINT`. Unset → every signing step
no-ops and the build ships unsigned. In CI the "Set up eSigner CKA" step of
the shared build action (`.github/actions/windows-build`, called by
`release-build.yml` with `sign: true`) sets it after loading the cert; the `CODESIGN_THUMBPRINT`
repo secret remains as a manual fallback for signing with a locally
installed cert (e.g. a self-hosted runner).

**When CI signs (#573):**

| Build | Research (this repo, public GitHub Release) | Clinical (private repo → Google Drive) |
|---|---|---|
| `X.Y.Z-dev.N` | unsigned zip + installer | unsigned zip + installer |
| `X.Y.Z-rc.N` | **signed** zip + installer | **signed** zip + installer |
| `X.Y.Z` | **signed** installer (no portable zip) | nothing automatic |
| manual `clinical-release.yml` on an `X.Y.Z` tag | n/a | **signed** installer |
| pushes to `next` / `main` | unsigned, no release | never built |

A `workflow_dispatch` of `release-build.yml` with the `sign` input checked
also signs. eSigner cloud signings are metered: 4 per signed Research build,
4 per signed Clinical build, so **an rc tag costs 8** and a production release
4 + 4 for the manual Clinical installer. **Signing rc tags (both variants) is
deliberate for the first releases under #573**, to prove the signing path
before a production release, **and is expected to be dropped later** to
conserve quota. Clinical: `sign: false` in the private repo's
`clinical-prerelease.yml`. Research:
change `!contains(github.ref, '-dev.')` back to `!contains(github.ref, '-')`
in the `sign:` expression of `release-build.yml` (and the guard in
`tests/test_release_workflow.py`). While it lasts, the in-app beta channel
can install an rc (#544). Testers may see SmartScreen warnings on dev
installers — expected and internal-only.

**Clinical never touches this repo's CI.** The repo is public, so a release
asset, a workflow artifact and the Actions log are all world-readable. The
private `OpenwaterHealth/openmotion-desktop-app-clinical` repo checks this
one out at the tag, runs the same `.github/actions/windows-build` with
`variants: clinical`, and uploads to a Shared Drive. `release-build.yml`'s
`notify-clinical` job starts its pre-release workflow on dev/rc tags and
never waits for it; the signed production installer is a manual run there,
which refuses a tag not on `main` and any output that is not validly signed.

macOS is unaffected: the DMG stays ad-hoc signed (Apple notarization is a
separate, unrelated pipeline).

## History: nothing shipped signed before 2026-09-17

The signing step was written and exercised on the PR branch (#443; the
2026-08-25 verification run was a manual dispatch there), but the PR was not
merged until 2026-09-17. Every release up to and including **1.5.2 shipped
unsigned**, and so did the previously vendored driver zip. The first signed
release is therefore the first production tag cut after that date; verify
it (below) before treating any tracker item that depends on signing as met.

## The certificate

Open Water Internet, Inc. (San Francisco, CA), issued by SSL.com EV Code
Signing Intermediate CA RSA R3, valid 2026-08-05 to 2027-11-06.

| Digest of the DER-encoded leaf | Value |
|---|---|
| SHA-1 (what Windows calls "Thumbprint") | `ADFCD7CB6A7900A204F91EDF9E5ADADB5A8C1AB4` |
| SHA-256 | `BE77B247E7776240EC65DC854C86423C4C15C00F22365385BE101189FC71AC1F` |

The in-app updater (`app_updater.ACCEPTED_SIGNER_SHA256`, #544) installs a
downloaded bundle only when Windows verifies its signature **and** the
signer is this certificate. **Rotation:** add the successor certificate's
SHA-256 to that set and ship a release signed with the *old* certificate
first, so installed apps learn the new pin before it is used.

## One-time setup (account + secrets)

1. **Finish eSigner enrollment** on the approved EV order at ssl.com:
   the order's certificate must be *attested into eSigner* (chosen as the
   cloud-delivery option, not a shipped YubiKey).
2. **Create the eSigner TOTP secret**: order → Signing Credentials →
   eSigner authenticator QR code → copy the **text version** of the
   secret. This is what lets CI generate the per-signature OTPs.
3. **Malware Blocker**: eSigner's pre-signing malware scan is only
   supported through CodeSignTool/eSigner Express. For CKA (signtool)
   signing it must be **disabled** on the signing credential in the
   SSL.com portal, or cloud signing requests fail.
4. **Create the GitHub secrets** — org-level, granted to
   `openmotion-bloodflow-app` **and** `openmotion-sdk` (the driver build
   uses the same cert):

   ```bash
   gh secret set ES_USERNAME    --org OpenwaterHealth --visibility selected --repos "openmotion-bloodflow-app,openmotion-sdk"
   gh secret set ES_PASSWORD    --org OpenwaterHealth --visibility selected --repos "openmotion-bloodflow-app,openmotion-sdk"
   gh secret set ES_TOTP_SECRET --org OpenwaterHealth --visibility selected --repos "openmotion-bloodflow-app,openmotion-sdk"
   ```

   `ES_USERNAME`/`ES_PASSWORD` are the SSL.com account login; the TOTP
   secret is from step 2. (Per-repo secrets work too if org policy is in
   the way.)

## Testing the pipeline without cutting a release

- Run **Build & Release** via `workflow_dispatch` with `sign` checked —
  signs real artifacts (metered!), uploads them as workflow artifacts,
  creates no GitHub release.
- For a dry run against SSL.com's **sandbox** environment instead of the
  production cert: set repo variable `ES_MODE=sandbox` and temporarily
  point the ES_* secrets at sandbox.ssl.com credentials. Unset when done.

Verify any produced artifact:

```powershell
Get-AuthenticodeSignature .\Open-Motion-Setup-1.6.0.exe | Format-List Status, SignerCertificate
# Status must be Valid; the signer must be the Openwater EV cert above
```

## Driver (openmotion-sdk)

`driver-msi.yml` uses the same CKA recipe to sign the four driver
catalogs and the driver MSI, replacing the retired self-signed
`CN=Openwater WinUSB` scheme (which required installing a private root
cert on every user machine). **EV signing runs only on manual
`workflow_dispatch`** — signings are metered, and the driver rarely
changes, so it is signed once per driver change: dispatch the workflow,
download the `OpenMotionDriver-x64` artifact, verify its signature,
commit it in the SDK repo as the canonical copy, and vendor the same zip
here as `resources/OpenMotionDriver-x64.zip` so the Setup bundles chain the
EV-signed driver MSI. PR-triggered runs build-validate with the legacy
self-signed key at zero eSigner cost. Details: sdk#216.

## Deliberate follow-ups

- **Driver: Microsoft attestation signing** (optional, later). The EV
  cert qualifies us to register a Microsoft Partner Center hardware
  account; attestation-signed driver packages install with no
  TrustedPublisher step and no prompt at all.
- eSigner billing: cloud signings are metered per the eSigner service
  plan — check the tier if release cadence increases significantly.
- Research rc bundles are signed since #573 (3 signings per rc). When that is
  dropped again, the in-app beta channel goes back to offering rc bundles and
  then refusing them (#544).
