# Code signing (SSL.com EV via eSigner cloud)

Release artifacts are Authenticode-signed with Openwater's SSL.com **EV
code-signing certificate**. Post-2023 CA/B Forum rules forbid EV keys from
existing as an exportable PFX, so the private key lives in SSL.com's
**eSigner** cloud HSM and never touches the runner. CI uses **eSigner CKA**
(SSL.com's Cloud Key Adapter): it registers a Windows CNG key provider and
loads the cert into `Cert:\CurrentUser\My`, so native `signtool` signs
against a store thumbprint while each signature is actually performed in
SSL.com's cloud.

## Pipeline split: what builds where (#501)

**Nothing is generated without a tag** — `release-build.yml` triggers on
tag pushes only (branch-push builds are gone; HIL keys its dev set off
dev/rc tag builds now).

| Trigger | Windows artifacts | Signed? |
|---|---|---|
| `X.Y.Z-dev.N` / `X.Y.Z-rc.N` tag (automatic) | Both portables + both installers | **Nothing** (0 signings) — QA validates scan *and install* flows on these |
| `X.Y.Z` production tag (automatic) | Both portables + the **Research** installer | Research installer chain + the shared exe (**4 signings**); missing eSigner secrets fail the build |
| `signed-installers.yml` (manual dispatch, production tag, after QA sign-off) | **Clinical** installer (repacked from the tag's QA-validated portable zip) | Fully (**3 signings** — the exe inside the zip is already signed; `sign.ps1` skips it) |

- The production tag build deliberately does **not** build a clinical
  installer, so an unsigned clinical installer can never appear on a
  production release — the manual workflow is its only source.
- `signed-installers.yml` fails hard if the eSigner secrets are missing,
  and both signing paths end in a `Get-AuthenticodeSignature` gate that
  refuses to publish anything not `Valid`.
- Repacking (instead of rebuilding) in the manual workflow guarantees the
  signed clinical installer contains the exact bytes QA validated; a
  rebuild could silently pick up a newer SDK from PyPI.
- **Distribution policy:** Clinical software ships **only** as the signed
  installer from `signed-installers.yml`, on full releases. Research
  ships as the auto-signed installer (full releases) or the unsigned
  portable zip. The Clinical portable zip is a QA-only artifact and is
  never distributed. Testers see SmartScreen warnings on unsigned dev/rc
  artifacts — expected and internal-only.

## What gets signed, and where

| Artifact | Signed by |
|---|---|
| `Open-Motion.exe` in the dist (→ both portables and every MSI harvest it) | production tag build (`package_artifacts.ps1` → `installer/sign.ps1`), once — `sign.ps1` skips already-signed files everywhere else |
| `Open-Motion-Research.msi` + Research Burn engine + Setup bundle | production tag build (`release-build.yml` → `installer/build_installer.ps1` → `sign.ps1`) |
| `Open-Motion.msi` + Clinical Burn engine + Setup bundle | `signed-installers.yml` manual dispatch → `installer/build_installer.ps1` → `sign.ps1` |
| WinUSB driver catalogs + `OpenMotionDriver-x64.msi` | `openmotion-sdk` repo, `driver-msi.yml` (sdk#216), once per driver change — the signed zip is vendored here as `resources/OpenMotionDriver-x64.zip` and never rebuilt or re-signed per app release |

Everything funnels through `installer/sign.ps1`, driven by one
environment variable: `CODESIGN_THUMBPRINT`. Unset → every signing step
no-ops and the build ships unsigned (correct for dev/rc and local
builds; both signed paths guard against a silent skip with their
signature-verify gates). Set → files already validly signed by that
exact cert are skipped, so repeated packaging passes never re-spend a
signing. In CI the "Set up eSigner CKA" steps set it after loading the
cert; a local/self-hosted build with a locally-installed cert works the
same way.

**Signing budget** — eSigner cloud signings are metered (fixed allowance
per year); timestamping is free.

| Event | Signings |
|---|---|
| dev/rc tag build | **0** |
| Production tag build (automatic — exe + Research installer chain) | **4** |
| Signed Installers dispatch, clinical, production tag | **3** |
| **Total per production release** | **7** |
| Driver refresh (sdk repo, rare) | **5** (4 catalogs + driver MSI) |
| Extra: signing an rc installer for a beta update push | 4 first variant (unsigned exe), +3 per additional |

macOS is unaffected: the DMG stays ad-hoc signed (Apple notarization is a
separate, unrelated pipeline).

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

## Testing the pipeline

- Run **Signed Installers** against an existing (pre-release) tag with
  `upload_to_release` unchecked — signs real artifacts and uploads them
  only as workflow artifacts, leaving the release untouched. Costs 4
  signings for one variant on a dev/rc tag (unsigned exe), +3 per
  additional variant.
- For a dry run against SSL.com's **sandbox** environment instead of the
  production cert: set repo variable `ES_MODE=sandbox` and temporarily
  point the ES_* secrets at sandbox.ssl.com credentials. Unset when done.
- The target tag must postdate the signing + pipeline-split PRs: the
  workflow runs the *tag's* copy of `installer/sign.ps1` and
  `scripts/build_common.ps1`, and older tags' scripts predate the
  signtool discovery/retry logic.

Verify any produced artifact:

```powershell
Get-AuthenticodeSignature .\Open-Motion-Setup-1.6.0.exe | Format-List Status, SignerCertificate
# Status must be Valid; the signer should be the Openwater EV cert, not CN=Openwater WinUSB
```

## Driver (openmotion-sdk)

`driver-msi.yml` uses the same CKA recipe to sign the four driver
catalogs and the driver MSI, replacing the retired self-signed
`CN=Openwater WinUSB` scheme (which required installing a private root
cert on every user machine). **EV signing runs only on manual
`workflow_dispatch`** — signings are metered, and the driver rarely
changes, so it is signed once per driver change: dispatch the workflow,
download the `OpenMotionDriver-x64` artifact, verify its signature,
commit it in the SDK repo as `winusb-driver/OpenMotionDriver-x64.zip`
(the canonical copy), and vendor the same zip here as
`resources/OpenMotionDriver-x64.zip` so the Setup bundles chain the
EV-signed driver MSI. PR-triggered runs build-validate with the legacy
self-signed key at zero eSigner cost. Details: sdk#216.

## Deliberate follow-ups

- **`_REQUIRE_SIGNED_UPDATES` (motion_connector.py) is still `False`.**
  Flip it to `True` one release *after* the first signed release ships
  and verifies, so the in-app updater starts refusing unsigned bundles.
  Caveat: dev/rc releases carry **unsigned** Setup bundles (built for QA
  install-flow validation), so after the flip a beta-channel updater
  would refuse them — either dispatch Signed Installers for the rc tag
  to push a signed beta, or keep the flip scoped to production-update
  channels. (A production release carries no clinical Setup bundle until
  the manual dispatch runs; until then the updater logs "no installer
  asset found" for it and offers nothing — run the dispatch promptly
  after QA sign-off.)
- **Driver: Microsoft attestation signing** (optional, later). The EV
  cert qualifies us to register a Microsoft Partner Center hardware
  account; attestation-signed driver packages install with no
  TrustedPublisher step and no prompt at all.
- eSigner billing: cloud signings are metered per the eSigner service
  plan — check the tier if release cadence increases significantly.
