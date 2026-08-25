# Code signing (SSL.com EV via eSigner cloud)

Release artifacts are Authenticode-signed with Openwater's SSL.com **EV
code-signing certificate**. Post-2023 CA/B Forum rules forbid EV keys from
existing as an exportable PFX, so the private key lives in SSL.com's
**eSigner** cloud HSM and never touches the runner. CI uses **eSigner CKA**
(SSL.com's Cloud Key Adapter): it registers a Windows CNG key provider and
loads the cert into `Cert:\CurrentUser\My`, so native `signtool` signs
against a store thumbprint while each signature is actually performed in
SSL.com's cloud.

## What gets signed, and where

| Artifact | Signed by |
|---|---|
| `Open-Motion.exe` inside the PyInstaller dist (→ both portable zips, harvested into both MSIs) | `scripts/package_artifacts.ps1` → `installer/sign.ps1` |
| `Open-Motion.msi` / `Open-Motion-Research.msi` | `installer/build_installer.ps1` → `sign.ps1` |
| Burn Setup bundles (engine signed detached, then the reattached bundle) | `installer/build_installer.ps1` → `sign.ps1` |
| WinUSB driver catalogs + `OpenMotionDriver-x64.msi` | `openmotion-sdk` repo, `driver-msi.yml` (sdk#216) — the signed zip is then vendored here as `resources/OpenMotionDriver-x64.zip` |

Everything funnels through `installer/sign.ps1`, which is driven by one
environment variable: `CODESIGN_THUMBPRINT`. Unset → every signing step
no-ops and the build ships unsigned (the pre-EV behavior). In CI the
"Set up eSigner CKA" step of `release-build.yml` sets it after loading the
cert; the `CODESIGN_THUMBPRINT` repo secret remains as a manual fallback
for signing with a locally-installed cert (e.g. a self-hosted runner).

**When CI signs:** tag builds, plus `workflow_dispatch` runs with the
`sign` input checked. Pushes to `next`/`main` build unsigned — no reason
to spend cloud signings on throwaway builds.

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

## Testing the pipeline without cutting a release

- Run **Build & Release** via `workflow_dispatch` with `sign` checked —
  signs real artifacts, uploads them as workflow artifacts, creates no
  GitHub release.
- For a dry run against SSL.com's **sandbox** environment instead of the
  production cert: set repo variable `ES_MODE=sandbox` and temporarily
  point the ES_* secrets at sandbox.ssl.com credentials. Unset when done.

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
download the `OpenMotionDriver-x64` artifact, verify its signature, and
commit it here as `resources/OpenMotionDriver-x64.zip` so the Setup
bundles chain the EV-signed driver MSI. PR-triggered runs build-validate
with the legacy self-signed key at zero eSigner cost. Details: sdk#216.

## Deliberate follow-ups

- **`_REQUIRE_SIGNED_UPDATES` (motion_connector.py) is still `False`.**
  Flip it to `True` one release *after* the first signed release ships
  and verifies, so the in-app updater starts refusing unsigned bundles.
- **Driver: Microsoft attestation signing** (optional, later). The EV
  cert qualifies us to register a Microsoft Partner Center hardware
  account; attestation-signed driver packages install with no
  TrustedPublisher step and no prompt at all.
- eSigner billing: cloud signings are metered per the eSigner service
  plan — check the tier if release cadence increases significantly.
