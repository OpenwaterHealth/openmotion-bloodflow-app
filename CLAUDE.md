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
#   build/installer/Openwater-Setup-<ver>.exe          (Clinical installer)
#   build/installer/Openwater-Setup-<ver>_Research.exe (Research installer)
# build_and_zip.ps1 runs PyInstaller once, then scripts/package_artifacts.ps1
# loops the variants. Installers are skipped with a warning if WiX isn't found;
# scripts/package_artifacts.ps1 -SkipInstaller forces portable-only.
```

- Tested on **Python 3.13.5**; `requirements.txt` pins PyQt6 6.8.0, qasync 0.27.1, pandas, numpy, matplotlib, pyusb, libusb1, PyInstaller 6.11.1, flake8 7.1.1.
- No `pyproject.toml`; pure `requirements.txt`.
- QML does **not** hot-reload — restart the app to pick up `.qml` changes.

## Layout

| Path | What lives here |
|---|---|
| `main.py` | Entry point. PyQt app, QML engine, logging. Registers `MotionInterface` as a QML singleton. |
| `motion_connector.py` | **4031 lines.** Single `MotionConnector` QObject — all UI⇄hardware glue, 135 signals/slots. State machine constants at lines 72–76; transitions at 1076–1084. |
| `motion_config.py` | FPGA model + laser-parameter helpers (extracted in May 2025 for reuse). |
| `pages/BloodFlow.qml` | Main scan page: patient info, sensor config, trigger. |
| `pages/DataAnalysis.qml` | Post-processing + BFI/BVI visualization. |
| `pages/Settings.qml` | Settings overlay. |
| `pages/scan/` | `ScanRunner.qml` plus task QMLs: `CaptureDataTask`, `ContactQualityCheckTask`, `FlashSensorsTask`, `PostProcessTask`, `SetTriggerTask`. Newer orchestration suite. |
| `components/` | 29 reusable QML components — `SettingsModal`, `ContactQualityModal`, `CameraDot`, `TestResultsWindow`, etc. |
| `processing/visualize_bloodflow.py` | BFI/BVI computation from CSV histograms. |
| `config/app_config.json` | 56 feature flags / thresholds, grouped by topic: output paths, scan/camera masks, plot/UI, calibration + FT thresholds, contact quality, developer/debug. |
| `config/laser_params.json` | 18 laser I2C register sets (TA / SEED / EE / OPT variants). **Not user-tunable calibration data** — init/baseline commands for the laser driver chips. |
| `openwater.spec` | PyInstaller spec. Custom logic mirrors vendored libusb binaries into `_internal\_vendor` so the runtime hook can find them. |
| `tests/` | Hardware-in-loop pytest suite, ~23 files. Markers: `@pytest.mark.dev` (~1–2 min, runs on every push to `next`), `@pytest.mark.release` (~6–8 min, runs on release tags). |

**Note:** the old `motion_singleton.py` no longer exists — connector logic was consolidated into `motion_connector.py` and registered as a QML singleton in `main.py`.

## State machine (motion_connector.py:72)

```
DISCONNECTED (0) → SENSOR_CONNECTED (1) → CONSOLE_CONNECTED (2) → READY (3) → RUNNING (4)
```

No FSM class — integer enum + conditional branches on `self._state`. Transitions in `motion_connector.py` around line 1076.

QML↔Python wiring: `main.py:256` registers the connector as a QML singleton (`qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", connector)`). QML calls `MotionInterface.slotName()`; Python emits signals QML connects to with `onSignalNameChanged`.

## Working without hardware

There is currently **no working no-hardware mock mode** for the running app:

- `cameraFakeData` in `app_config.json` is broken — ignore it (confirmed 2026-06-11). It never enabled a hardware-free launch anyway: it only sets the `DEBUG_FLAG_FAKE_DATA` bit on an already-connected sensor (`motion_connector.py:714` / `_run_sensor_init`), and the firmware-side synthetic histograms no longer work.
- The SDK's `demo_mode` is also broken (confirmed 2026-05-28).

To exercise app logic without hardware, write unit tests that mock the hardware seams instead — see the tests marked `@pytest.mark.unit` (e.g. `tests/test_live_plot_sink.py`, `tests/test_scan_notes_db.py`). The conftest autouse fixtures short-circuit on that marker, so no app launch or UI machinery fires.

Debug flags that are still useful when hardware **is** attached (`config/app_config.json`):

- `engineeringMode: true` — show per-camera CQ dots, test buttons, debug telemetry.
- `commVerbose: true` + `verboseCommandHandling: true` — SDK logs all UART packets + MCU printf output.

## Notable config flags (`config/app_config.json`)

