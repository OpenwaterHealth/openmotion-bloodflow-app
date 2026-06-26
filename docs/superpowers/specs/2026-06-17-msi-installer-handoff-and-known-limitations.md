# MSI Installer + In-App Updater — Hand-off & Known Limitations

**Date:** 2026-06-17
**Companion to:** [design](2026-06-17-msi-installer-in-app-update-design.md) ·
[plan](../plans/2026-06-17-msi-installer-in-app-update.md)
**Source:** adversarial audit of the implemented branch (35 findings raised, 31
confirmed). The clearly in-scope, low-risk fixes were applied on the branch
("Bucket 1"). This document records the items that are **deliberately not
fixed here** because they require a cross-workstream decision (most belong to
workstream #2, tamper-resistance) or a deployment-policy call.

---

## Decisions for workstream #2 / deployment owner

### 1. `%PROGRAMDATA%\OpenWater\` multi-user ACL (design tension — needs a call)

The writable state lives in `%PROGRAMDATA%\OpenWater\` so settings + data are
machine-wide and survive Windows user-switching on a shared clinical cart
(design §a). But **default `%PROGRAMDATA%` ACLs let a standard user create files
yet only the *creator* (and admins) can later modify them.** So if user A first
runs the app and writes `app_config.local.json`, a different non-admin user B
**cannot overwrite it** — B's settings changes silently fail (the write is
already try/except-guarded, so no crash, just lost persistence).

Making multi-user shared settings actually work requires the installer (the
elevated moment) to **create `%PROGRAMDATA%\OpenWater\` and grant `Users` modify**.
But that **directly conflicts with #2's tamper-resistance goal** — an open ACL
means any user can tamper with the config the gate persists into.

**This is a genuine fork that should not be resolved unilaterally. Options:**
- **(A) Per-user settings** — relocate the writable overrides to
  `%LOCALAPPDATA%\OpenWater\` (each Windows user gets their own). Clean ACLs, no
  cross-user write problem, but settings are *not* shared across operators on a
  cart. (Data/logs can stay machine-wide or move too.)
- **(B) Machine-wide, open ACL** — installer grants `Users` modify on
  `%PROGRAMDATA%\OpenWater\`. Shared settings work; weakest tamper posture.
- **(C) Machine-wide, locked ACL** — installer grants only admins write;
  `developerMode`/threshold changes require elevation. Strong tamper posture;
  routine settings changes need an admin.

The installer currently does **neither create nor ACL** the directory (the app
creates it on first run, inheriting default ACLs → option-B-minus, with the
multi-user-write gap). Whatever #2 decides, the `app.wxs` `CreateFolder` +
`util:PermissionEx` (WiX Util extension) is the place to implement A/B/C.

### 2. Update variant identity comes from a mutable runtime flag

`is_ruo = not reducedMode` (`motion_connector.py`), and `reducedMode` is a
persisted, runtime-toggleable config key (Settings UI). So which installer the
updater pulls is derived from mutable state, not a fixed property of the build.
In practice the risk is bounded (the updater is only active when
`reducedMode == false`, i.e. already "RUO mode"), but the robust fix is a
**build-time variant stamp** (e.g. a `_VARIANT` constant written into
`version.py` at build, or a marker file in the bundle) that the updater reads
instead of inferring from `reducedMode`. Low effort; worth doing when #2
touches the gating model.

### 3. Clinical build has no in-app updater (confirm this is intended)

By existing design (issue #96) the update banner is hidden and the auto-check is
skipped whenever `reducedMode == true` — which is the **shipped clinical
default**. So the shipped clinical product **never self-updates**; clinical
carts are expected to be updated by IT / re-imaged. The audit flagged this as
"headline feature dead in the primary product." Confirm this is the intended
deployment model (clinical = managed updates; RUO = self-update). If clinical
*should* self-update, the banner gating needs revisiting.

### 4. Single-instance mutex vs. Fast User Switching

`utils/single_instance.py` uses a `Global\` mutex, which blocks a **second
Windows user** from launching the app at all on a shared / Fast-User-Switching
clinical PC. Combined with item 1, this affects the multi-user story. If
multi-user concurrent use is a requirement, switch to a `Local\` (per-session)
mutex; if the cart is strictly single-session, leave as-is and document it.

---

## Installer-policy items to decide (lower stakes)

- **Uninstall leaves `%PROGRAMDATA%\OpenWater\`** (settings, scans, logs, `scans.db`).
  This is the conventional choice (don't delete user data on uninstall), but it
  should be an explicit decision. If clean removal is wanted, add a
  `RemoveFolderEx` (Util extension) guarded by an "also remove data" checkbox.
- **Driver MSI is `Vital="yes" Permanent="yes"`** — a driver-install failure
  aborts the whole bundle, and the driver is never removed on uninstall and
  can't be repaired by a later run. Acceptable for a shared WinUSB driver, but
  note there's no rollback path if a future driver MSI is broken.
- **`MajorUpgrade AllowSameVersionUpgrades="yes"`** with auto-GUID file
  components can raise ICE61 / same-version component-rule warnings. Harmless
  for function (verified building), but expect the warnings.
- **Burn bundle signing** (`build_installer.ps1`, dormant until a cert exists)
  does `wix burn reattach ... -o <samepath>` — reattaching to the same path it
  read. Re-verify that read/write-in-place is safe (or write to a temp then
  move) when signing is turned on.

---

## Already fixed on this branch (for reference)

From the same audit, these were applied (Bucket 1):
- In-app updater now relaunches via a detached PowerShell helper that waits for
  the app to exit, runs the bundle `/passive /norestart`, then restarts the app
  — fixing the missing relaunch, the FilesInUse race, and the interactive-BA
  surprise in one place.
- Updater rejects non-`.exe` URLs and non-PE (truncated/HTML) downloads before
  launching; no more `html_url` fallback.
- Re-entrancy guard + `updateProgress` feedback (banner shows Downloading /
  Installing, re-enables on failure).
- `updates/` download dir is cleared before each download.
- Config overrides written atomically (temp + `os.replace`).
- `build_installer.ps1` is ASCII-only (no PowerShell 5.1 mojibake).
- App MSI harvests from an absolute `SourceDir` with a hard-fail guard (a
  relative path silently produced an app-less 36 KB MSI).

## Verified on this machine (not yet on a clean VM)
- Both variants build end-to-end: `OpenWater-Setup-1.3.0.exe` and
  `..._RUO.exe`, 183 MB each, with distinct ProductName + UpgradeCode (confirmed
  they can't cross-upgrade).
- Still pending (manual, needs clean VM + hardware + the future cert): actual
  install/upgrade behavior, driver install, the relaunch flow, signing.
