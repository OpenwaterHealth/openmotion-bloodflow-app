# New UI Test Scripts — HIL (Hardware-in-the-Loop) UI Automation

Automated GUI tests for the OpenWater Bloodflow desktop application using **pytest**, **pyautogui**, and **pywinauto**. Designed to run on a self-hosted GitHub Actions runner with the app and hardware connected.

## Test Suites

| File | Tests | What it covers |
|------|-------|----------------|
| `test_scan_settings.py` | 28 | Sensor dropdowns (all 9 options x2), duration toggle, H/M/S inputs, close via X and Escape |
| `test_scan_flow.py` | 15 | End-to-end: configure scan settings, type notes, start 2-min scan, wait, visualize BFI/BVI and Contrast/Mean |
| `test_history.py` | 7 | Open History modal, verify scan listing, visualize plots, close |
| `test_notes.py` | 20 | Typing, persistence, multi-line, clipboard ops (cut/paste/undo), long text, rapid open/close |

## Quick Start

### Prerequisites

- Windows 11 with a display (tests use GUI automation)
- Python 3.9+
- The OpenWater Bloodflow app installed or available

### Install dependencies

```bash
pip install pytest pyautogui pywinauto pygetwindow psutil
```

### Run all tests

```bash
cd tests/New_UI_TestScripts
pytest
```

### Run a single suite

```bash
pytest test_scan_settings.py -v
```

### JUnit XML output

JUnit XML is generated automatically at `test_logs/results.xml` (configured in `pytest.ini`). Override the path with:

```bash
pytest --junitxml=custom_path/results.xml
```

## GitHub Actions Integration

```yaml
- name: Run HIL UI Tests
  run: |
    cd tests/New_UI_TestScripts
    pytest

- name: Publish Test Results
  uses: dorny/test-reporter@v1
  if: always()
  with:
    name: HIL Test Results
    path: tests/New_UI_TestScripts/test_logs/results.xml
    reporter: java-junit
```

## Architecture

```
New_UI_TestScripts/
  conftest.py           # Shared fixtures and helpers:
                        #   - app() fixture (session-scoped, launches or connects to app)
                        #   - require_focus() — asserts app has foreground focus before keystrokes
                        #   - click_sidebar() — coordinate-based clicks for MouseArea sidebar buttons
                        #   - click_by_name() — UIA element lookup + pyautogui click
                        #   - read_combobox_values() — UIA readback for verification
                        #   - @pytest.mark.incremental — stops class on first failure
                        #   - wait_with_log(), get_clipboard(), etc.
  pytest.ini            # pytest config (verbose, junitxml, live logging)
  test_scan_settings.py # Scan Settings modal tests
  test_scan_flow.py     # End-to-end scan flow tests
  test_history.py       # History modal tests
  test_notes.py         # Session Notes tests
```

### Key Design Decisions

**Sequential tests with `@pytest.mark.incremental`**: Each test class represents a UI workflow where steps depend on the previous state (e.g., open modal -> interact -> close). When a test fails, subsequent tests in the same class are marked `xfail` instead of running against broken UI state.

**Focus guards (`require_focus()`)**: Before sending any bare keystrokes via pyautogui, tests call `require_focus()` to ensure the app window is the active foreground window. This prevents keystrokes from silently going to the wrong window.

**UIA readback verification**: After UI interactions (e.g., selecting a ComboBox option), tests read back the actual value via Windows UI Automation and assert it matches the expected value — ensuring the interaction actually landed.

**Sidebar clicks are coordinate-based**: The QML sidebar uses `MouseArea` elements which are not exposed via UIA. These buttons are clicked using relative window coordinates. All other elements (modal buttons, dropdowns) are found by name via UIA.

## App Discovery

Tests find the app in this order:
1. `OPENWATER_EXE` environment variable
2. Glob search in common install locations (`Documents/OpenMotion/`, `Desktop/`, `Program Files/`)
3. `OpenWaterApp.exe` in the test script directory

Set the env var for CI:
```bash
set OPENWATER_EXE=C:\path\to\OpenWaterApp.exe
```

## Output Artifacts (gitignored)

| Directory | Contents |
|-----------|----------|
| `test_logs/` | `results.xml` (JUnit), `pytest.log`, per-suite logs |
| `run-logs/` | Scan run CSV data and metadata |
| `scan_data/` | Raw scan data captured during test runs |
| `app-logs/` | Application debug logs |

## Legacy Scripts

The original standalone test scripts (`scan_flow_test.py`, `scan_settings_test.py`, `history_test.py`, `notes_test.py`) are still present. They use a custom JSON reporting format and can be run directly with `python script_name.py`. The new `test_*.py` files are the pytest equivalents.
