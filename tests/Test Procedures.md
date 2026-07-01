# Open-Motion — HIL Test Procedures

Hardware-in-the-loop UI test suite for the Open-Motion desktop
application. Tests drive the real PyQt6/QML app via `pyautogui` mouse +
keyboard input, inspect window state via `pywinauto` UI Automation
(UIA), and exercise real console + camera hardware over USB. The
suite is built on **pytest**, runs on a self-hosted Windows runner
(`MOTION-RUNNER-1`), and produces JSON + Markdown reports for V&V
evidence at session end.

For style guidance on writing or editing tests, see
[`STYLE_GUIDE.md`](STYLE_GUIDE.md).

---

## Test inventory

125 tests across 11 files, split into two run-tier markers.

### `dev` tier — fires on every push to `next` (~5 min total)

| File | Tests | Coverage |
|---|---|---|
| `test_history.py` | 5 | History modal: open, scan listing, "View in plot →" into the embedded PlotViewer, close. Class-scoped autouse fixture seeds a 30 s Middle/Middle scan if History is empty. |
| `test_notes.py` | 18 | Notes textarea: open/auto-focus, type, persist across reopen, append, clear, multi-line, long text, numeric+punctuation, cut/paste/undo, sidebar toggle, rapid open/close. |
| `test_scan_settings.py` | 30 | Scan Settings modal: User Label field, Left/Right sensor dropdowns (parametrized over all 9 options × 2), duration toggle (Timed ↔ Free Run), H/M/S inputs, close via X and Escape. |

### `release` tier — fires on release-pattern tag pushes only (~3 hours total at full sweep)

| File | Tests | Coverage | Wall-clock |
|---|---|---|---|
| `test_scan_flow.py` | 15 | End-to-end happy path: configure → notes → check → 2-min scan → view in plot. | ~10 min |
| `test_clinicalmode.py` | 35 | Clinical Mode workflow in four classes (Clinical Mode forced on via `app_config.json`, not the Settings modal): keyboard-driven (05–20), mouse-driven (22–31), Settings feature (33–37: Time Window dropdown × 4, Auto-scale Y-axes ON), and modal-exclusivity detector (38: opens Settings then clicks Start, asserts only one modal is visible — verifies the `modalManager.closeCurrent()` call in `BloodFlow.qml`'s `onStartStopClicked` dismisses Settings before `ContactQualityModal` opens). | ~52 min |
| `test_connection_redesign.py` | 4 | Power-cycle resilience: app off + power on → auto-connect; idle power-cycle; mid-scan power-cycle; rapid 5× toggle survival. Requires Shelly outlet. | ~5 min |
| `test_scan_auto_stop_bug.py` | 5 | GH issue #47 repro: 5×10-min All/All scans, power-cycle between loops 1–3, skip cycle on 4–5. Loop 5 expected to fail. Requires Shelly. | ~70 min |
| `test_scan_auto_stop_bug_abbreviated.py` | 5 | Same repro logic with 2-min Far/Far scans for fast iteration. | ~25 min |
| `test_usb_disconnect_freeze.py` | 1 | USB disconnect during scan trigger; 3 iterations without restarting console; verify app does not enter Win32 hung-window state via `IsHungAppWindow()`. | ~12 min |
| `test_force_laser_fail.py` | 1 | Toggle `forceLaserFail=true`, restart app, fire laser, assert persistent safety toast appears, restore + power-cycle to clean state. | ~3 min |
| `test_calibration_target_isolation.py` | 1 | Issue #117 — calibration target dropdown isolation. Calibrate Both → Left → Right; between iterations close the app, open a fresh `MotionInterface` in-process to read the console EEPROM JSON, and assert (a) every dump is numerically valid (`C_max > C_min`, `I_max > I_min`, finite), and (b) the un-targeted module's row is byte-identical to the previous dump. Confirms a Left calibration cannot clobber the Right module's stored values, and vice versa. | ~7 min |

Everything in the `release` tier requires a connected console and at
least one sensor. Tests that cycle power additionally require
`$SHELLY_IP_ADDRESS` to point at the bench's Shelly outlet.

---

## Quick start

### Prerequisites

- Windows 11 with an interactive desktop session (UI tests need a real display).
- Python 3.12.
- Open-Motion app — either the frozen build (`OPENWATER_EXE=…`) or the source (`OPENWATER_FROM_SOURCE=1`).
- For `release`-tier tests: Shelly outlet on the LAN, `$SHELLY_IP_ADDRESS` exported.

### Install dependencies

```bash
pip install pytest pyautogui pywinauto pygetwindow psutil requests
```

### Run the dev tier locally

```bash
cd tests
pytest -m dev
```

### Run a specific test file

```bash
pytest test_scan_settings.py -v
```

### Run the full release tier (requires hardware)

```bash
pytest -m release
```

### Run from-source instead of from a frozen build

```bash
pytest --from-source             # equivalent to OPENWATER_FROM_SOURCE=1
```

---

## Architecture

### File layout

```
tests/
├── conftest.py                 ← shared fixtures, autouse guards, report writer
├── utils.py                    ← UIA helpers, panel button calibration, log tailing
├── shelly.py                   ← Shelly outlet driver (HTTP)
├── pytest.ini                  ← markers, JUnit XML, log file
├── STYLE_GUIDE.md              ← conventions for writing/editing HIL tests
├── Test Procedures.md          ← this document
├── test_history.py             ← dev    — History modal
├── test_notes.py               ← dev    — Session Notes textarea
├── test_scan_settings.py       ← dev    — Scan Settings modal
├── test_scan_flow.py           ← release — end-to-end happy path
├── test_clinicalmode.py         ← release — Clinical Mode workflow
├── test_connection_redesign.py ← release — power-cycle resilience
├── test_scan_auto_stop_bug.py  ← release — issue #47 repro (10-min scans)
├── test_scan_auto_stop_bug_abbreviated.py  ← release — same, 2-min scans
├── test_usb_disconnect_freeze.py           ← release — USB disconnect freeze
└── test_force_laser_fail.py                ← release — forceLaserFail flag
```

### Key infrastructure (in `conftest.py` and `utils.py`)

| Capability | Where | Why it exists |
|---|---|---|
| **Panel button calibration** | `utils.calibrate_panel_buttons` (autouse session) | DPI scaling and window size differ across machines. Static `(rx, ry)` ratios miss the hitbox on the runner. UIA discovery first, QML pixel-layout fallback, ratio fallback last. |
| **App-alive guard** | `_check_app_alive` (autouse function) | If a test crashes the app, fail the *next* test fast with a pointed message naming the killer test, instead of cascading "App window not found" through every later test. |
| **Modal cleanup** | `_dismiss_leftover_modals_per_class` (autouse class) | Sends Escape between classes so a stale Session Notes modal from a prior class can't mask the current class's UIA queries. |
| **Test report writer** | `_hil_report_session` + `_write_hil_report` (atexit) | Parses `test_logs/results.xml` after pytest finalises the session and writes `HIL_Report_<timestamp>.json` + `.md`. Markdown report includes summary, per-test grid, failure details, environment info, sign-off block. |
| **App log tailing** | `utils.find_app_log` + `utils.wait_for_pattern` | Power-cycle / disconnect tests assert against the SDK's `state … -> CONNECTED` / `DISCONNECTED` log lines instead of guessing from UI state. |
| **Shelly outlet driver** | `shelly.py` | Power-cycle the console via HTTP for reconnect resilience tests. Skips cleanly if outlet is unreachable. |
| **`@pytest.mark.incremental`** | `conftest.py` hooks | When a test in an incremental class fails, subsequent tests in the same class are auto-xfailed. Stops cascade noise on workflow tests where step N depends on N-1. |

### Stable interaction: `click_panel(label)` over coordinates

The QML sidebar's panel buttons (Start, Scan Settings, Notes, Check,
History, Settings) are MouseArea-driven and don't expose accessible
names directly, but the inner `Text` elements do — which UIA can
find. `utils.calibrate_panel_buttons` walks UIA once at session
start, caches each button's screen rect, and `click_panel("History")`
is a cheap dict lookup thereafter. If UIA fails for a button, falls
through to a QML pixel-layout calculation (e.g. button center =
`w.left + 56, w.top + 113 + slot×85`).

