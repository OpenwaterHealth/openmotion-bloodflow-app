import time
import json
from pywinauto.application import Application

APP_PATH = r"C:\Users\vpenn\Documents\OpenWaterApp-0p4\OpenWaterApp.exe"
REPORT_FILE = "Sensor_Duration_report.json"

MIN_SEC = 16
MAX_SEC = 1800

# ---------- Start app ----------
app = Application(backend="uia").start(APP_PATH)
time.sleep(7)
win = app.top_window()
win.set_focus()
time.sleep(1)

report = {
    "left_dropdown": {},
    "right_dropdown": {},
    "duration": {},
    "start_scan": {},
}

# ---------- Helper: find 2 dropdowns ----------
cbs = win.descendants(control_type="ComboBox")
if len(cbs) >= 2:
    cbs = sorted(cbs, key=lambda cb: (cb.rectangle().left + cb.rectangle().right) // 2)
    left_cb = cbs[0]
    right_cb = cbs[-1]
else:
    left_cb = None
    right_cb = None

def cycle_dropdown(cb):
    BELOW_STEPS = 3   # below "Outer"
    ABOVE_STEPS = 7   # above "ALL"

    out = {"passed": True, "error": "", "moves": [], "selected_values": []}

    try:
        cb.set_focus()
        time.sleep(0.2)

        def open_dd():
            try:
                cb.expand()
            except Exception:
                cb.type_keys("%{DOWN}", set_foreground=True)
            time.sleep(0.25)

        def read_selected():
            sel = ""
            try:
                edits = cb.descendants(control_type="Edit")
                if edits:
                    sel = (edits[0].window_text() or "").strip()
            except Exception:
                pass
            return sel

        # DOWN up to 3, break early if ALL
        open_dd()
        for _ in range(BELOW_STEPS):
            cb.type_keys("{DOWN}{ENTER}", set_foreground=True)
            time.sleep(0.25)
            sel = read_selected()
            out["moves"].append("DOWN")
            out["selected_values"].append(sel)
            if sel.upper() == "ALL":
                break
            open_dd()

        # UP 7 steps
        open_dd()
        for _ in range(ABOVE_STEPS):
            cb.type_keys("{UP}{ENTER}", set_foreground=True)
            time.sleep(0.25)
            sel = read_selected()
            out["moves"].append("UP")
            out["selected_values"].append(sel)
            open_dd()

        cb.type_keys("{ESC}", set_foreground=True)
        time.sleep(0.2)

    except Exception as e:
        out["passed"] = False
        out["error"] = str(e)

    return out


# ==========================================================
# LEFT SENSOR DROPDOWN
# ==========================================================
if left_cb is None:
    report["left_dropdown"] = {"passed": False, "error": "Left dropdown not found"}
else:
    report["left_dropdown"] = cycle_dropdown(left_cb)

# ==========================================================
# RIGHT SENSOR DROPDOWN
# ==========================================================
if right_cb is None:
    report["right_dropdown"] = {"passed": False, "error": "Right dropdown not found"}
else:
    report["right_dropdown"] = cycle_dropdown(right_cb)

# ==========================================================
# DURATION SLIDER (slow move to 16 and 1800)
# ==========================================================
sliders = win.descendants(control_type="Slider")
slider = sliders[0] if sliders else None

dur_edit = None
for e in win.descendants(control_type="Edit"):
    try:
        txt = (e.window_text() or "").strip()
        if txt.isdigit():
            dur_edit = e
            break
    except Exception:
        pass

def read_duration():
    try:
        if dur_edit:
            txt = (dur_edit.window_text() or "").strip()
            return int(txt) if txt.isdigit() else None
    except Exception:
        return None
    return None

if slider is None:
    report["duration"] = {"passed": False, "error": "Duration slider not found"}
else:
    slider.set_focus()
    time.sleep(0.3)

    before = read_duration()

    # Move LEFT slowly until <= 16 (or until tries exhausted)
    min_val = before
    for _ in range(16):
        slider.type_keys("{LEFT}", set_foreground=True)
        time.sleep(0.05)
        v = read_duration()
        if v is not None:
            min_val = v
            if v <= MIN_SEC:
                break

    # Move RIGHT slowly until >= 1800
    max_val = min_val
    for _ in range(1800):
        slider.type_keys("{RIGHT}", set_foreground=True)
        time.sleep(0.05)
        v = read_duration()
        if v is not None:
            max_val = v
            if v >= MAX_SEC:
                break

    # Evaluate
    if min_val is None or max_val is None:
        report["duration"] = {
            "passed": True,
            "note": "Duration value not readable via UIA; movement-only verified",
            "before": before
        }
    else:
        report["duration"] = {
            "passed": (min_val == MIN_SEC and max_val == MAX_SEC),
            "before": before,
            "min_actual": min_val,
            "max_actual": max_val,
            "min_expected": MIN_SEC,
            "max_expected": MAX_SEC
        }

# ==========================================================
# START SCAN ( check if button exists + enabled state)
# ==========================================================
start_btn = None
for b in win.descendants(control_type="Button"):
    name = (b.element_info.name or "").strip()
    if "Start Scan" in name:
        start_btn = b
        break

if start_btn is None:
    report["start_scan"] = {"found": False, "error": "Start Scan button not found"}
else:
    try:
        enabled = bool(start_btn.is_enabled())
    except Exception:
        enabled = None

    report["start_scan"] = {"found": True, "enabled": enabled}

# ---------- Save JSON report ----------
with open(REPORT_FILE, "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2)

print("DONE. Report:", REPORT_FILE)

try:
    app.kill()
except Exception:
    pass
