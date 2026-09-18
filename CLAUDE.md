# openmotion-bloodflow-app — Claude guide

PyQt6 + QML clinical desktop app for blood flow / volume monitoring. Consumes the `omotion` package from `openmotion-sdk` (installed editable from `../openmotion-sdk`).

Cross-repo context: [../CLAUDE.md](../CLAUDE.md). SDK details: [../openmotion-sdk/CLAUDE.md](../openmotion-sdk/CLAUDE.md).

## Run / build

```powershell
# Dev setup — SDK is installed editable from the sibling repo
pip install -r requirements.txt
pip install -e ../openmotion-sdk

python main.py                          # run the app

powershell -File scripts\build_nuitka.ps1 -Variant research   # native onefile → dist\research\Open-Motion\Open-Motion.exe (#548)
python -m PyInstaller -y openwater.spec # PyInstaller fallback → dist/Open-Motion.exe (onefile, #547)
.\build_and_zip.ps1                     # build + package all 4 artifacts

# 4 artifacts: Clinical/Research × Portable/Installer
#   Open-Motion-<ver>.zip                (Clinical portable)
#   Open-Motion-Research-<ver>.zip       (Research portable)
#   build/installer/Open-Motion-Setup-<ver>.exe          (Clinical installer)
#   build/installer/Open-Motion-Research-Setup-<ver>.exe (Research installer)
# build_and_zip.ps1 runs PyInstaller once, then scripts/package_artifacts.ps1
# loops the variants. Installers are skipped with a warning if WiX isn't found;
# scripts/package_artifacts.ps1 -SkipInstaller forces portable-only.
```

- Tested on **Python 3.13.5**; `requirements.txt` pins PyQt6 6.8.0, pandas, numpy, matplotlib, pyusb, libusb1, PyInstaller 6.11.1, flake8 7.1.1.
- No `pyproject.toml`; pure `requirements.txt`.
- QML does **not** hot-reload — restart the app to pick up `.qml` changes.

### macOS (.app + DMG)

```bash
brew install libusb                  # PyUSB backend; bundled from /opt/homebrew/lib
pip install --upgrade "pyinstaller>=6.13"
./build_macos.sh                     # → dist/Open-Motion.app + dist/Open-Motion-<ver>-macOS.dmg
```

CI builds it too: the `build-macos` job in `release-build.yml` (macos-15, Apple
Silicon) attaches the DMG to the **same** GitHub Release as the Windows
artifacts, so every tagged release carries one.

- **`openwater_macos.spec` is generated, not authored.** `build_macos.sh` rewrites
  it from an inline heredoc on every run. Edit the heredoc — a change made only
  to the tracked `.spec` is silently discarded on the next build, and the tracked
  copy has drifted from the heredoc before.
