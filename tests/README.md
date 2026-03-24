# BloodFlow HappyPath Test Runner

Automated UI test suite for the **OpenWater BloodFlow** application.  
Runs 4 tests in sequence and produces a single combined JSON report.

---

## Files Overview

| File | Purpose |
|---|---|
| `HappyPath_Tests.bat` | Builds `HappyPathTestRunner.exe` using PyInstaller |
| `combined_runner.py` | Orchestrator — runs all 4 tests in order, merges results into one JSON |
| `SubjectIDwithJson.py` | Test 1 — validates Subject ID field inputs |
| `Notes.py` | Test 2 — tests the Notes text area (empty, single-line, multiline, long text) |
| `Sensorduration.py` | Test 3 — tests sensor dropdowns, duration slider, and Start Scan button |
| `Analyze.py` | Test 4 — navigates Analyze page, runs BFI/BVI and Contrast/Mean visualizations |

---

## How to Build the EXE

> **Requirements:** Python installed and on PATH, internet access for pip.

1. Place all the following files in the **same folder**:
   - `HappyPath_Tests.bat`
   - `combined_runner.py`
   - `SubjectIDwithJson.py`
   - `Notes.py`
   - `Sensorduration.py`
   - `Analyze.py`

2. Double-click `HappyPath_Tests.bat`

3. The script will automatically:
   - Check Python is available
   - Install PyInstaller if not already installed
   - Clean any previous build artifacts
   - Build the exe

4. Output: `dist\HappyPathTestRunner.exe`

---

## How to Run the Tests

1. Copy `dist\HappyPathTestRunner.exe` to the same folder as `OpenWaterApp.exe`
2. Double-click `HappyPathTestRunner.exe`
3. The 4 tests run automatically in this order:
   - **Test 1** — Subject ID Validation
   - **Test 2** — Notes
   - **Test 3** — Sensor Duration
   - **Test 4** — Analyze

4. When complete, a single report file is written:

```
HappyPath_test_report.json
```

---

## Output Report Structure

```json
{
  "run_started": "2026-03-23T10:00:00",
  "app_path": "C:\\path\\to\\OpenWaterApp.exe",
  "results": {
    "subject_id": { ... },
    "notes": { ... },
    "sensor_duration": { ... },
    "analyze": { ... }
  },
  "run_finished": "2026-03-23T10:45:00"
}
```

Only `HappyPath_test_report.json` is produced — individual per-test reports are automatically deleted after being merged.

---

## Test Details

### Test 1 — Subject ID (`SubjectIDwithJson.py`)
- Enters various Subject ID values (valid and invalid)
- Checks that the app correctly accepts or rejects each input
- Test cases include: special characters (`&`, `.`, `?`, `@`, `/`), spaces, `<>`, `#`

### Test 2 — Notes (`Notes.py`)
- Types into the Notes text area using 4 cases: empty, single line, multiline, 500-character string
- Verifies no exception is thrown during input

### Test 3 — Sensor Duration (`Sensorduration.py`)
- Cycles through Left and Right sensor dropdowns (3 steps down, 7 steps up)
- Moves duration slider to min (16s) and max (1800s) and verifies values
- Checks Start Scan button exists and is enabled
- If Start Scan is clicked, waits for `scan_duration + 100` seconds automatically

### Test 4 — Analyze (`Analyze.py`)
- Navigates to the Analyze page
- Selects first scan from dropdown
- Clicks Visualize BFI/BVI and closes the window
- Clicks Visualize Contrast/Mean and closes the window

---

## Optional: Run Without Building EXE

If Python and dependencies are already installed, run directly:

```powershell
cd tests
python combined_runner.py --app-path "C:\path\to\OpenWaterApp.exe"
```

Or set the environment variable instead:

```powershell
$env:APP_PATH = "C:\path\to\OpenWaterApp.exe"
python combined_runner.py
```

---

## Dependencies

```
pywinauto
comtypes
pyinstaller   (build only)
```

Install with:
```powershell
pip install pywinauto comtypes
```
