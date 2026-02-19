import time
import json
import os
import argparse
from datetime import datetime
from pathlib import Path
from pywinauto.application import Application
from pywinauto.mouse import click

# Default app path - can be overridden with APP_PATH environment variable
DEFAULT_APP_PATH = os.getenv("APP_PATH", r".\OpenWaterApp.exe")

def find_app_path():
    """Find OpenWaterApp.exe using the default path"""
    
    # Check if default path exists (relative or absolute)
    default_path = Path(DEFAULT_APP_PATH)
    if default_path.exists():
        print(f"Using app path: {default_path.absolute()}")
        return str(default_path.absolute())
    
    raise FileNotFoundError(f"OpenWaterApp.exe not found at: {DEFAULT_APP_PATH}. Set APP_PATH environment variable or place OpenWaterApp.exe in same directory as script.")

REPORT_FILE = "notes_test_report.json"

# Notes box rectangle from your --inspect
L, T, R, B = 621, 544, 1266, 989

TEST_CASES = [
    {"name": "empty", "text": ""},
    {"name": "single_line", "text": "This is a test note"},
    {"name": "multiline", "text": "Line 1\nLine 2\nLine 3"},
    {"name": "long_text", "text": "A" * 500},
]

def now():
    return datetime.now().isoformat(timespec="seconds")

def escape_for_type_keys(s: str) -> str:    # Escape special characters for type_keys
    s = s.replace("{", "{{}")   # literal {
    s = s.replace("}", "{}}")   # literal }
    s = s.replace("+", "{+}")
    s = s.replace("^", "{^}")
    s = s.replace("%", "{%}")
    s = s.replace("~", "{~}")
    return s

def type_text_in_notes(win, text: str):
    # Click center of Notes every time (ensures focus)
    x = (L + R) // 2
    y = (T + B) // 2
    click(coords=(x, y))
    time.sleep(0.2)

    # Clear
    win.type_keys("^a{BACKSPACE}", set_foreground=True)
    time.sleep(0.2)

    if not text:
        return
       

    # Type line by line to support multiline reliably
    for i, line in enumerate(text.split("\n")):
        safe = escape_for_type_keys(line)
        if safe:
            win.type_keys(safe, with_spaces=True, set_foreground=True)
        if i < len(text.split("\n")) - 1:
            win.type_keys("{ENTER}", set_foreground=True)
        time.sleep(0.05)
        

def main():
    parser = argparse.ArgumentParser(description='Open-MOTION BloodFlow Notes Test Script')
    parser.add_argument('--app-path', type=str, help='Path to OpenWaterApp.exe')
    parser.add_argument('--run', action='store_true', help='Run the test')
    
    args = parser.parse_args()
    
    # Determine which app path to use
    if args.app_path:
        # Use command-line argument if provided
        app_path = args.app_path
        print(f"Using app path from argument: {app_path}")
    else:
        # Use default path detection
        app_path = find_app_path()
    
    report = {"feature": "Notes", "started": now(), "results": []}

    app = Application(backend="uia").start(app_path)
    time.sleep(6)

    win = app.top_window()
    win.set_focus()
    time.sleep(1)

    for tc in TEST_CASES:
        result = {"case": tc["name"], "input_length": len(tc["text"]), "passed": True, "error": ""}

        try:
            type_text_in_notes(win, tc["text"])
            # PASS criteria: typing did not throw an exception
            # (Qt/QML often doesn't allow reliable read-back via UIA)
            time.sleep(0.5)
        except Exception as e:
            result["passed"] = False
            result["error"] = str(e)

        report["results"].append(result)
        print(f"{tc['name']}: {'PASS' if result['passed'] else 'FAIL'}")

    report["finished"] = now()

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(f"\nJSON report saved: {REPORT_FILE}")

    try:
        app.kill()
    except Exception:
        pass

if __name__ == "__main__":
    main()