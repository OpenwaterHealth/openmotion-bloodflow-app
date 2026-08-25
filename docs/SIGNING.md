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

- **`release-build.yml`** (automatic: tag pushes, `next`/`main` pushes,
  plain dispatch) builds **unsigned portable zips only** — the QA
  artifacts. It never installs WiX, never touches eSigner, and never
  builds an installer.
- **`signed-installers.yml`** (manual `workflow_dispatch`, given an
  existing release tag) is the **only** place app artifacts are signed.
  It downloads the tag's QA-validated portable zip, signs
  `Open-Motion.exe` inside it, repacks it into the per-variant MSI + Burn
  Setup bundles, signs those, **verifies every signature is `Valid`**,
  and attaches the Setup bundles to the tag's GitHub release. It fails
  hard if the eSigner secrets are missing — an unsigned artifact from
  that workflow is a bug, not a fallback. Repacking (instead of
  rebuilding) guarantees the signed installer contains the exact bytes QA
  validated; a rebuild could silently pick up a newer SDK from PyPI.
- **Distribution policy:** Clinical software ships **only** as the signed
  installer from `signed-installers.yml`. Research ships as the signed
  installer or the unsigned portable zip. The Clinical portable zip is a
  QA-only artifact and is never distributed. Testers see SmartScreen
  warnings on unsigned QA builds — expected and internal-only.

## What gets signed, and where

| Artifact | Signed by |
|---|---|
| `Open-Motion.exe` (from the tag's portable zip; harvested into both MSIs) | `signed-installers.yml` → `installer/sign.ps1` |
| `Open-Motion.msi` / `Open-Motion-Research.msi` | `installer/build_installer.ps1` → `sign.ps1` |
| Burn Setup bundles (engine signed detached, then the reattached bundle) | `installer/build_installer.ps1` → `sign.ps1` |
| WinUSB driver catalogs + `OpenMotionDriver-x64.msi` | `openmotion-sdk` repo, `driver-msi.yml` (sdk#216) — the signed zip is then vendored here as `resources/OpenMotionDriver-x64.zip` |

Everything funnels through `installer/sign.ps1`, which is driven by one
environment variable: `CODESIGN_THUMBPRINT`. Unset → every signing step
no-ops and the build ships unsigned (correct for QA/local builds; the
signed-installers workflow guards against it with its signature-verify
gate). In CI the "Set up eSigner CKA" step of `signed-installers.yml`
sets it after loading the cert. `scripts/package_artifacts.ps1` also
signs the dist exe when a thumbprint is present, so a local/self-hosted
build with a locally-installed cert still produces fully signed output.

**Signing budget** — eSigner cloud signings are metered (fixed allowance
per year); timestamping is free. Per dispatch of `signed-installers.yml`:

| Run | Signings |
|---|---|
| Both variants | **7** (exe ×1, MSI ×2, Burn engine ×2, Setup bundle ×2) |
| One variant | **4** |
| Driver refresh (sdk repo, rare) | **5** (4 catalogs + driver MSI) |

Nothing signs automatically anywhere, so the yearly spend is exactly the
dispatches you choose to run — typically one both-variant run (7) per
production release, plus an optional run for an rc tag when a signed
beta installer is wanted (see the updater note below).

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
  only as workflow artifacts, leaving the release untouched. Costs the
  normal 7 (or 4 with a single `variant`) signings.
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
- **In-app updates and pre-releases:** releases only carry Setup bundles
  when `signed-installers.yml` was dispatched for that tag, so a dev/rc
  release normally has none. The updater handles this softly — a release
  with no matching `*-Setup-*.exe` asset is logged and not offered
  (`_select_update_asset`) — so beta-channel users simply see no update
  for unbundled pre-releases. To push a beta through the in-app updater,
  dispatch Signed Installers for that rc tag (7 signings).
- **Driver: Microsoft attestation signing** (optional, later). The EV
  cert qualifies us to register a Microsoft Partner Center hardware
  account; attestation-signed driver packages install with no
  TrustedPublisher step and no prompt at all.
- eSigner billing: cloud signings are metered per the eSigner service
  plan — check the tier if release cadence increases significantly.