| Flag | Default | Purpose |
|---|---|---|
| `engineeringMode` | `true` | Show debug telemetry, per-camera CQ dots, test buttons. |
| `clinicalMode` | `true` | Clinical UI: hide settings, large BFI/BVI panels. Build-time only — there is no Settings toggle for it (removed; config-only now). |
| `portableMode` | `false` | Build-time flag: `true` keeps all writable state (config overrides, logs, scan data/db) next to the exe (old un-installed layout); `false` scatters it to `%PROGRAMDATA%\Openwater`. Portable zips ship `true`, installers force `false` — see `Set-PortableMode` (`scripts/build_common.ps1`) and `utils/app_paths.py:writable_root`. |
| `forceLaserFail` | `false` | Debug: simulate a laser safety trip. |
| `cameraFakeData` | `false` | **Broken — do not use.** Was meant to request firmware fake histograms; see "Working without hardware". |
| `histoThrottle` | `false` | Drop histograms to reduce log spam. |
| `histoCmp` | `true` | Histogram compression (firmware `DEBUG_FLAG_HISTO_CMP`, bit `0x40` — firmware logs it as "histo compress"). Toggle live from Settings → Engineering → "Histogram compression". |
| `deferHistoSend` | `true` | Defer the per-frame histogram send out of the FSIN ISR into the firmware main loop (firmware `DEBUG_FLAG_SEND_DEFER`, bit `0x80`; sensor-fw#68). Config-only (no Settings UI); pushed to connected sensors at connect via `_compute_sensor_debug_flags`. Requires the PR-branch sensor firmware — stock firmware ignores bit 7. |
| `tecTripTempC` | `40` | Console over-temp trip (°C) pushed to the console user config on connect via `motion_config.ensure_tec_trip` (read-modify-write, preserves calibration + OPT/EE keys). Validated to 1–60 °C; absent/invalid values leave the device's existing trip untouched (never writes `0`, which would disable the firmware trip). |
| `ft_min_mean_per_camera` | `[40,40,…]` | Calibration pass threshold — min pixel mean per camera (8-element array). |
| `calibration_scan_duration_sec` | `15` | Calibration runtime. |
| `test_scan_duration_sec` | `5` | "Test" scan runtime (feature #132). |
| `cq_dark_threshold_per_camera` | `[3.0,…]` | Contact-quality dark threshold. |
| `bfiClampLow` / `bfiClampHigh` | `0.0` / `10.0` | Display clamps (values outside show `--`). |
| `bviLowPassEnabled` | `true` | 1-pole LPF on BVI (cutoff 40 Hz). |
| `dataDirectory` | `C:\Users\ethan\Projects\scan_data` | Single output root — `logs/` and `data/` (scan CSVs/DB, calibrations) land under here. |

## Reading the app log

Every launch writes a timestamped log to `<dataDirectory>/logs/open-motion-<YYYYMMDD_HHMMSS>.log`. Use it as the first stop when diagnosing scan / calibration / connect failures — it captures every SDK + connector log line, including pipeline-stage exceptions that are caught and silently swallowed by `ScanRunner._safe_consume`.

```powershell
# Latest log, full contents:
Get-ChildItem C:\Users\ethan\Projects\scan_data\logs\open-motion-*.log |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1 | Get-Content

# Filter the latest log for failure-shaped lines (most common starting point):
Get-ChildItem C:\Users\ethan\Projects\scan_data\logs\open-motion-*.log |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1 |
  Get-Content | Select-String -Pattern "WARNING|ERROR|raised|exception|Traceback|FAIL|aborted" -Context 0,3

# Just the calibration outcome:
Get-ChildItem C:\Users\ethan\Projects\scan_data\logs\open-motion-*.log |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1 |
  Get-Content | Select-String -Pattern "Calibration phase|procedure complete|samples captured"
```

The `dataDirectory` config key controls the root (defaults to cwd if unset — falls back to `~/Documents/Open-Motion` on macOS). When unset on a frozen build, the default instead follows `portableMode`: next to the exe (portable zip) or `%PROGRAMDATA%\Openwater` (installer). Two fixed children live under that root:
- `logs/` — app log files (one per launch)
- `data/` — everything else: scan output files (raw / corrected / telemetry CSV + `scans.db`) land directly here; `data/calibrations/` holds saved calibration JSONs plus the SDK's per-camera PASS/FAIL CSVs (`calibration-<ts>.csv` / `test-<ts>.csv`); `data/debug-bundles/` holds "Send Debug Logs" zips; `data/updates/` holds in-app-updater downloads. Scan notes live in `scans.db` (`sessions.session_notes`), not as files; `*_notes.txt` files are legacy read-only fallbacks. (`data/ft-test-csvs/` was a legacy per-scan factory-test export — dead since May 2026 and retired; the Test/Calibrate flows' CSVs are its superset.)

**Important:** the runner is fail-soft. `ScanRunner._safe_consume` catches sink exceptions and logs them as `sink %r raised on channel ...` at ERROR; `pipeline.process` exceptions log as `pipeline.process raised — resetting and continuing` at ERROR. **Neither aborts the scan**, so the app may report "complete" while every interval was actually broken. Always grep for `raised|exception` even on apparent successes when something downstream looks wrong.

## Gotchas

- **`motion_connector.py` is 4031 lines** — the file is doing too much. Don't add to it without considering extraction; recent precedent is `motion_config.py` (May 2025).
- **Cross-thread signals:** 135+ signals; several (e.g. `_calibrationCompleteSignal`, `safetyTripDuringCaptureRequested`) fire from USB I/O / scanner worker threads. Use `Qt.QueuedConnection` or you'll race QML.
- **PyInstaller libusb mirror** (`openwater.spec` lines 61–95): if bundled app fails USB enumeration, the runtime hook can't find vendored libusb DLLs. Check the spec's mirror step.
- **`laser_params.json` is not "tunable":** editing values risks laser-off, wrong pulse widths, safety failures. Treat as locked baseline.
- **SDK is editable, not pinned to a wheel here** (unlike `openmotion-test-app`). Bumping the SDK requires no action; bugs in either repo are visible immediately.
- **Don't track `docs/superpowers/`** — brainstorming specs, implementation plans, and session/status notes are local process artifacts, not repo deliverables. The directory is gitignored; keep it that way and never `git add -f` files under it into a PR.

## Branching and releases

- Default branch: `main`; daily work on `next`. PR feature → `next`, `next` → `main` for release.
- Releases triggered by semver tags (e.g. `1.1.2`, `1.1.2-dev.0`, `1.1.2-rc.1`) — see [../CLAUDE.md](../CLAUDE.md) for tag format.
- CI workflows: `.github/workflows/release-build.yml` (Windows runner, builds .exe + zip on tags / manual dispatch) and `hil-tests.yml` (self-hosted Windows runner with Shelly IoT outlet power control, runs after the release build completes).

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
