# openmotion-bloodflow-app — Claude guide

PyQt6 + QML clinical desktop app for blood flow / volume monitoring. Consumes the `omotion` package from `openmotion-sdk` (installed editable from `../openmotion-sdk`).

Cross-repo context: [../CLAUDE.md](../CLAUDE.md). SDK details: [../openmotion-sdk/CLAUDE.md](../openmotion-sdk/CLAUDE.md).

## Run / build

```powershell
# Dev setup — SDK is installed editable from the sibling repo
pip install -r requirements.txt
pip install -e ../openmotion-sdk

python main.py                          # run the app

python -m PyInstaller -y openwater.spec # package .exe → dist/Open-Motion/
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

## Layout

| Path | What lives here |
|---|---|
| `main.py` | Entry point. PyQt app, QML engine, logging. Registers `MotionInterface` as a QML singleton. |
| `motion_connector.py` | **~4,700 lines.** Single `MotionConnector` QObject — all UI⇄hardware glue, ~64 signals / ~80 slots. State machine constants around line 252; transitions in `update_state` (~line 1947). |
| `motion_config.py` | FPGA model + laser-parameter helpers (extracted in May 2025 for reuse). |
| `pages/BloodFlow.qml` | Main scan page: patient info, sensor config, trigger. The only page `main.qml` loads. |
| `components/PlotViewer.qml` | Real-time + replay BFI/BVI plot viewer (pan/zoom DVR, autoscale). |
| `components/SettingsModal.qml` | Settings overlay (opened from BloodFlow — there is no `pages/Settings.qml`). |
| `pages/scan/` | `ScanRunner.qml` plus task QMLs: `CaptureDataTask`, `ContactQualityCheckTask`, `FlashSensorsTask`, `PostProcessTask`, `SetTriggerTask`. Newer orchestration suite. |
| `components/` | 25 reusable QML components — `SettingsModal`, `ContactQualityModal`, `CameraDot`, `TestResultsWindow`, etc. |
| `processing/visualize_bloodflow.py` | BFI/BVI computation from CSV histograms. |
| `config/app_config.json` | 56 feature flags / thresholds, grouped by topic: output paths, scan/camera masks, plot/UI, calibration + FT thresholds, contact quality, developer/debug. |
| `config/laser_params.json` | 18 laser I2C register sets (TA / SEED / EE / OPT variants). **Not user-tunable calibration data** — init/baseline commands for the laser driver chips. |
| `resources/sample_scan.csv` | Real exported scan (History → Export CSV / SDK `materialize_corrected_csv` per-cam wide format). Offered — never auto-loaded — into the replay viewer when a **research build** boots with **no device connected** (#314): the startup connection watchdog raises `components/SampleScanOfferModal.qml`, and only accepting it binds the sample. Clinical builds never see the offer. Swap this one file to change the sample dataset. Bundled by `openwater.spec` (targeted `datas` entry); located at runtime via `utils.resource_path.resource_path("resources", "sample_scan.csv")`. Parsed DB-free by `data_sources.load_csv_scan_buffers` → `PastScanSource(preloaded_buffers=…, scan_db=None)`; the user's real `scans.db` is never touched. |
| `openwater.spec` | PyInstaller spec. Custom logic mirrors vendored libusb binaries into `_internal\_vendor` so the runtime hook can find them. |
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

- `cameraFakeData` in `app_config.json` is broken — ignore it (confirmed 2026-06-11). It never enabled a hardware-free launch anyway: it only sets the `DEBUG_FLAG_FAKE_DATA` bit on an already-connected sensor (`motion_connector.py:714` / `_run_sensor_init`), and the firmware-side synthetic histograms no longer work.
- The SDK's `demo_mode` is also broken (confirmed 2026-05-28).

To exercise app logic without hardware, write unit tests that mock the hardware seams instead — see the tests marked `@pytest.mark.unit` (e.g. `tests/test_live_plot_sink.py`, `tests/test_scan_notes_db.py`). The conftest autouse fixtures short-circuit on that marker, so no app launch or UI machinery fires.

**Boot with no device → sample scan offer (#314):** nothing auto-loads in any build variant anymore. Instead, the existing startup connection watchdog (`connectionTimeoutSec`, default `8` s — same one-shot timer that raises the E-104/E-106 warning toast) also calls `_maybe_offer_sample_scan()`. In **research (non-clinical) builds only**, when no device at all is connected (`_any_device_connected()`, not merely a missing console) and nothing is already bound to the viewer, the connector emits `sampleScanOfferRequested`; `components/SampleScanOfferModal.qml` shows a "No console detected — would you like to open a sample dataset?" dialog. Only clicking "Open sample dataset" calls the `loadSampleScan()` slot, which binds `resources/sample_scan.csv` to the plot viewer as a DB-free `PastScanSource` so you can pan/zoom/scrub real BFI/BVI traces. Clinical builds get neither the auto-load nor the dialog — a clinical user is never shown fabricated traces, not even behind a prompt. Declining (or just not answering) is final for that launch; relaunch to be offered again. This is NOT a mock mode (no live capture, no CQ, no scan flow); it just gives the replay/DVR machinery real data to explore. Starting a real scan later replaces the sample source; a missing/corrupt CSV fails soft (logged, viewer stays empty). Gating + parsing are unit-tested in `tests/test_sample_scan_replay.py`.

Debug flags that are still useful when hardware **is** attached (`config/app_config.json`):

- `engineeringMode: true` — show per-camera CQ dots, test buttons, debug telemetry.
- `commVerbose: true` + `verboseCommandHandling: true` — SDK logs all UART packets + MCU printf output.

## Notable config flags (`config/app_config.json`)

| Flag | Default | Purpose |
|---|---|---|
| `engineeringMode` | `false` | Engineering UI + diagnostics: debug telemetry, per-camera CQ dots, test buttons, Force Dismiss in the CQ modal footer, firmware-update banner, profiling HUD. Also gates the per-scan telemetry CSV (issue #43 — clinical users must not get it; raw CSVs are gated `!clinicalMode \|\| engineeringMode` since #234). Unlockable at runtime via `EngineeringUnlockModal`. |
| `clinicalMode` | `true` (repo config) | Clinical build variant: hide settings, large BFI/BVI panels. `main.py` baseline default is `false` (= Research distribution; window title "Open-Motion Research"). The build flips it per artifact variant (`scripts/build_common.ps1`); env override `OPENMOTION_CLINICAL=1/0` beats both. Build-time only (#233): no Settings toggle, and `config_store` neither loads nor persists it as a runtime override. |
| `portableMode` | `false` | Build-time flag: `true` keeps all writable state (config overrides, logs, scan data/db) next to the exe (old un-installed layout); `false` scatters it to `%PROGRAMDATA%\Openwater`. Portable zips ship `true`, installers force `false` — see `Set-PortableMode` (`scripts/build_common.ps1`) and `utils/app_paths.py:writable_root`. Env override `OPENMOTION_PORTABLE=1` for dev testing. |
| `forceLaserFail` | `false` | Debug: simulate a laser safety trip. |
| `cameraFakeData` | `false` | **Broken — do not use.** Was meant to request firmware fake histograms; see "Working without hardware". |
| `histoThrottle` | `false` | Drop histograms to reduce log spam. |
| `histoCmp` | `true` | Histogram compression (firmware `DEBUG_FLAG_HISTO_CMP`, bit `0x40` — firmware logs it as "histo compress"). Toggle live from Settings → Engineering → "Histogram compression". |
| `deferHistoSend` | `true` | Defer the per-frame histogram send out of the FSIN ISR into the firmware main loop (firmware `DEBUG_FLAG_SEND_DEFER`, bit `0x80`; sensor-fw#68). Config-only (no Settings UI); pushed to connected sensors at connect via `_compute_sensor_debug_flags`. Requires the PR-branch sensor firmware — stock firmware ignores bit 7. |
| `scanDataStallTimeoutSec` | `15` | Whole-scan data-stall watchdog (#248): abort the scan with the E-303 critical modal when NO camera delivers a frame for this long while the trigger is ON. `<= 0` disables. Per-camera dropouts (`cameraDropoutThresholdSec`, default 2 s, code-only key) stay fail-soft — only total loss aborts. |
| `tecTripTempC` | `40` | Console over-temp trip (°C) pushed to the console user config on connect via `motion_config.ensure_tec_trip` (read-modify-write, preserves calibration + OPT/EE keys). Validated to 1–60 °C; absent/invalid values leave the device's existing trip untouched (never writes `0`, which would disable the firmware trip). |
| `ft_min_mean_per_camera` | `[40,40,…]` | Calibration pass threshold — min pixel mean per camera (8-element array). |
| `calibration_scan_duration_sec` | `15` | Calibration runtime. |
| `test_scan_duration_sec` | `5` | "Test" scan runtime (feature #132). |
| `cq_dark_threshold_per_camera` | `[3.0,…]` | Contact-quality dark threshold. |
| `bfiClampLow` / `bfiClampHigh` | `0.0` / `10.0` | Display clamps (values outside show `--`). |
| `bviLowPassCutoffHz` | `20.0` | 1-pole IIR low-pass on the **displayed** BVI stream only (live PlotViewer traces + clinical side averages, applied at `LiveScanSource` ingest); `scans.db`, CSVs, DB-tail history, and replay stay raw. Config-only, no Settings UI — the switch and `bviLowPassEnabled` were removed (#228); the number is the whole contract: missing/invalid → 20, `<= 0` disables. alpha = dt/(RC+dt) at nominal 40 Hz (≈ 0.76 at 20 Hz). |
| `writeRawCsv` | `false` | Opt-in raw histogram CSVs (`{scan_id}_(left\|right)_mask*_raw.csv`). Settings → Data Output toggle, shown when `!clinicalMode \|\| engineeringMode` (#234 — research users get raw CSVs); the same flag gate is re-checked at scan start, so a plain clinical build never writes raw output even if the toggle was left on (#43). `rawCsvDurationSec` caps seconds written (`null` = whole scan). |
| `writeCorrectedCsv` | `false` | Opt-in corrected per-cam CSV (`{scan_id}.csv`) — redundant now that per-cam BFI/BVI lands in `scans.db`. Config-only, no Settings UI. |
| `dataDirectory` | `null` | Single output root — `logs/` and `data/` (`scans.db`, calibrations) live under it. `null` = `app_paths.writable_root()`: cwd for dev runs, exe-adjacent or `%PROGRAMDATA%\Openwater` per `portableMode` when frozen. |

## Reading the app log

Every launch writes a timestamped log to `<dataDirectory>/logs/open-motion-<YYYYMMDD_HHMMSS>.log`. Use it as the first stop when diagnosing scan / calibration / connect failures — it captures every SDK + connector log line, including pipeline-stage exceptions that are caught and silently swallowed by `ScanRunner._safe_consume`.

```powershell
# Dev runs (dataDirectory null) log to <repo>/logs/. If dataDirectory is set
# in config/app_config.json, substitute <dataDirectory>\logs\ below.

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

