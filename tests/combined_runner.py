#!/usr/bin/env python3
"""
Combined BloodFlow Test Runner
Runs all tests in order and writes a single combined JSON report.
Order: SubjectIDwithJson -> Notes -> Sensorduration -> Analyze
"""

import sys
import os
import json
import time
import argparse
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── PyInstaller compatibility ──────────────────────────────────────────────────
# When running as a frozen .exe, add the bundle directory to sys.path so the
# bundled test modules (SubjectIDwithJson, Notes, etc.) are importable, and
# set working directory to the exe's folder so relative paths (e.g. .\OpenWaterApp.exe)
# resolve correctly.
if getattr(sys, "frozen", False):
    _bundle_dir = sys._MEIPASS
    os.chdir(os.path.dirname(sys.executable))
else:
    _bundle_dir = os.path.dirname(os.path.abspath(__file__))

if _bundle_dir not in sys.path:
    sys.path.insert(0, _bundle_dir)

# ── Module-level imports so PyInstaller detects and bundles them ───────────────
import SubjectIDwithJson
import Notes
import Sensorduration
import Analyze

COMBINED_REPORT_FILE = "combined_test_report.json"

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def find_app_path(provided: Optional[str] = None) -> str:
    if provided:
        p = Path(provided)
        if not p.exists():
            raise FileNotFoundError(f"OpenWaterApp.exe not found at: {provided}")
        return str(p.absolute())
    default = os.getenv("APP_PATH", r".\OpenWaterApp.exe")
    p = Path(default)
    if p.exists():
        return str(p.absolute())
    raise FileNotFoundError(
        f"OpenWaterApp.exe not found at: {default}. "
        "Set APP_PATH environment variable or place OpenWaterApp.exe in the same folder as this exe."
    )


def read_and_delete_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return {"error": f"Could not read report '{path}': {e}"}
    try:
        os.remove(path)
    except Exception:
        pass
    return data


def section(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


# ──────────────────────────────────────────────────────────────────────────────
# Individual test runners
# ──────────────────────────────────────────────────────────────────────────────

def run_subject_id_test(app_path: str) -> dict:
    report_path = "subject_id_test_report.json"
    section("TEST 1/4: Subject ID Validation")
    try:
        try:
            SubjectIDwithJson.run_subject_id_only(
                app_path,
                SubjectIDwithJson.SUBJECT_ID_HINT,
                SubjectIDwithJson.ERROR_HINT,
                report_path,
            )
        except SystemExit:
            pass  # run_subject_id_only raises SystemExit on failures; report is still written
    except Exception as e:
        print(f"  ERROR: {e}")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump({"error": str(e)}, f, indent=2)
    return read_and_delete_json(report_path)


def run_notes_test(app_path: str) -> dict:
    report_path = "notes_test_report.json"
    section("TEST 2/4: Notes")
    try:
        from pywinauto.application import Application

        now_ts = lambda: datetime.now().isoformat(timespec="seconds")
        report = {"feature": "Notes", "started": now_ts(), "results": []}

        app = Application(backend="uia").start(app_path)
        time.sleep(6)
        win = app.top_window()
        win.set_focus()
        time.sleep(1)

        for tc in Notes.TEST_CASES:
            result = {
                "case": tc["name"],
                "input_length": len(tc["text"]),
                "passed": True,
                "error": "",
            }
            try:
                Notes.type_text_in_notes(win, tc["text"])
                time.sleep(0.5)
            except Exception as e:
                result["passed"] = False
                result["error"] = str(e)
            report["results"].append(result)
            print(f"  {tc['name']}: {'PASS' if result['passed'] else 'FAIL'}")

        report["finished"] = now_ts()
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)

        try:
            app.kill()
        except Exception:
            pass

    except Exception as e:
        print(f"  ERROR: {e}")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump({"error": str(e)}, f, indent=2)

    return read_and_delete_json(report_path)


def run_sensor_duration_test(app_path: str) -> dict:
    report_path = "Sensor_Duration_report.json"
    section("TEST 3/4: Sensor Duration")
    try:
        Sensorduration.run_sensor_duration_test(app_path)
    except Exception as e:
        print(f"  ERROR: {e}")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump({"error": str(e)}, f, indent=2)
    return read_and_delete_json(report_path)


def run_analyze_test(app_path: str) -> dict:
    report_path = "analyze_test_report.json"
    section("TEST 4/4: Analyze")
    try:
        tester = Analyze.BloodFlowTester(app_path=app_path)
        tester.run_full_test()  # calls write_report() internally for complete runs
        # Write explicitly in case an early step caused run_full_test to return False
        if not os.path.exists(report_path):
            tester.write_report(report_path)
    except Exception as e:
        print(f"  ERROR: {e}")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump({"error": str(e)}, f, indent=2)
    return read_and_delete_json(report_path)


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Combined BloodFlow Test Runner – runs all 4 tests and writes one JSON report."
    )
    parser.add_argument("--app-path", type=str, help="Path to OpenWaterApp.exe")
    args = parser.parse_args()

    app_path = find_app_path(args.app_path)

    print("=" * 60)
    print("  Combined BloodFlow Test Runner")
    print("=" * 60)
    print(f"  App : {app_path}")
    print(f"  Time: {datetime.now().isoformat(timespec='seconds')}")
    print(f"\n  Running tests in order:")
    print("    1. SubjectIDwithJson")
    print("    2. Notes")
    print("    3. Sensorduration")
    print("    4. Analyze")

    combined = {
        "run_started": datetime.now().isoformat(),
        "app_path": app_path,
        "results": {},
        "run_finished": None,
    }

    combined["results"]["subject_id"]      = run_subject_id_test(app_path)
    combined["results"]["notes"]           = run_notes_test(app_path)
    combined["results"]["sensor_duration"] = run_sensor_duration_test(app_path)
    combined["results"]["analyze"]         = run_analyze_test(app_path)

    combined["run_finished"] = datetime.now().isoformat()

    with open(COMBINED_REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(combined, f, indent=2)

    print("\n" + "=" * 60)
    print("  ALL TESTS COMPLETE")
    print(f"  Combined report: {COMBINED_REPORT_FILE}")
    print("=" * 60)
    input("\nPress Enter to exit...")


if __name__ == "__main__":
    main()
