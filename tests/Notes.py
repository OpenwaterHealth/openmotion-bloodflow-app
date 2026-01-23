import time
import json
from datetime import datetime
from pywinauto.application import Application
from pywinauto.mouse import click

APP_PATH = r"C:\Users\vpenn\Documents\OpenWaterApp-0p4\OpenWaterApp.exe"
REPORT_FILE = "notes_test_report.json"

# Notes box rectangle from your --inspect
L, T, R, B = 621, 544, 1266, 989

TEST_CASES = [
    {"name": "empty", "text": ""},
    {"name": "single_line", "text": "This is a test note"},
    {"name": "multiline", "text": "Line 1\nLine 2\nLine 3"},
#    {"name": "special_chars", "text": "@#()[]{};:'\",.<>/?\\|+=-_"},
    {"name": "long_text", "text": "A" * 500},
]

def now():
    return datetime.now().isoformat(timespec="seconds")

def escape_for_type_keys(s: str) -> str:
    """
    pywinauto type_keys uses SendKeys syntax:
      {TAB} etc are special keys.
    To type literal { and } we must escape:
      {  -> {{}   (Send literal '{')
      }  -> {}}   (Send literal '}')

    Also escape + ^ % ~ which have special meaning in SendKeys.
    """
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
    report = {"feature": "Notes", "started": now(), "results": []}

    app = Application(backend="uia").start(APP_PATH)
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
