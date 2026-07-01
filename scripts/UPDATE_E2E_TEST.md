# In-app updater — end-to-end test runbook

Exercises the full in-place update flow **entirely on one machine over
loopback** — no public hosting of the (proprietary) installer. An "old" build
detects a "newer" release from a local fake-releases server, downloads it,
verifies it, and performs the in-place upgrade + relaunch.

What it covers: release detection → variant asset selection → version compare →
download → PE/signature verification → wait-for-exit helper → `/passive`
in-place upgrade → relaunch on the new version.

## Prerequisites

- A machine with **Python 3** (to run the server) — the **dev box is easiest**
  (it also has the console for the bonus hardware test).
- To *build* the bundles: the WiX 5.0.2 + .NET 8 toolchain and a conda env with
  PyQt6 + **a clean-`next` `omotion` SDK** + PyInstaller (`pylib` here). See
  `installer/` and the build script. **Bundles whatever `omotion` resolves to —
  make sure it's clean.**
- The server + the installed app must be the **same machine** (the old build is
  pointed at `127.0.0.1:8077`; pass `-ServerUrl` to the build script to change).

## 1. Build the two test bundles

```powershell
powershell -File scripts\build_update_test_bundles.ps1
```

Produces (in `build\installer\`):

- `Openwater-Setup-1.3.0_Research.exe` — **OLD**, the build under test (its update
  check points at `http://127.0.0.1:8077/releases/latest`).
- `Openwater-Setup-1.3.1_Research.exe` — **NEW**, the upgrade target.

The script prints the SDK path it built against — confirm it's a clean `next`.

## 2. Start the fake-releases server (leave running)

```powershell
conda run -n pylib python scripts\fake_release_server.py `
    --bundle build\installer\Openwater-Setup-1.3.1_Research.exe --tag 1.3.1 --port 8077
```

It serves `releases/latest` (advertising 1.3.1) and the bundle file. Watch its
log — you'll see the app's requests, confirming nothing goes to the internet.

## 3. Install the OLD bundle + run the test

1. Double-click `Openwater-Setup-1.3.0_Research.exe` → SmartScreen "More info → Run
   anyway" + UAC. Installs to `C:\Program Files\Openwater\Open-Motion\`.
2. Launch **Open-Motion Research** from the Start menu.
3. Within ~3 s the banner appears: *"A new version is available: 1.3.1."*
   (the server log shows `GET /releases/latest`).
4. Click **Update**. Expect: button → "Downloading…" → "Installing…", the app
   **quits**, **one UAC** prompt (the bundle self-elevates), a `/passive`
   progress install, then the app **relaunches**.
5. **Verify:** the relaunched app reports **1.3.1** (Settings/About), and the
   banner does **not** reappear (1.3.1 == latest).
6. **State preserved:** settings / scan data still under `C:\ProgramData\Openwater\`.

## 4. Bonus (dev box + hardware)

Repeat with a **console connected and streaming** → confirm no FilesInUse hang
during the in-place file swap (the scenario the relaunch-helper was built for).

## Cleanup

- Uninstall via Apps & Features (single Research entry).
- Stop the server (Ctrl+C).

## Notes

- The download is allowed despite being **unsigned** because
  `_REQUIRE_SIGNED_UPDATES = False` (transition period until the EV cert lands);
  the PE-header (`MZ`) check still rejects truncated/HTML downloads.
- `updateRepo` / `updateApiUrl` config keys default to `None` → production
  GitHub repo. They exist only to point a build at a staging/local source.
