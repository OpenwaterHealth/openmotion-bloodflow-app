import os
import time
import re
import json
import argparse
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any
from datetime import datetime

from pywinauto.application import Application

# -----------------------------
# USER CONFIG (edit as needed)
# -----------------------------
DEFAULT_APP_PATH = os.getenv("APP_PATH", r".\OpenWaterApp.exe")  # change if running elsewhere

# Hint used to locate Subject ID field (matches control name or automation_id).
SUBJECT_ID_HINT = os.getenv("SUBJECT_ID_HINT", "Subject ID")

# Hint used to find validation/error text near the field (toast/label).
# Make this broad if you don't know the exact wording, e.g. "Subject" or "ID"
ERROR_HINT = os.getenv("ERROR_HINT", "Subject")

# Wait timings
LAUNCH_TIMEOUT_SEC = 20
UI_DELAY = 1.00

# Assumed Subject ID rule (edit to match the application)
# Example: 3..20 chars, A-Z a-z 0-9 _ - @
SUBJECT_ID_REGEX = re.compile(r"^[A-Za-z0-9_@-]{3,50}$")


@dataclass
class Case:
    name: str
    value: 1
    should_be_valid: bool
    expected_error_contains: Optional[str] = None


TESTS: List[Case] = [
    Case("valid_basicampersand", "SUBJ&01", True),
    Case("valid_period", "SUBJ.001", True),
    Case("valid_underscore", "SUBJ_?001", True),
    Case("valid_attherate", "SUBJ01@", True),
    Case("valid_slash", "SUBJ/\001", True),

    Case("greater/lessthan", "<AB>", False, "required"),
    Case("space_invalid", "SUB J01", False, "invalid"),
    Case("special_invalid", "SUB#J", False, "invalid"),
]


def launch(app_path: str):
    if not os.path.exists(app_path):
        raise FileNotFoundError(
            f"EXE not found: {app_path}\n"
            f"Pass --app-path or set APP_PATH env var."
        )
    app = Application(backend="uia").start(app_path)
    start = time.time()
    win = None
    while time.time() - start < LAUNCH_TIMEOUT_SEC:
        try:
            win = app.top_window()
            win.wait("visible", timeout=2)
            break
        except Exception:
            time.sleep(0.5)
    if win is None:
        raise RuntimeError("Could not detect main window after launch.")
    win.set_focus()
    return app, win


def inspect_ui(win):
    print("\n=== UI TREE (control identifiers) ===\n")
    win.print_control_identifiers(depth=7)
    print("\nFind the Subject ID Edit control name/automation_id and set SUBJECT_ID_HINT.\n")


def find_subject_id_edit(win, hint: str):
    edits = win.descendants(control_type="Edit")
    if not edits:
        raise RuntimeError("No Edit controls found. Run with --inspect first.")

    h = hint.lower()
    for e in edits:
        try:
            name = (e.element_info.name or "").lower()
            auto_id = (e.element_info.automation_id or "").lower()
            if h in name or h in auto_id:
                return e
        except Exception:
            continue

    # Fallback (not ideal): first Edit control
    return edits[0]


def clear_and_type(edit, value: str):
    edit.set_focus()
    time.sleep(UI_DELAY)
    try:
        edit.type_keys("^a{BACKSPACE}", set_foreground=True)
    except Exception:
        pass
    time.sleep(UI_DELAY)
    if value:
        edit.type_keys(value, with_spaces=True, set_foreground=True)
    time.sleep(UI_DELAY)


def trigger_field_validation(edit):
    # Many apps validate on focus loss. TAB out.
    try:
        edit.type_keys("{TAB}", set_foreground=True)
    except Exception:
        pass
    time.sleep(UI_DELAY)


def collect_error_text(win, hint: str) -> str: # Look for Text controls containing the hint and return their combined text
    h = hint.lower()
    texts = win.descendants(control_type="Text")
    matches = []
    for t in texts:
        try:
            s = (t.element_info.name or "").strip()
            if s and h in s.lower():
                matches.append(s)
        except Exception:
            continue
    return " | ".join(matches)


