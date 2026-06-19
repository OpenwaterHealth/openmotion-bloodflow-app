# MSI Installer + In-App Updater — Design

**Date:** 2026-06-17
**Branch:** `claude/naughty-booth-e77327` (off `next` once rebased)
**Scope:** Workstream #1 of three. Workstreams #2 (tamper resistance) and #3
(cleanup) are running in parallel agent threads and are **out of scope here**,
except for one documented hand-off (config-overrides hardening → #2).

## Summary

Replace the current "PyInstaller `.zip` + manual unzip + manual Zadig driver
install" distribution with a real Windows installer and a real in-app updater:

1. **A single signed `OpenWater-Setup-X.Y.Z.exe`** (WiX Burn bundle) that
   installs the WinUSB **driver** and the **app into Program Files** in one run,
   one UAC prompt.
2. **An in-app "Update" button** that downloads that same bundle, verifies its
   signature, and performs an **in-place upgrade** (one UAC prompt), then
   relaunches — "like a regular app," within the constraints of a Program Files
   install.
3. The minimum **app-code change** required for a read-only Program Files
   install to work: relocate runtime-writable state to `%PROGRAMDATA%\OpenWater\`.

PyInstaller stays. The existing `openwater.spec` build is untouched — it already
solves the hard problem on this app (vendored `libusb` + runtime hooks for USB
enumeration). WiX wraps its output.

## Decisions (confirmed with user)

- **Toolchain:** WiX path, **not** Briefcase. Keep PyInstaller; WiX v5 packages
  `dist\OpenWaterApp\`. (Briefcase rejected: replaces the working build, expects
  a `src/` restructure, and its supported GUI bootstraps are Toga/PySide6 — not
  PyQt6+QML — so migrating a feature-complete app onto it is avoidable risk.)
- **Driver install:** WiX **Burn bundle** chaining driver MSI → app MSI. One
  artifact, one UAC prompt, install-time driver install; the same bundle is what
  the in-app updater downloads and runs.
- **Install location:** **Program Files** (per-machine).
- **Writable state:** **`%PROGRAMDATA%\OpenWater\`** (machine-wide, all users
  share settings + data; survives user-switching on a shared clinical PC).
- **Config split:** this spec owns the *mechanism* (read-only shipped defaults +
  writable overrides file). **#2 owns the hardening policy.**
- **Code signing:** no cert yet ("soon"). Signing is **wired-but-skippable** —
  a real `signtool` step that is a no-op until a cert is configured, so turning
  it on later is a config change, not a re-architecture. Target an **EV** cert
  (instant SmartScreen trust) for a clinical product.
- **Update friction:** **one UAC prompt per update** accepted. No privileged
  updater service (would add a persistent elevated component = new attack
  surface, against the tamper goal).
- **RUO variant:** **two distinct products** — separate clinical and RUO
  bundles with distinct `ProductName` + `UpgradeCode`, `reducedMode` baked into
  each variant's read-only config. They cannot cross-upgrade; each variant's
  updater pulls only its matching bundle.

## Background (current state)

- **Build:** `openwater.spec` → `dist\OpenWaterApp\` (one-dir). `build_and_zip.ps1`
  and `.github/workflows/release-build.yml` zip it and also produce an **RUO**
  zip by flipping `reducedMode: true → false` in the bundled
  `_internal\config\app_config.json`.
- **Driver:** `resources/OpenMotionDriver-x64.zip` already contains a signed
  **`OpenMotionDriver-x64.msi`** (+ `cab1.cab`). It is attached to every GitHub
  release today. Install is currently manual (Zadig / run the MSI by hand).
- **Update flow today:** `motion_connector.py:checkForUpdates()` (~line 3870)
  hits `GET /repos/OpenwaterHealth/openmotion-bloodflow-app/releases/latest`,
  finds the **`.zip`** asset, and `_version_newer` (tag-string comparison)
  decides if newer. On newer → `updateAvailable(version, url)` →
  `UpdateBanner.qml` shows a button that calls `openDownloadUrl()` =
  **opens the browser** to the zip. No in-place install.
- **Config today:**
  - `main.py:_load_app_config()` (~line 67) builds a `defaults` dict, then merges
    `resource_path("config", "app_config.json")` (the bundled file) over it
    (known keys only). `developerMode` already defaults to `false`.
  - `motion_connector.py:_save_app_config()` (~line 1469) writes the **whole**
    in-memory dict back to `resource_path("config", "app_config.json")`.
    `setConfig` / `setConfigs` / raw-CSV setters / `dataDirectory` setter all
    call it. The write is already wrapped in try/except (line ~1481) — so a
    read-only target fails *soft* (logged warning), it does not crash, but
    persistence is silently lost.
  - `dataDirectory` (null by default) controls where `app-logs/`, scan CSVs, and
    `scans.db` land; null → cwd. In a Program Files install, cwd is read-only.

## Architecture

Five units, each independently understandable and testable:

```
 CI / local build
 ┌──────────────────────────────────────────────────────────────┐
 │ PyInstaller (openwater.spec)  →  dist\OpenWaterApp\           │  (unchanged)
 │            │                                                   │
 │            ▼                                                   │
 │ installer\build_installer.ps1                                 │
 │   wix build app.wxs     → OpenWaterApp[-RUO].msi              │
 │   wix build bundle.wxs  → OpenWater-Setup-X.Y.Z[_RUO].exe ────┼─┐ chains
 │   Invoke-Sign (skippable)                                     │ │  driver MSI
 └──────────────────────────────────────────────────────────────┘ │
                                                                    ▼
 Runtime                                          Program Files\OpenWater\Bloodflow\
 ┌────────────────────────────────┐               %PROGRAMDATA%\OpenWater\
 │ app_paths.py (writable root)   │──────────────▶  app_config.local.json
 │ main.py load / connector save  │                 data\  (scans, app-logs, db)
 │ in-app updater (connector+QML) │──┐
 └────────────────────────────────┘  │ downloads + runs the same bundle
                                      ▼
                          GitHub release: OpenWater-Setup-*.exe
```

### Unit 1 — Installer sources (`installer/`)

New top-level `installer/` directory:

- **`app.wxs`** — the app MSI.
  - Installs `dist\OpenWaterApp\` (harvested at build time) into
    `Program Files\OpenWater\Bloodflow\`.
  - **`UpgradeCode`**: a single constant GUID, fixed forever, per variant
    (clinical ≠ RUO). **`ProductCode`**: regenerated each build (`*`).
  - `MajorUpgrade` with `AllowSameVersionUpgrades="yes"` (so a `-dev.N`/`-rc.N`
    build whose numeric `X.Y.Z` equals a prior one still upgrades during
    testing) and a clear "newer version already installed" downgrade message.
  - Start-menu shortcut; desktop shortcut optional.
  - `ProductName` differs per variant ("OpenWater Bloodflow" vs
    "OpenWater Bloodflow (RUO)").
- **`bundle.wxs`** — the Burn bundle.
  - Chains: `OpenMotionDriver-x64.msi` (from `resources/`) **then** the app MSI.
  - Driver `MsiPackage` gets a `DetectCondition` / uses the driver MSI's own
    `UpgradeCode` so it is skipped when already current.
  - Bundle `UpgradeCode` is also per-variant and constant.
- **`build_installer.ps1`** — orchestrates: resolve version (§Unit 4), harvest
  `dist\`, `wix build` app + bundle for the requested variant, then `Invoke-Sign`.
  Parameterized `-Variant clinical|ruo`.

WiX v5 CLI (`dotnet tool install --global wix`). File harvesting via WiX v5's
directory/file harvesting (no separate `heat` step needed in v5).

### Unit 2 — Code signing (wired, skippable)

- **`Invoke-Sign`** helper (in `build_installer.ps1` or a sibling
  `installer/sign.ps1`): signs the launcher `.exe` inside the staged app dir,
  the app `.msi`, and the bundle.
  - Bundle signing uses the **`insignia`** detach → sign engine → reattach →
    sign bundle sequence (the documented Burn signing dance), scripted once.
  - **No-op gate:** if the signing identity env var / CI secret (e.g.
    `CODESIGN_THUMBPRINT` or an Azure Trusted Signing config) is unset, the
    helper logs "signing skipped (no cert configured)" and returns success.
    Everything builds and ships unsigned today; the day the EV cert lands,
    setting the secret turns signing on with no other change.
- **CI signing note:** post-2023 CA/B rules require code-signing keys on FIPS
  hardware, so CI signing means a cloud service (Azure Trusted Signing /
  SignPath / DigiCert KeyLocker) **or** signing on a self-hosted runner with the
  token. The skippable hook is provider-agnostic; the provider is chosen when
  the cert is procured.

### Unit 3 — Writable-state relocation (app code)

New module **`app_paths.py`**:

- `writable_root() -> Path` — returns `%PROGRAMDATA%\OpenWater\` when frozen
  (PyInstaller), created on first access (with the dir created if missing); in a
  dev (non-frozen) run, falls back to the repo/cwd so local development is
  unchanged.
- Helpers: `local_config_path()` → `<root>\app_config.local.json`;
  `default_data_dir()` → `<root>\data`.

Wiring:

- **`main.py:_load_app_config()`** — load order becomes
  **defaults → bundled read-only `app_config.json` → `app_config.local.json`**
  (deep-merge the local overrides last, known keys only). The bundled file stays
  the shipped baseline; the local file holds only keys the user changed at
  runtime.
- **`motion_connector.py:_save_app_config()`** — write **only** to
  `app_paths.local_config_path()`. Write only the keys that differ from the
  shipped baseline (keep the file small and legible), creating the ProgramData
  dir if needed. Never write into Program Files.
- **`dataDirectory` default** — when null/unset, resolve to
  `app_paths.default_data_dir()` instead of cwd. `app-logs/` and `scans.db`
  follow `dataDirectory` as they do today, so they land under ProgramData.

**Hand-off to #2 (documented, not implemented here):** whether `developerMode`
should persist across restarts at all, and whether `app_config.local.json`
should be ACL-restricted or signed, is #2's hardening policy. This spec only
provides the read-only-baseline + writable-overrides *mechanism*.

### Unit 4 — Versioning

- MSI/bundle `ProductVersion` must be numeric `X.Y.Z` (and `≤ 255.255.65535`),
  so the git tag's pre-release suffix (`-dev.N` / `-rc.N`) is **dropped** for the
  installer version. `AllowSameVersionUpgrades="yes"` covers the resulting
  equal-version reinstalls during dev/rc testing.
- The **full** human version (with suffix) stays in `version.py` and the GitHub
  tag. The in-app updater compares the **GitHub tag string** via the existing
  `_version_newer`, which already understands suffixes — so "is there an update"
  is correct even though the MSI version is coarser.
- `build_installer.ps1` derives `X.Y.Z` from the same git-describe/tag logic the
  existing build already uses (`build_and_zip.ps1` / `release-build.yml`).

### Unit 5 — In-app updater

- **`checkForUpdates` / `_check_for_updates_worker`** (motion_connector.py
  ~3870): change the asset matcher from `name.endswith(".zip")` to the matching
  **`OpenWater-Setup-*.exe`** — clinical build matches the non-RUO bundle, RUO
  build matches `*_RUO.exe`. Variant is known from the baked `reducedMode` / a
  build-stamped marker so the right asset is selected. `updateAvailable` still
  carries `(latest_version, download_url)`.
- **New connector slot** (replacing the browser hop) — e.g.
  `@pyqtSlot(str) def applyUpdate(self, download_url)`:
  1. Download the bundle to `%PROGRAMDATA%\OpenWater\updates\` (or `%TEMP%`).
  2. **Verify Authenticode signature + expected publisher** (when signing is
     live; until then, log a warning and proceed — never silently trust).
  3. Spawn the bundle **detached**, then quit the app so its files unlock.
  4. Burn detects the installed app and performs an in-place major upgrade
     (one UAC prompt), then relaunches the app on completion.
  Progress/errors surfaced via existing signals (`updateCheckFailed` and/or new
  `updateProgress`/`updateFailed`).
- **`UpdateBanner.qml`** — the Update button calls `applyUpdate(...)` instead of
  `openDownloadUrl(...)`. Show a downloading/installing state.

### Unit 6 — CI (`release-build.yml`)

After the existing PyInstaller build step, add:

- Install WiX v5 (`dotnet tool install --global wix`).
- Run `installer\build_installer.ps1 -Variant clinical` and `-Variant ruo`
  (the RUO run reuses the existing reducedMode-flip step to produce the RUO
  `dist\`, then packages it).
- `Invoke-Sign` (skippable) runs inside the script.
- Publish **`OpenWater-Setup-X.Y.Z.exe`** and **`OpenWater-Setup-X.Y.Z_RUO.exe`**
  as the GitHub release assets. Keep attaching `OpenMotionDriver-x64.zip` as
  today. The legacy `.zip` app artifacts may be retired, or kept for one release
  as a transition.

## Verification plan

- **Local, clean VM (no prior install, no driver):**
  1. Build both bundles; run `OpenWater-Setup-X.Y.Z.exe` → confirm single UAC
     prompt, driver installed, app under `Program Files\OpenWater\Bloodflow\`,
     Start-menu shortcut present.
  2. Launch app → change a setting (e.g. unlock developer mode) → confirm
     `%PROGRAMDATA%\OpenWater\app_config.local.json` is written and the bundled
     Program Files config is untouched; restart → setting persists.
  3. Run a scan → confirm CSVs / `scans.db` / `app-logs/` land under
     `%PROGRAMDATA%\OpenWater\data`.
- **Update path:** bump version, publish a newer release, click **Update**
  in-app → one UAC prompt → in-place upgrade → app relaunches on the new
  version; ProgramData state preserved.
- **RUO isolation:** install the RUO bundle on a box with the clinical product
  → confirm they coexist / do not cross-upgrade (distinct ProductCodes in
  Programs & Features).
- **Signing:** once the EV cert exists, set the secret, rebuild → confirm signed
  `.exe`/`.msi`/bundle and that `applyUpdate`'s signature verification passes.

## Out of scope

- **#2 (tamper resistance):** hardening of `app_config.local.json` (ACLs /
  signing), `developerMode` persistence policy, password-constant handling.
- **#3 (cleanup):** dead-flag removal, `motion_connector.py` refactor, etc.
- **No privileged updater service** (rejected — keeps the elevated-surface area
  flat for #2). One UAC prompt per update is accepted.
- **macOS packaging** (`openwater_macos.spec`) — unchanged; this is Windows-only.