The `dataDirectory` config key controls the root (defaults to cwd if unset — falls back to `~/Documents/Open-Motion` on macOS). When unset on a frozen build, the default instead follows `portableMode`: next to the exe (portable zip) or `%PROGRAMDATA%\Openwater` (installer). Two fixed children live under that root:
- `logs/` — app log files (one per launch)
- `data/` — everything else. **Scan output is DB-only by default**: everything lands in `scans.db` (per-cam BFI/BVI, sessions, notes in `sessions.session_notes`); no per-scan CSVs are written unless opted in. User-facing CSVs are export-time artifacts: History → Export CSV (`exportScanCsv`). The opt-in per-scan CSVs land directly in `data/`: telemetry CSV (`{scan_id}_{subject}_telemetry.csv`, gated on `engineeringMode` — issue #43), raw histogram CSVs (`(!clinicalMode \|\| engineeringMode) && writeRawCsv`, #234), corrected per-cam CSV (`writeCorrectedCsv`). `data/calibrations/` holds saved calibration JSONs plus the SDK's per-camera PASS/FAIL CSVs (`calibration-<ts>.csv` / `test-<ts>.csv`); `data/debug-bundles/` holds "Send Debug Logs" zips; `data/updates/` holds in-app-updater downloads. `*_notes.txt` files are legacy read-only fallbacks. (`data/ft-test-csvs/` was a legacy per-scan factory-test export — dead since May 2026 and retired; the Test/Calibrate flows' CSVs are its superset.)

**Important:** the runner is fail-soft. `ScanRunner._safe_consume` catches sink exceptions and logs them as `sink %r raised on channel ...` at ERROR; `pipeline.process` exceptions log as `pipeline.process raised — resetting and continuing` at ERROR. **Neither aborts the scan**, so the app may report "complete" while every interval was actually broken. Always grep for `raised|exception` even on apparent successes when something downstream looks wrong.

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
   prod/rc tags install the SDK's latest PyPI release at build time (see
   `release-build.yml`'s SDK-selection step), so diff the SDK version bundled
   at the last production release (check that build's CI log for
   `Successfully installed ... openmotion-sdk-X.Y.Z`) against whatever's
   currently on PyPI, and fold in anything user-visible.
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
| Tune a clinical threshold | `config/app_config.json` (check the table above first — most knobs live here). |
| Reproduce a bug without hardware | Not currently possible in the running app (no working mock mode — see "Working without hardware"). Write a `unit`-marked pytest that mocks the hardware seam instead. |
| Touch laser register defaults | `config/laser_params.json` — but loop in firmware/SDK owners first; this is locked baseline data. |
| Diagnose USB enumeration in the packaged exe | `openwater.spec` libusb mirror + `rthook_libusb_paths.py`. |