def get_edit_value(edit) -> str:
    for meth in ("get_value", "window_text"):
        try:
            v = getattr(edit, meth)()
            if isinstance(v, str):
                return v
        except Exception:
            continue
    return ""


def write_json_report(path: str, report: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)


def run_subject_id_only(app_path: str, subject_hint: str, error_hint: str, report_path: str):
    run_started = datetime.now().isoformat(timespec="seconds")

    app, win = launch(app_path)
    edit = find_subject_id_edit(win, subject_hint)

    results: List[Dict[str, Any]] = []
    failures = 0

    print("\n=== Running Subject ID field-only validation tests (NO Save/Next click) ===")

    for tc in TESTS:
        print(f"\n[Test] {tc.name}")
        print(f"  Input: {repr(tc.value)} | Expect valid: {tc.should_be_valid}")

        clear_and_type(edit, tc.value)
        trigger_field_validation(edit)

        current = get_edit_value(edit)
        err = collect_error_text(win, error_hint)

        # Decide pass/fail:
        if tc.should_be_valid:
            passed = (err.strip() == "")
            if not passed:
                failures += 1
                print(f"  ❌ FAIL: Unexpected error shown: {err}")
            else:
                print("  ✅ PASS: No error shown")
        else:
            invalid_signal = bool(err.strip())

            if tc.expected_error_contains:
                invalid_signal = invalid_signal and (tc.expected_error_contains.lower() in err.lower())

            # Another invalid signal: app silently modifies input
            if not invalid_signal and current and current != tc.value:
                invalid_signal = True

            passed = invalid_signal
            if not passed:
                failures += 1
                print("  ❌ FAIL: No validation signal detected for invalid input.")
                print(f"     Field now: {repr(current)} | Error captured: {repr(err)}")
            else:
                print(f"  ✅ PASS: Validation detected. Error: {repr(err)} | Field now: {repr(current)}")

        results.append({
            "test_name": tc.name,
            "input": tc.value,
            "expected_valid": tc.should_be_valid,
            "expected_error_contains": tc.expected_error_contains,
            "actual_field_value": current,
            "captured_error_text": err,
            "passed": passed,
        })

    report = {
        "meta": {
            "app_path": app_path,
            "subject_id_hint": subject_hint,
            "error_hint": error_hint,
            "run_started": run_started,
            "run_finished": datetime.now().isoformat(timespec="seconds"),
        },
        "summary": {
            "total": len(TESTS),
            "passed": len(TESTS) - failures,
            "failed": failures,
        },
        "results": results,
    }

    # Write JSON report
    write_json_report(report_path, report)
    print(f"\n🧾 JSON report written to: {report_path}")

    # Close app
    try:
        app.kill()
    except Exception:
        pass

    if failures:
        raise SystemExit(f"{failures} test(s) failed. See report: {report_path}")
    print("All tests passed.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--app-path", default=os.getenv("APP_PATH", DEFAULT_APP_PATH))
    p.add_argument("--inspect", action="store_true")
    p.add_argument("--run", action="store_true")
    p.add_argument("--subject-hint", default=SUBJECT_ID_HINT)
    p.add_argument("--error-hint", default=ERROR_HINT)
    p.add_argument("--report", default=os.getenv("REPORT_PATH", "subject_id_test_report.json"),
                   help="Output JSON report path")
    args = p.parse_args()

    if args.inspect:
        app, win = launch(args.app_path)
        inspect_ui(win)
        try:
            app.kill()
        except Exception:
            pass
        return

    if args.run:
        run_subject_id_only(args.app_path, args.subject_hint, args.error_hint, args.report)
    else:
        print("Use --inspect to find controls or --run to execute tests.")


if __name__ == "__main__":
    main()