- **The two specs are maintained separately and have diverged.** `openwater.spec`
  and the macOS heredoc bundle different resource sets; a resource added to one
  and not the other goes missing in that platform's build with no error at build
  time (issue #432 — the replay sample scan). Add bundled resources to **both**.
- **macOS is research-only.** `main.py` forces `clinicalMode` off on darwin,
  overriding both the bundled config and the `--clinical` dev flag — `clinicalMode`
  drives `require_encrypted_db`, and the SDK refuses the scan-db keystore on
  macOS, so a "clinical" macOS session cannot start at all.
- **`portableMode` does not apply.** Both variants write to
  `~/Library/Application Support/Openwater` (`utils/app_paths.py`); writing inside
  `Open-Motion.app` would invalidate its code signature. That is where `logs/` and
  `data/` live on a Mac.
- **PyInstaller 6.11.1 (the `requirements.txt` pin) does not work here** — it
  creates Qt framework symlinks twice and dies with `FileExistsError` on
  `Versions/Current/*`. CI overrides the pin for the macOS job only.
- **Ad-hoc signed, not notarized** (`codesign --sign -`). A *downloaded* DMG is
  quarantined by Gatekeeper: first launch needs right-click → Open, or
  `xattr -dr com.apple.quarantine /Applications/Open-Motion.app`. The `xattr -cr`
  in the build script only cleans the build machine's copy.

## Layout

| Path | What lives here |
|---|---|
| `main.py` | Entry point. PyQt app, QML engine, logging. Registers `MotionInterface` as a QML singleton. |
| `motion_connector.py` | **~4,700 lines.** Single `MotionConnector` QObject — all UI⇄hardware glue, ~64 signals / ~80 slots. State machine constants around line 252; transitions in `update_state` (~line 1947). |
| `motion_config.py` | FPGA model + laser-parameter helpers (extracted in May 2025 for reuse). |
| `app_updater.py` | The in-app self-updater (GitHub release check, download, Authenticode check, detached install handoff). **Compiled out of clinical builds** (#543): `motion_connector.py` imports it only when the compiled `CLINICAL_MODE` is False and `openwater.spec` excludes it from the clinical bundle; the connector keeps the `checkForUpdates` / `applyUpdate` slots and update signals in every variant as no-ops. A source run with `--clinical` still has the module (constant unchanged) and relies on the runtime `clinicalMode` guard. **Signature policy (#544):** a downloaded bundle installs only if `WinVerifyTrust` (direct ctypes call, no PowerShell) reports a valid chained Authenticode signature **and** the signer's SHA-256 thumbprint is in `ACCEPTED_SIGNER_SHA256` (the Openwater EV certificate; add the successor there before rotating). Unsigned bundles, including every dev/rc pre-release under the current signing quota policy, are refused with a message pointing at the releases page. |
| `pages/BloodFlow.qml` | Main scan page: patient info, sensor config, trigger. The only page `main.qml` loads. |
| `components/PlotViewer.qml` | Real-time + replay BFI/BVI plot viewer (pan/zoom DVR, autoscale). |
| `components/SettingsModal.qml` | Settings overlay (opened from BloodFlow — there is no `pages/Settings.qml`). |
| `pages/scan/` | `ScanRunner.qml` plus task QMLs: `CaptureDataTask`, `ContactQualityCheckTask`, `FlashSensorsTask`, `PostProcessTask`, `SetTriggerTask`. Newer orchestration suite. |
| `components/` | 25 reusable QML components — `SettingsModal`, `ContactQualityModal`, `CameraDot`, `TestResultsWindow`, etc. |
| `processing/visualize_bloodflow.py` | BFI/BVI computation from CSV histograms. |
| `config/app_config.py` | **Compiled** app config (#546): the ~80 flags / thresholds the JSON used to carry, same keys and order, each in one of four tiers (`CONSTANT_KEYS` / `SESSION_KEYS` / `PREFERENCE_KEYS` / `STATE_KEYS`). `CLINICAL_MODE` at the top is stamped per variant by the build. `config/tec_params.py` holds the TEC DAC setpoints. There is no JSON config any more, shipped or writable. |
| `utils/settings_store.py` | The `settings` table in `scans.db` — where PREFERENCE/STATE keys persist (SQLCipher-encrypted + HMAC on clinical builds). Replaced `app_config.local.json`; a leftover one from an older install is ignored (nothing reads it), so preferences on that machine start from the compiled defaults. |
| `config/laser_params.json` | 18 laser I2C register sets (TA / SEED / EE / OPT variants). **Not user-tunable calibration data** — init/baseline commands for the laser driver chips. |
| `resources/sample_scan.csv` | Real exported scan (History → Export CSV / SDK `materialize_corrected_csv` per-cam wide format). Offered — never auto-loaded — into the replay viewer when a **research build** boots with **no device connected** (#314): the startup connection watchdog raises `components/SampleScanOfferModal.qml`, and only accepting it binds the sample. Clinical builds never see the offer. Swap this one file to change the sample dataset. Bundled by `openwater.spec` (targeted `datas` entry); located at runtime via `utils.resource_path.resource_path("resources", "sample_scan.csv")`. Parsed DB-free by `data_sources.load_csv_scan_buffers` → `PastScanSource(preloaded_buffers=…, scan_db=None)`; the user's real `scans.db` is never touched. |
| `openwater.spec` | PyInstaller spec, **onefile** since #547: the build output is the single `Open-Motion.exe` with every bundled byte (bytecode archive, Qt/native DLLs, QML, assets, sample scan) appended to it, so the Authenticode signature covers all of it. Custom logic mirrors the vendored libusb DLLs to `_vendor\` at the bundle root so `rthook_libusb_paths.py` finds them in the extraction directory. The spec refuses a dist directory that still holds a onedir `_internal\` tree. See "Packaging" below. |
| `utils/frozen.py` | The one answer to "built executable?" and "which exe / where are resources?" for PyInstaller **and** Nuitka (#548). Nothing else may read `sys.frozen`, `sys._MEIPASS` or `sys.executable`. |
| `scripts/build_nuitka.ps1` | Opt-in native compile (#548); same output path as the PyInstaller build. See "Native compile with Nuitka". |
| `sdk-version.txt` | The one place the release SDK is pinned (#545): rc/prod CI builds install exactly this `openmotion-sdk` release and fail if the installed version differs; the per-release SBOM asserts the same line. Dev builds and the editable local setup ignore it. |
| `tests/` | Hardware-in-loop pytest suite, ~23 files. Markers: `@pytest.mark.dev` (~1–2 min, runs on every push to `next`), `@pytest.mark.release` (~6–8 min, runs on release tags). |

**Note:** the old `motion_singleton.py` no longer exists — connector logic was consolidated into `motion_connector.py` and registered as a QML singleton in `main.py`.

## State machine (motion_connector.py:252)

```
DISCONNECTED (0) → SENSOR_CONNECTED (1) → CONSOLE_CONNECTED (2) → READY (3) → RUNNING (4)
```

No FSM class — integer enum + conditional branches on `self._state`. Transitions in `update_state` (`motion_connector.py`, ~line 1947).

QML↔Python wiring: `main.py:256` registers the connector as a QML singleton (`qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", connector)`). QML calls `MotionInterface.slotName()`; Python emits signals QML connects to with `onSignalNameChanged`.

## Working without hardware

There is currently **no working no-hardware mock mode** for the running app:

- `cameraFakeData` was removed with the config conversion (#546). It never enabled a hardware-free launch anyway: it only set the `DEBUG_FLAG_FAKE_DATA` bit on an already-connected sensor, and the firmware-side synthetic histograms no longer work.
- The SDK's `demo_mode` is also broken (confirmed 2026-05-28).

To exercise app logic without hardware, write unit tests that mock the hardware seams instead — see the tests marked `@pytest.mark.unit` (e.g. `tests/test_live_plot_sink.py`, `tests/test_scan_notes_db.py`). The conftest autouse fixtures short-circuit on that marker, so no app launch or UI machinery fires.

**Boot with no device → sample scan offer (#314):** nothing auto-loads in any build variant anymore. Instead, the existing startup connection watchdog (`connectionTimeoutSec`, default `12` s — same one-shot timer that raises the E-104/E-106 warning toast) also calls `_maybe_offer_sample_scan()`. In **research (non-clinical) builds only**, when no device at all is connected (`_any_device_connected()`, not merely a missing console) and nothing is already bound to the viewer, the connector emits `sampleScanOfferRequested`; `components/SampleScanOfferModal.qml` shows a "No console detected — would you like to open a sample dataset?" dialog. Only clicking "Open sample dataset" calls the `loadSampleScan()` slot, which binds `resources/sample_scan.csv` to the plot viewer as a DB-free `PastScanSource` so you can pan/zoom/scrub real BFI/BVI traces. Clinical builds get neither the auto-load nor the dialog — a clinical user is never shown fabricated traces, not even behind a prompt. Declining (or just not answering) is final for that launch; relaunch to be offered again. This is NOT a mock mode (no live capture, no CQ, no scan flow); it just gives the replay/DVR machinery real data to explore. Starting a real scan later replaces the sample source; a missing/corrupt CSV fails soft (logged, viewer stays empty). Gating + parsing are unit-tested in `tests/test_sample_scan_replay.py`.

Debug flags that are still useful when hardware **is** attached (`config/app_config.py`; the ones without a Settings toggle are compiled constants, so for a source run pass them with `--config-override '{"commVerbose": true}'`):

- `engineeringMode: true` — show per-camera CQ dots, test buttons, debug telemetry.
- `commVerbose: true` + `verboseCommandHandling: true` — SDK logs all UART packets + MCU printf output.

## Environment variables — the app reads none

A packaged artifact must start identically no matter what env vars the host
machine carries, so **no app code reads `os.environ`** (enforced by
`tests/test_app_paths.py` / `tests/test_main_config_wiring.py`; grep
`os.environ|getenv` outside `tests/` before adding one). What replaced the
old knobs:

| Old env var | Now |
|---|---|
| `OPENMOTION_CLINICAL=1/0` | `python main.py --clinical` / `--research` (source runs only) |
| `OPENMOTION_PORTABLE=1` | `python main.py --portable` (source runs only) |
| `OPENWATER_DATA_ROOT` | `python main.py --data-root <dir>` (source runs only); tests set `app_paths.DATA_ROOT_OVERRIDE` (conftest's autouse `_isolate_writable_root` already routes every `unit` test at tmp_path) |
| `OPENWATER_CONFIG_DIR` | gone — there is no config file to redirect (#546); unit tests `monkeypatch.setitem(config.app_config.APP_CONFIG, key, value)` (pattern in `tests/test_main_config_wiring.py`) |
| `%PROGRAMDATA%`, `%USERPROFILE%` / `$HOME` | `utils/app_paths.py` asks the Windows shell (`SHGetKnownFolderPath`) / the passwd db instead |

`main._parse_dev_args` strips the dev flags from argv before Qt sees it and
**drops them entirely in a frozen build** (logged at WARNING) — an
installed Research exe cannot be flipped to Clinical from a shortcut. The
Zed tasks in `../.zed/tasks.json` pass the flags, and the startup report
(#527) marks the keys they forced as `[dev-flag]`. `main._pin_qt_environment`
runs before the first `QApplication`: it removes the Qt runtime knobs a host
could inject (`QT_QPA_PLATFORM`, `QT_SCALE_FACTOR`, `QT_QUICK_CONTROLS_CONF`,
`QSG_RHI_BACKEND`, … — the list is `_QT_ENV_SCRUB`) and pins the three the
app relies on (Material style, Dark theme, font-logging rule). It leaves
`QT_PLUGIN_PATH` / `QML2_IMPORT_PATH` / `PATH` alone: PyInstaller's PyQt6
runtime hook sets those to the bundle's own Qt tree and the frozen build
needs them. Need the offscreen platform for a headless run? Pass
`-platform offscreen` on the command line — that is an explicit argument,
not ambient state, and passes through untouched.

The HIL harness's own env vars (`OPENWATER_EXE`, `OPENWATER_FROM_SOURCE`,
`OPENWATER_APP_CONFIG`, `OPENWATER_BANNER`, `TESTER_NAME`, Shelly `OW_*`)
configure pytest, not the app — they never reach app code.

## Config tiers (`config/app_config.py`, #546)

The shipped configuration is a Python module compiled into the executable; no
JSON ships and nothing writable holds config any more. Every key is in exactly
one tier (checked at import):

| Tier | Where the value lives | Runtime write | Keys |
|---|---|---|---|
| `CONSTANT` | compiled in | **refused** + `config_write_refused` audit event | build variant, masks, every threshold/duration, firmware/debug flags with no UI, updater source, SMTP, `dataDirectory` |
| `SESSION` | in-process only, reset at every launch | allowed, never persisted | `engineeringMode`, `forceLaserFail`, `debugHistoStallTest`, `histoCmp`, sensor/console debug logging, alt camera/laser settings, `writeTelemetryCsv`, `downloadBetaUpdates`, `showProfiling` |
| `PREFERENCE` | `settings` table in `scans.db` | allowed, persisted as a diff vs compiled | plot bounds/colors/window, masks, theme, `showAxisLabels`, `bviLowPassEnabled`, `writeRawCsv`, `rawCsvDurationSec` |
| `STATE` | `settings` table | internal | the three `alt*Dirty` restore flags |

Why session-only for the engineering/debug flags: the engineering unlock is per
session, so nothing it enables may outlive the session that authorized it.
`portableMode` is **derived**, not configured: a frozen Windows build is
"installed" when its exe lives in the directory the MSI registered at
`HKLM\Software\Openwater\Open-Motion\InstallDir`, else "portable" — so the
portable zip and the installer share one byte-identical exe per variant.

**Build variant is a compile-time stamp.** `scripts/build_common.ps1
Set-BuildVariant` rewrites `CLINICAL_MODE = True|False` before PyInstaller
runs, one build per variant into `dist\<variant>\Open-Motion`
(`Invoke-VariantBuild`); CI does the same with `sed`. The repo value is
`False` (Research) and must never be committed flipped. **Source runs**
override anything with dev flags: `--clinical` / `--research`, `--portable`,
`--data-root <dir>`, and `--config-override '{"key": value, …}'` (any
compiled key, constants included). A frozen build drops all of them.

**Adding a key:** add it to `APP_CONFIG` in the JSON-like order *and* to one
tier set, or the module refuses to import. A key the code reads but nobody
declares is a `KeyError` at startup, not a silent default, which is the
point.

**HIL tests** force values with a module-level `FORCE_APP_CONFIG = {...}`;
conftest passes the merged dict to a from-source launch as
`--config-override`. A packaged exe cannot take forced config (by design):
run such modules with `--from-source` or against a build of the matching
variant. UI-driven persistence is asserted through
`hil_helpers.read_local_config_value`, which reads the settings table.

## Notable config flags (`config/app_config.py`)

| Flag | Default | Purpose |
|---|---|---|
| `engineeringMode` | `false` | Engineering UI + diagnostics: debug telemetry, per-camera CQ dots, test buttons, Force Dismiss in the CQ modal footer, firmware-update banner, profiling HUD. Also gates the per-scan telemetry CSV (issue #43 — clinical users must not get it; additionally opt-in via `writeTelemetryCsv` since #471; raw CSVs are gated `!clinicalMode \|\| engineeringMode` since #234). Unlockable at runtime via `EngineeringUnlockModal`. |
| `clinicalMode` | `CLINICAL_MODE = False` (repo = Research) | Clinical build variant: hide settings, large BFI/BVI panels; window title "Open-Motion" vs "Open-Motion Research". A **compile-time stamp** (#546): `scripts/build_common.ps1 Set-BuildVariant` rewrites the constant before PyInstaller, one build per variant. For a **source run only**, `python main.py --clinical` / `--research` overrides it (a frozen build ignores the flags, and no env var is ever read). Tier CONSTANT: a runtime write is refused. **On macOS it is forced `false`** and beats even the dev flag (#430). |
| `portableMode` | derived | **Derived at launch, not configured** (#546): `true` keeps all writable state (logs, scan data/db with the settings table) next to the exe; `false` scatters it to `%PROGRAMDATA%\Openwater`. A frozen Windows build is installed when its exe lives in the MSI-registered `HKLM\...\Open-Motion\InstallDir` (`installer/app.wxs`), portable otherwise — `utils/app_paths.py:portable_mode`. The zip and the installer therefore share one exe per variant. `python main.py --portable` only labels a source run. **Ignored on macOS** (#428). |
| `altCameraSettingsEnabled` | `false` | Engineering (#446): "Enable Alternative camera settings?" in Settings → Engineering → Camera settings. While on, `altCameraExposureUs` + `altCameraGains` are written via the SDK I2C passthrough to every scanned camera just before **each** scan start (`startCapture`, before `start_scan`) and **each** preflight signal-quality check (`runContactQualityCheck`) — one shared path, `_sync_alt_scan_settings` (#510: the preflight must evaluate contact under the registers the scan it gates will use; "sync" = apply-or-restore-or-no-op, so off-and-clean does zero hardware I/O). Exposure must be a whole 9 µs row (Tline = HTS 432 px / 48 MHz; valid 99–2196 µs). The UI dropdown offers a ~100 µs-spaced usability subset (each target snapped to the nearest row) plus the 648 µs default; a hand-edited config may hold any valid row multiple — the connector validates against the full row grid. Gains are 8 per-position analog gains (1/2/4/8/16, applied to both modules; digital gain untouched), laid out as a horizontal serpentine (top row 1→4, bottom row 8→5) matching the CQ modal's sensor diagram. Defaults mirror the sensor firmware config (648 µs = 72 rows; 16/4/2/1/1/2/4/16). Camera registers persist until camera power-off, so turning the toggle **off** restores fw defaults at the next scan start via the internal `altCameraSettingsDirty` flag. Caveat: the SDK pipeline's shot-noise correction still assumes the stock `CAMERA_GAIN_MAP`, so BFI/BVI corrections are approximate under alternative gains. Helpers in `motion_config.py`; unit tests in `tests/test_alt_camera_settings.py`. |
| `altLaserPulseWidthEnabled` | `false` | Engineering, **experiments only** (#449): separate toggle in the same "Camera & laser settings" area as #446 (deliberately not the camera toggle — camera experiments must not silently change laser emission). While on, `altLaserPulseWidthUsec` (whole µs, 20–2200; UI offers a 20 µs short-pulse entry plus 100 µs steps, default 500) is applied at every scan start **and every preflight signal-quality check** (the shared `_sync_alt_scan_settings` path, #510 — before the fix a post-restart preflight ran at the connect-time ~500 µs baseline, so a 20 µs pulse passed preflight then failed live CQ) by writing the **TA driver FPGA's `pulse_width` register** (I2C 0x41, mux 1 ch 4, offset 0, 24-bit LE, 0.32 µs/tick — `motion_config.ta_pulse_width_write`), which is what actually times the optical pulse: the TA **edge-detects** the console's laser trigger, so the trigger config's `LaserPulseWidthUsec` alone changes nothing (bench-proven 2026-08-10, 500/700/1100 µs gates → identical means). The `setTrigger` override still mirrors the width into the trigger config so scan records show it. Both writes log at WARNING. Restore: TA register persists until console power-off/reconnect (apply_laser_power re-applies `laser_params.json` [27,6,0] = 1563 ticks ≈ 500.2 µs, measured ~494 µs optical), so disabling restores the baseline once via persisted `altLaserPulseWidthDirty`. Ignored on a plain clinical build. **The register is not the emitted width:** `driver_control.v` drops the drive at `pulse_count > pulse_width - 55`, so emission runs ~54 ticks (~17.3 µs) short — 3% at 500 µs, but the 20 µs entry (62 ticks) is a **~3 µs** optical pulse. Below 55 ticks (17.6 µs) that 24-bit compare underflows and nothing else clears `pulse` — the TA drive would stay latched ON — hence the hard 20 µs floor (`LASER_PULSE_WIDTH_MIN_US`, re-clamped at the write via `TA_PULSE_MIN_TICKS`); do not lower it without re-reading the Verilog. The safety FPGAs' `PULSE_WIDTH_UL` trips and **latches** (E-202) above the unit's ceiling — 1000 µs stock, ~TA×1.1 ≈ 550 µs on a WI-15-calibrated console — so while the toggle is on the connector **raises both Safety EE/OPT ceilings to 2200 µs** (`ALT_PULSE_WIDTH_SAFETY_CEILING_US` = the dropdown max; `motion_config.safety_pulse_width_ul_writes`, I2C 0x41 mux 1 ch 6/7 offset 0x04, 32-bit LE, 0.32 µs/tick) at every scan start / preflight check *before* the TA write, and on the first run after it is turned off restores them *after* the TA restore — to the console's flash user-config value when it has one, else the stock 1000 µs — via persisted `altLaserSafetyCeilingDirty` (#483, `_apply_alt_laser_pulse_width_registers`). A failed raise skips the TA write for that scan (a wider pulse under an unmoved ceiling would latch the console); nothing is written while `forceLaserFail` is armed (it would neutralize that test, the sdk#252 class). Nobody edits the laser-safety config by hand for this any more. Seed runs CW so TA width alone bounds emission; keep camera exposure ≥ delay (100 µs) + width. **Dark-frame skip is pinned to 1800 µs app-wide** (`motion_config.DEFAULT_TRIGGER_OVERRIDES` → `MotionInterface(default_trigger_config=…)` in `main.py` — every build, clinical included; not gated on any toggle). A scheduled dark displaces the pulse to start at `LaserPulseDelayUsec + LaserPulseSkipDelayUsec` = 100+1800 = 1900 µs, so an exposure ≳ 1800 µs re-catches the displaced pulse and contaminates the dark reference (bench 2026-08-10: terminal laser-off dark clean 128 DN, scheduled darks 143-185 DN ∝ camera brightness; onset between 1700 and 1800 µs exposure). **It was briefly 2400 and was reverted on 2026-08-17** (`DARK_SKIP_HIGH_EXPOSURE_US` keeps the value): 2400 clears the whole exposure dropdown, but it cuts the post-dark inter-pulse gap to `25000 − 2400 = 22600 µs` at 40 Hz, which undercuts the `EE/OPT_RATE_LL` floor stock consoles ship with — the safety FPGAs then latch `RATE_LOWER_LIMIT_FAIL` until a power-cycle (app surfaces it as **E-202**), i.e. no laser at all on any un-provisioned unit. Buying clean darks for engineering-only high-exposure runs at the cost of breaking every scan on every console is the wrong trade. 1800 leaves a 23200 µs gap, clearing both the stock 22500 and the 23125 observed on real hardware (`DARK_RATE_LOWER_LIMIT_US` = max tolerated RATE_LL). **To run a >1700 µs exposure experiment, do both in order:** (1) lower the console's `EE/OPT_RATE_LL` to ≤ 22600 µs (raw ≤ 70625, recipe in `HANDOFF-laser-safety-ceiling-override.md`) and power-cycle to clear any latch; (2) set `DARK_SKIP_DELAY_US = DARK_SKIP_HIGH_EXPOSURE_US`. Step 2 without step 1 latches on the first dark frame. The Settings exposure dropdown shows a yellow warning whenever the selected exposure exceeds `DARK_SKIP_CLEAN_EXPOSURE_MAX_US` (1700). Wherever the pin lands it MUST ride the interface default, never a connector-side patch after resolve: `ScanWorkflow` re-sends the interface-resolved trigger config immediately before `start_trigger` (fsync-counter reset), silently reverting anything patched in later — the original alt-gated `setTrigger` pin shipped exactly that bug. |
| `forceLaserFail` | `false` | Debug: simulate a laser safety trip. |
| `cameraFakeData` | `false` | **Broken — do not use.** Was meant to request firmware fake histograms; see "Working without hardware". |
| `histoThrottle` | `false` | Drop histograms to reduce log spam. |
| `histoCmp` | `true` | Histogram compression (firmware `DEBUG_FLAG_HISTO_CMP`, bit `0x40` — firmware logs it as "histo compress"). Toggle live from Settings → Engineering → "Histogram compression". |
| `deferHistoSend` | `true` | Defer the per-frame histogram send out of the FSIN ISR into the firmware main loop (firmware `DEBUG_FLAG_SEND_DEFER`, bit `0x80`; sensor-fw#68). Config-only (no Settings UI); pushed to connected sensors at connect via `_compute_sensor_debug_flags`. Requires the PR-branch sensor firmware — stock firmware ignores bit 7. |
| `debugHistoStallTest` | `false` | QA/bench stall repro (firmware `DEBUG_FLAG_HISTO_STALL`, bit `0x100`; sensor-fw#75): sensors stop sending histogram frames ~45 s into a scan while USB stays alive — the deterministic #248/#174 repro. Toggle live from Settings → Engineering → "Histogram stall test" (#525); toggling re-pushes the debug-flag bitmask to every connected sensor immediately via `setSensorDebugFlag`. |
| `scanDataStallTimeoutSec` | `15` | Whole-scan data-stall watchdog (#248): abort the scan with the E-303 critical modal when NO camera delivers a frame for this long while the trigger is ON. `<= 0` disables. Per-camera dropouts (`cameraDropoutThresholdSec`, default 2 s, code-only key) stay fail-soft — only total loss aborts. |
| `tecTripTempC` | `40` | Console over-temp trip (°C) pushed to the console user config on connect via `motion_config.ensure_tec_trip` (read-modify-write, preserves calibration + OPT/EE keys). Validated to 1–60 °C; absent/invalid values leave the device's existing trip untouched (never writes `0`, which would disable the firmware trip). |
| `ft_min_mean_per_camera` | `[40,80,…,80,40]` | Calibration pass threshold — min pixel mean per camera (8-element array). Cameras 1 and 8 (far) get a lower bar than the middle six. |
| `calibration_scan_duration_sec` | `15` | Calibration runtime. |
| `test_scan_duration_sec` | `5` | "Test" scan runtime (feature #132). |
| `cq_dark_threshold_per_camera` | `[3.0,…]` | Contact-quality dark threshold. |
| `cq_live_activate_frames` / `cq_live_clear_frames` | `10` / `80` | Live **mid-scan** contact-quality debounce (#364), **asymmetric**: the two edges are debounced independently. RAISE (OK→poor) latches after `cq_live_activate_frames` consecutive bad light-frame evaluations — FAST (10 ≈ 0.25 s at ~40 Hz), because a late warning is a safety miss. CLEAR (poor→OK) latches after `cq_live_clear_frames` consecutive good ones — SLOW / conservative (80 ≈ 2 s), because a premature dismiss strands the operator on a still-bad camera. Both count consecutive light-frame observations at the ~40 Hz capture rate. Config-only, no Settings UI (the old single `cq_live_debounce_frames` key is retired). The dark/ambient path is deliberately undebounced — scheduled darks are ~15 s apart, which is its own debounce. Evaluation lives in the SDK (`omotion/contact_quality.py` → `ContactQualityMonitor`, `CameraLatch`), attached as a scan sink by `startCapture`; the connector is a pure signal adapter. Shares `cq_dark_threshold_per_camera` / `cq_light_threshold_per_camera` / `cq_rolling_avg_window` (shipped `5`) with the pre-scan check, so preflight and live can't disagree. A fully unlit camera (decoupled fiber, covered sensor) is detected via the pipeline's `low_light_rt` flag rather than a threshold: `DarkCorrectionStage` leaves `mean_dc_rt` NaN for those frames, so the monitor treats a NaN reading as poor contact when `low_light_rt` is set and skips it otherwise. Without that split the detection window inverts — partial degradation warns while *total* signal loss stays silent. (#364 itself was silent for a simpler reason: nothing emitted at all.) |
| `bfiClampLow` / `bfiClampHigh` | `0.0` / `10.0` | Display clamps (values outside show `--`). |
| `autoScalePerPlot` | `false` | Per-plot autoscale (#452): with `autoScale` on, each `PlotCell` fits its own camera's trace (`ScanDataSource.compute_bounds_for_cell`) instead of every cell sharing one 2/98-percentile range per metric. Switch is in the plot's bottom-right ⋯ popup, disclosed under Autoscale, **research-only**; `PlotViewer.qml` reads the key straight from `appConfig` (like `showAxisLabels`) and forces it off in clinical. `SettingsModal` no longer mirrors it. |
| `bviLowPassCutoffHz` | `20.0` | 1-pole IIR low-pass on the **displayed** BVI stream only (live PlotViewer traces + clinical side averages, applied at `LiveScanSource` ingest); `scans.db`, CSVs, DB-tail history, and replay stay raw. The cutoff is a compiled constant (tier CONSTANT); the **research-only** Settings → Realtime Plot Display "BVI low-pass filter" switch (`bviLowPassEnabled`, PREFERENCE, default `true`, #552) gates it. The connector resolves `enabled ? cutoff : off` at scan start and pushes a toggle to the running `LiveScanSource` (`set_bvi_lpf_cutoff_hz`, state re-seeds), so it applies mid-scan; a clinical build ignores the flag and always filters. Number contract: missing/invalid → 20, `<= 0` disables. alpha = dt/(RC+dt) at nominal 40 Hz (≈ 0.76 at 20 Hz). |
| `writeRawCsv` | `false` | Opt-in raw histogram CSVs (`{scan_id}_(left\|right)_mask*_raw.csv`). Settings → Data Output toggle, shown when `!clinicalMode \|\| engineeringMode` (#234 — research users get raw CSVs); the same flag gate is re-checked at scan start, so a plain clinical build never writes raw output even if the toggle was left on (#43). `rawCsvDurationSec` caps seconds written (`null` = whole scan). |
| `writeCorrectedCsv` | `false` | Opt-in corrected per-cam CSV (`{scan_id}.csv`) — redundant now that per-cam BFI/BVI lands in `scans.db`. Config-only, no Settings UI. |
| `writeTelemetryCsv` | `false` | Opt-in per-scan telemetry CSV (`{scan_id}_{subject}_telemetry.csv`) via Settings → Engineering → "Save telemetry CSV" (#471, immediate-apply). Still hard-gated on `engineeringMode` (#43): the scan-start gate is `engineeringMode && writeTelemetryCsv`, re-checked at `startCapture`, so a stale toggle never writes on a plain clinical build. |
| `dataDirectory` | `None` | Single output root — `logs/` and `data/` (`scans.db`, calibrations) live under it. `None` = `app_paths.writable_root()`: cwd for dev runs; when frozen, exe-adjacent or `%PROGRAMDATA%\Openwater` per the derived `portableMode` on Windows, `~/Library/Application Support/Openwater` on macOS. Tier CONSTANT since #546 (the settings table that would persist a choice lives under it): the Settings "Output Folder" field is read-only; use `--data-root <dir>` on a source run. |

## Reading the app log

Every launch writes a timestamped log to `<dataDirectory>/logs/open-motion-<YYYYMMDD_HHMMSS>.log`. Use it as the first stop when diagnosing scan / calibration / connect failures — it captures every SDK + connector log line, including pipeline-stage exceptions that are caught and silently swallowed by `ScanRunner._safe_consume`.

```powershell
# Dev runs (dataDirectory null) log to <repo>/logs/. If dataDirectory is set
# in config/app_config.py, substitute <dataDirectory>\logs\ below.

# Latest log, full contents:
Get-ChildItem C:\Users\ethan\Projects\openmotion-bloodflow-app\logs\open-motion-*.log |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1 | Get-Content

# Filter the latest log for failure-shaped lines (most common starting point):
Get-ChildItem C:\Users\ethan\Projects\openmotion-bloodflow-app\logs\open-motion-*.log |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1 |
  Get-Content | Select-String -Pattern "WARNING|ERROR|raised|exception|Traceback|FAIL|aborted" -Context 0,3

# Just the calibration outcome:
Get-ChildItem C:\Users\ethan\Projects\openmotion-bloodflow-app\logs\open-motion-*.log |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1 |
  Get-Content | Select-String -Pattern "Calibration phase|procedure complete|samples captured"
```

The `dataDirectory` config key controls the root (defaults to cwd if unset). When unset on a frozen build, the default is `~/Library/Application Support/Openwater` on macOS, and on Windows follows `portableMode`: next to the exe (portable zip) or `%PROGRAMDATA%\Openwater` (installer). Any of those falling through to an unwritable location lands on `~/Documents/Open-Motion` as a last resort. Two fixed children live under that root:
- `logs/` — app log files (one per launch)
- `data/` — everything else. **Scan output is DB-only by default**: everything lands in `scans.db` (per-cam BFI/BVI, sessions, notes in `sessions.session_notes`); no per-scan CSVs are written unless opted in. User-facing CSVs are export-time artifacts: History → Export CSV (`exportScanCsv`). The opt-in per-scan CSVs land directly in `data/`: telemetry CSV (`{scan_id}_{subject}_telemetry.csv`, gated on `engineeringMode && writeTelemetryCsv` — issues #43/#471), raw histogram CSVs (`(!clinicalMode \|\| engineeringMode) && writeRawCsv`, #234), corrected per-cam CSV (`writeCorrectedCsv`). `data/calibrations/` holds saved calibration JSONs plus the SDK's per-camera PASS/FAIL CSVs (`calibration-<ts>.csv` / `test-<ts>.csv`); `data/debug-bundles/` holds "Send Debug Logs" zips; `data/updates/` holds in-app-updater downloads. `*_notes.txt` files are legacy read-only fallbacks. (`data/ft-test-csvs/` was a legacy per-scan factory-test export — dead since May 2026 and retired; the Test/Calibrate flows' CSVs are its superset.)

**Important:** the runner is fail-soft. `ScanRunner._safe_consume` catches sink exceptions and logs them as `sink %r raised on channel ...` at ERROR; `pipeline.process` exceptions log as `pipeline.process raised — resetting and continuing` at ERROR. **Neither aborts the scan**, so the app may report "complete" while every interval was actually broken. Always grep for `raised|exception` even on apparent successes when something downstream looks wrong.

## Packaging (onefile, #547)

The Windows build is a **onefile** exe (Nuitka by default, PyInstaller as the
fallback; both land at) `dist\<variant>\Open-Motion\Open-Motion.exe`
and nothing else (the `Open-Motion\` folder is kept so the portable zip and the
MSI harvest keep their shape and a portable user's `logs\` / `data\` land beside
the exe). Why: every shipped byte sits under the exe's Authenticode signature,
so tampering with any bundled file is detectable by verifying that one file
(tracker V-04, V-05, R-13). What it does **not** do, stated for the security
review: at each launch the bootloader extracts the payload into a per-process,
randomly named `%TEMP%\_MEIxxxxxx` directory and deletes it on exit, and
nothing verifies those extracted copies before they are loaded. A process that
can write that directory in the interval between extraction and load is outside
what signing defends against; every self-extracting bundler shares that window,
Nuitka's onefile included (#548). Startup therefore pays the extraction
(~120 MB) every launch: a few seconds on an SSD, longer on a slow disk or under
aggressive antivirus scanning. macOS stays a onedir `.app` (research-only,
ad-hoc signed).

### Native compile with Nuitka (#548, the default since 2026-09-17)

`scripts/build_nuitka.ps1 -Variant <clinical|research>` compiles the app to
C with Nuitka (`--standalone --onefile --deployment`, PyQt6 plugin) and lands
the exe at the **same** path as the PyInstaller build
(`dist\<variant>\Open-Motion\Open-Motion.exe`), so `package_artifacts.ps1
-Compiler nuitka` zips and installs it unchanged. CI: `workflow_dispatch` with
`compiler=pyinstaller` selects the PyInstaller fallback; every push and tag
build compiles with Nuitka (MSVC on `windows-latest`, its C-compile cache
restored between runs; locally `pip install -r requirements.txt` plus
`pip install ziglang` for the compiler). `package_artifacts.ps1` and
`build_and_zip.ps1` default to Nuitka too. The clinical build passes
`--nofollow-import-to=app_updater` (Nuitka follows the conditional import
statically; the spec's `excludes=` only covered PyInstaller). macOS stays on
PyInstaller. Nuitka became the default before any Nuitka build had been
launched on the HIL rig (Ethan, 2026-09-17); the first dev tag after that is
that launch.

What Nuitka buys: no `.pyc` archive to decompile (tracker M-09 / M-14).
What it does not change: onefile still extracts to a per-process
`%TEMP%\onefile_<pid>_<time>_<random>` directory at every launch (never pass
`--onefile-tempdir-spec` with a static location), so the residual risk above
is identical. Costs: the compile takes tens of minutes per variant, the
first signed release should be submitted to Microsoft's false-positive portal
(antivirus dislikes fresh onefile bootstraps), and the runner's VC++ runtime
DLLs must be bundled (MSVC finds them; zig does not, so a local zig build
relies on the target's installed runtime).

**Runtime facts that differ from PyInstaller**, all absorbed by
`utils/frozen.py` (use it, never `sys.frozen` / `sys._MEIPASS` /
`sys.executable` directly; `tests/test_frozen.py` greps for offenders):

- Nuitka sets **no** `sys.frozen`; every compiled module has `__compiled__`.
  `is_frozen()` checks both. This matters: `main._parse_dev_args` drops the
  `--clinical` / `--data-root` flags only when frozen.
- In onefile, `sys.executable` is the *extracted* `python.exe` in the temp
  directory; the launched exe is `__compiled__.original_argv0` →
  `executable_path()`. Used by the installed-vs-portable check, the portable
  data root and the updater's relaunch.
- No `_MEIPASS`: data files sit next to the modules, i.e. the parent of
  `utils/` exactly like a source tree → `bundle_dir()`.
- No runtime hooks: `main.py` calls `utils.libusb_paths.register_vendored_libusb`
  before importing the SDK; the PyInstaller `rthook_libusb_paths.py` is now
  a shim over the same function.
- **`@pyqtSlot` methods are not compiled** (#566): Nuitka's pyqt6 plugin
  lists `pyqtSlot` as an "uncompiled decorator", so every slot is shipped as
  its unparsed source and re-created at import time by `exec` into the class
  namespace (tracebacks show `File "<string>"`, lines counted from the
  decorator). Such a function has no `__class__` cell: a zero-argument
  `super()` inside it raises `RuntimeError: super(): __class__ cell not
  found` on first call — that was the first Nuitka build's crash on Scan.
  Write `super(ClassName, self)` in slots; `tests/test_nuitka_slot_compat.py`
  fails on the zero-argument form anywhere in the app tree. Source runs and
  PyInstaller never hit this, so the guard test is the only early warning.

Consequences to remember:

- `sys._MEIPASS` is the extraction directory and the payload is **flat** there
  (no `_internal\`); `utils/resource_path.py` and `rthook_libusb_paths.py` probe
  both layouts.
- Never build into a dist directory that still holds an older onedir tree:
  `Invoke-VariantBuild` wipes it, the spec and `build_installer.ps1` refuse it.
- To inventory what ships, list the exe's embedded archive rather than a folder:
  `PyInstaller.archive.readers.CArchiveReader(exe).toc` (typecodes `b` binary,
  `x` data, `z` the PYZ, `s` scripts) and open the `z` entry with
  `ZlibArchiveReader` for the module list.

## Gotchas

- **`motion_connector.py` is ~4,700 lines** — the file is doing too much. Don't add to it without considering extraction; recent precedent is `motion_config.py` (May 2025).
- **Cross-thread signals:** 135+ signals; several (e.g. `_calibrationCompleteSignal`, `safetyTripDuringCaptureRequested`) fire from USB I/O / scanner worker threads. Use `Qt.QueuedConnection` or you'll race QML.
- **PyInstaller libusb mirror** (`openwater.spec` lines 61–95): if bundled app fails USB enumeration, the runtime hook can't find vendored libusb DLLs. Check the spec's mirror step.
- **`laser_params.json` is not "tunable":** editing values risks laser-off, wrong pulse widths, safety failures. Treat as locked baseline.
- **SDK is editable, not pinned to a wheel here** (unlike `openmotion-test-app`). Bumping the SDK requires no action; bugs in either repo are visible immediately.
- **Don't track `docs/superpowers/`** — brainstorming specs, implementation plans, and session/status notes are local process artifacts, not repo deliverables. The directory is gitignored; keep it that way and never `git add -f` files under it into a PR.

## Branching and releases

- Default branch: `main`; daily work on `next`. PR feature → `next`, `next` → `main` for release.
- Releases triggered by semver tags (e.g. `1.1.2`, `1.1.2-dev.0`, `1.1.2-rc.1`) — see [../CLAUDE.md](../CLAUDE.md) for tag format.
- **SDK pin (#545):** rc and production tags (and main/dispatch builds) install
  exactly `openmotion-sdk==<sdk-version.txt>`; dev tags and pushes to `next`
  install `openmotion-sdk@next`. **Bump `sdk-version.txt` to the SDK release
  that is on PyPI before tagging an rc or production release** — CI fails the
  build (`scripts/check_sdk_pin.py --enforce`) if the installed SDK differs.
  The local dev setup is unaffected: it still installs the SDK editable and
  bundles whatever is installed.
- **SBOM (#545):** each CI run generates a CycloneDX SBOM of the build
  environment (`cyclonedx-py environment` via `pipx`, then
  `scripts/stamp_sbom.py` sets the app name/tag and, on rc/prod, asserts the
  SDK line equals the pin). `Open-Motion-<tag>-sbom.cdx.json` (Windows, both
  variants) and `Open-Motion-<tag>-macOS-sbom.cdx.json` are uploaded as
  workflow artifacts and attached to the GitHub Release. There is no tracked
  SBOM file any more.
- **Code signing (#443, merged 2026-09-17):** **production**-tag builds (`X.Y.Z`, no suffix) Authenticode-sign the onefile exe and both Setup bundles (Burn engine signed detached, then the reattached bundle) with the SSL.com EV cert via eSigner CKA (cloud key; `ES_*` org secrets); dev/rc tags and branch pushes build unsigned (signings are metered: 3 per variant, 6 per signed build). The app MSI inside the bundle is deliberately **not** signed (#569): it never ships on its own, Burn runs it already elevated and verifies it by the hash in its signed manifest; the accepted cost is that an AppLocker / WDAC publisher rule for MSIs would block it. A `workflow_dispatch` with the `sign` input also signs — **ask Ethan before triggering one.** The driver is EV-signed separately, once per driver change, via manual dispatch of the SDK's `driver-msi.yml`, then vendored as `resources/OpenMotionDriver-x64.zip`. Everything funnels through `installer/sign.ps1` on `CODESIGN_THUMBPRINT`. **Before this merged no release build had ever been signed** (the step lived only on the PR branch; 1.5.2 shipped unsigned). The in-app updater pins the same certificate (#544). See [docs/SIGNING.md](docs/SIGNING.md).
- CI workflows: `.github/workflows/release-build.yml` (Windows runner, builds .exe + zip on tags / manual dispatch) and `hil-tests.yml` (self-hosted Windows runner with Shelly IoT outlet power control, runs after the release build completes).

### Curating release notes (issue #348)

`release-build.yml`'s auto-generated GitHub Release body is just a raw commit
list since the *previous tag* — dev/rc noise included, and it resets on every
pre-release so it never shows the full diff since the last production
version. Treat it as a traceability appendix, not release notes. Before
announcing an rc/dev build to the test team, curate the release
(`gh release edit <tag> --notes-file <file>`) by adding two sections above
that commit list:

1. **Changelog** — plain-English feature summary since the last *production*
   release (not the last pre-release tag — diff against the last `X.Y.Z`
   with no suffix), grouped by theme. Include `openmotion-sdk` changes too:
   rc/prod tags install the exact SDK release pinned in `sdk-version.txt`
   (#545), so diff the pin at the last production tag
   (`git show <tag>:sdk-version.txt`) against the current pin, and fold in
   anything user-visible.
2. **Known Issues** — currently-open bugs a tester could hit in this build.
   Never list unimplemented/future features here. Cross-check each candidate
   against the merge log first — a ticket can still show "open" on the board
   after its fix merged, since tickets stay in **In review** through
   pre-release validation (see the board process in [../CLAUDE.md](../CLAUDE.md)).
   **Always confirm the candidate list with Ethan before publishing** — the
   backlog accumulates duplicates, stale hardware-specific reports, and
   test-infra-only bugs that don't belong in a test-team-facing note.

Keep the raw commit list / compare-diff link below the curated sections —
it's the audit trail, not something to delete.

## "Start here" by task

| Task | First files |
|---|---|
| Add or change a QML page | `pages/BloodFlow.qml` → `components/` → wire to `motion_connector.py` slot. |
| Hook a new SDK feature into the UI | Add `@pyqtSlot`/`@pyqtSignal` in `motion_connector.py`; bind in the relevant QML page. |
| Modify scan orchestration | `pages/scan/ScanRunner.qml` + the task QMLs. |
| Tune a clinical threshold | `config/app_config.py` (check the tables above first — most knobs live here; thresholds are CONSTANT, so a change is a new build). |
| Reproduce a bug without hardware | Not currently possible in the running app (no working mock mode — see "Working without hardware"). Write a `unit`-marked pytest that mocks the hardware seam instead. |
| Touch laser register defaults | `config/laser_params.json` — but loop in firmware/SDK owners first; this is locked baseline data. |
| Diagnose USB enumeration in the packaged exe | `openwater.spec` libusb mirror + `rthook_libusb_paths.py`. |