Call `recalibrate_panel_buttons()` after any test that resizes or
moves the window. The auto-update banner (`UpdateBanner.qml`) shifts
the sidebar down by 36 px when visible — calibration handles that
automatically.

---

## CI integration

Two workflows in `.github/workflows/`:

- `release-build.yml` — builds the frozen app on every push to
  `main`/`next` and on tag pushes. Different SDK source per tag form
  (see `AGENTS.md` for the SDK selection table).
- `hil-tests.yml` — chained off Build & Release via `workflow_run`.
  Picks the test set from the upstream commit:
  - Tag matching `[0-9]+.[0-9]+.[0-9]+` or `[0-9]+.[0-9]+.[0-9]+-rc.[0-9]+`
    → `pytest -m release`.
  - Push to `next` (no tag) → `pytest -m dev`.
  - Anything else → skip.

JUnit XML is uploaded as an artifact along with `tests/test_logs/`
(pytest log + the JSON/Markdown HIL reports + any per-test screenshot
captures).

---

## App discovery

The `app` fixture finds the bloodflow app in this order:

1. `$OPENWATER_FROM_SOURCE=1` → launch via `python main.py`.
2. `$OPENWATER_EXE` → use that absolute path.
3. Glob across `Documents/OpenMotion/`, `Desktop/`, `Program Files/`.
4. `Open-Motion.exe` next to `tests/`.

If none match, the fixture `pytest.skip`s.

---

## Output artifacts (gitignored)

| Path | Contents |
|---|---|
| `tests/test_logs/results.xml` | pytest JUnit XML (consumed by the report writer) |
| `tests/test_logs/pytest.log` | full pytest log for the run |
| `tests/test_logs/HIL_Report_<ts>.json` | structured V&V report (per-test results, env, summary) |
| `tests/test_logs/HIL_Report_<ts>.md` | human-readable V&V report with sign-off block |
| `tests/logs/` | bloodflow app logs captured during tests |
| `logs/` | bloodflow app logs captured outside the tests dir (varies by working dir) |

---

## Writing or editing tests

Read [`STYLE_GUIDE.md`](STYLE_GUIDE.md) first. Quick rules of thumb:

- One feature per file. Marker (`dev` or `release`) at the top.
- File-level docstring naming the feature, what it covers, and any
  preconditions a fresh runner needs.
- Sequential method numbering inside an incremental class
  (`test_01_open`, `test_02_…`).
- Use `click_panel(label)` for sidebar panel buttons, never raw
  `click_sidebar(*ratio)`.
- Polling-based waits over `time.sleep(N)` for state checks.
- Wrap state-mutating tests in `try/finally` so a failure can't
  poison the bench config for subsequent tests.
- Tests that run a scan dismiss the auto-opened Session Notes modal
  at the end (the autouse cleanup catches it as defence in depth,
  but fix the source).
- Failure messages name what was checked and why it likely failed.

There's a 12-item checklist at the bottom of `STYLE_GUIDE.md` to run
through before committing a new test.
