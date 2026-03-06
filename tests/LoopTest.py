import time
import json
import argparse
import os
from datetime import datetime
from pywinauto.application import Application


# ----------------------- DEFAULTS -----------------------
DEFAULT_APP_PATH = os.getenv("APP_PATH", r".\OpenWaterApp.exe")
DEFAULT_CYCLES = 2 # number of app launches
DEFAULT_SCANS_PER_CYCLE = 5 # number of scans per app launch

DEFAULT_DURATION_SEC = 600  # set slider to 10 minutes
DEFAULT_WAIT_SEC = 600      # wait 10 minutes after clicking Start Scan

DEFAULT_LAUNCH_WAIT_SEC = 150  # wait for app to launch and be ready
DEFAULT_LOG_FILE = "looprun_scan_log.json"  

TARGET_OPTION_TEXT = "ALL"  # dropdown option for 16 cameras

SLIDER_STEP_DELAY = 0.05    # increase to slow slider movement
# -------------------------------------------------------


# ----------------------- LOGGING ------------------------
def now_iso():
    return datetime.now().isoformat(timespec="seconds")

def log_event(log_path, event): 
    event["ts"] = now_iso()
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")

def safe_sleep(seconds, log_path=None, label="sleep"):
    remaining = int(seconds)
    while remaining > 0:
        chunk = 5 if remaining >= 5 else remaining
        time.sleep(chunk)
        remaining -= chunk
        if log_path:
            log_event(log_path, {"event": label, "remaining_sec": remaining})
# -------------------------------------------------------


# ----------------------- UI HELPERS ---------------------
def launch_app(app_path, launch_wait_sec, log_path):
    if not os.path.exists(app_path):
        raise FileNotFoundError(f"App not found: {app_path}")

    log_event(log_path, {"event": "app_launch_start", "app_path": app_path})
    app = Application(backend="uia").start(app_path)
    time.sleep(launch_wait_sec)

    win = app.top_window()
    win.set_focus()
    time.sleep(1)

    log_event(log_path, {"event": "app_launch_ready", "window_title": (win.window_text() or "")})
    return app, win

def kill_app(app, log_path):
    try:
        log_event(log_path, {"event": "app_kill"})
        app.kill()
        time.sleep(2)
    except Exception as e:
        log_event(log_path, {"event": "app_kill_error", "error": str(e)})

def find_left_right_dropdowns(win): 
    cbs = win.descendants(control_type="ComboBox")
    if len(cbs) < 2:
        return None, None
    cbs = sorted(cbs, key=lambda cb: (cb.rectangle().left + cb.rectangle().right) // 2)
    return cbs[0], cbs[-1]

def open_dropdown(cb):
    try:
        cb.expand()
    except Exception:
        cb.type_keys("%{DOWN}", set_foreground=True)
    time.sleep(0.25)

def read_dropdown_selected_text(cb):
    try:
        edits = cb.descendants(control_type="Edit")
        if edits:
            return (edits[0].window_text() or "").strip()
    except Exception:
        pass
    return ""

def select_all_in_dropdown(cb, log_path, which):
    cb.set_focus()
    time.sleep(0.2)

    # Try type ALL + ENTER
    try:
        open_dropdown(cb) 
        cb.type_keys("^a", set_foreground=True) 
        cb.type_keys(TARGET_OPTION_TEXT, set_foreground=True) 
        cb.type_keys("{ENTER}", set_foreground=True) 
        time.sleep(0.4)

        selected = read_dropdown_selected_text(cb) 
        log_event(log_path, {"event": "dropdown_select_try", "which": which, "method": "type_ALL", "selected": selected})
        if selected.upper() == TARGET_OPTION_TEXT:
            return True, selected
    except Exception as e:
        log_event(log_path, {"event": "dropdown_select_error", "which": which, "method": "type_ALL", "error": str(e)})

    # Fallback: step down 
    try: 
        open_dropdown(cb) 
        for i in range(15):
            cb.type_keys("{DOWN}{ENTER}", set_foreground=True) 
            time.sleep(0.35)
            selected = read_dropdown_selected_text(cb)
            log_event(log_path, {"event": "dropdown_select_try", "which": which, "method": "down_steps", "step": i + 1, "selected": selected}) 
            if selected.upper() == TARGET_OPTION_TEXT:
                return True, selected
            open_dropdown(cb)
    except Exception as e:
        log_event(log_path, {"event": "dropdown_select_error", "which": which, "method": "down_steps", "error": str(e)})

    return False, read_dropdown_selected_text(cb)

def find_start_scan_button(win): 
    for b in win.descendants(control_type="Button"):
        name = (b.element_info.name or "").strip()
        if "Start Scan" in name:
            return b
    return None

def click_start_scan(win, log_path):
    btn = find_start_scan_button(win)
    if btn is None:
        log_event(log_path, {"event": "start_scan_not_found"})
        return False, "Start Scan button not found"

    try:
        enabled = bool(btn.is_enabled())
    except Exception:
        enabled = None

    log_event(log_path, {"event": "start_scan_state", "enabled": enabled})

    if enabled is False:
        return False, "Start Scan is disabled"

    try:
        btn.click_input()
        log_event(log_path, {"event": "start_scan_clicked"})
        return True, ""
    except Exception as e:
        log_event(log_path, {"event": "start_scan_click_error", "error": str(e)})
        return False, str(e)

def find_duration_slider(win): 
    sliders = win.descendants(control_type="Slider")
    return sliders[0] if sliders else None

def find_duration_edit(win): 
    # Best effort: first Edit control with digits only
    for e in win.descendants(control_type="Edit"):
        try:
            txt = (e.window_text() or "").strip()
            if txt.isdigit():
                return e
        except Exception:
            pass
    return None

def read_duration(edit):
    if edit is None:
        return None
    try:
        txt = (edit.window_text() or "").strip()
        return int(txt) if txt.isdigit() else None
    except Exception:
        return None

def set_duration_seconds(win, target_sec, log_path): 
    out = {"passed": True, "error": "", "before": None, "after": None, "target": target_sec}

    slider = find_duration_slider(win)
    if slider is None:
        out["passed"] = False
        out["error"] = "Duration slider not found"
        log_event(log_path, {"event": "duration_set_fail", **out})
        return out

    dur_edit = find_duration_edit(win)
    before = read_duration(dur_edit)
    out["before"] = before

    if before is None:
        out["passed"] = False
        out["error"] = "Duration value not readable via UIA (cannot set target reliably)"
        log_event(log_path, {"event": "duration_set_fail", **out})
        return out

    slider.set_focus()
    time.sleep(0.2)

    cur = before
    # cap steps to avoid infinite loops
    for step in range(5000): 
        if cur == target_sec:
            break

        if cur < target_sec:
            slider.type_keys("{RIGHT}", set_foreground=True)
        else:
            slider.type_keys("{LEFT}", set_foreground=True)

        time.sleep(SLIDER_STEP_DELAY)
        v = read_duration(dur_edit)
        if v is not None:
            cur = v

    out["after"] = cur
    out["passed"] = (cur == target_sec)
    if not out["passed"]:
        out["error"] = f"Could not set duration to {target_sec}. Ended at {cur}."

    log_event(log_path, {"event": "duration_set", **out})
    return out
# -------------------------------------------------------


def do_one_scan(win, log_path, scan_index_in_cycle, duration_sec):
    left_cb, right_cb = find_left_right_dropdowns(win)
    if left_cb is None or right_cb is None:
        log_event(log_path, {"event": "dropdowns_not_found"})
        return False, "Dropdowns not found"

    ok_l, sel_l = select_all_in_dropdown(left_cb, log_path, which="left")
    ok_r, sel_r = select_all_in_dropdown(right_cb, log_path, which="right")

    log_event(log_path, {
        "event": "dropdowns_selected",
        "scan_index_in_cycle": scan_index_in_cycle,
        "left_ok": ok_l, "left_selected": sel_l,
        "right_ok": ok_r, "right_selected": sel_r
    })

    if not ok_l or not ok_r:
        return False, "Could not select ALL in one or both dropdowns"

    dur = set_duration_seconds(win, duration_sec, log_path)
    if not dur["passed"]:
        return False, f"Duration set failed: {dur['error']}"

    ok_click, err = click_start_scan(win, log_path)
    if not ok_click:
        return False, f"Start Scan click failed: {err}"

    return True, ""


def main(): 
    parser = argparse.ArgumentParser() 
    parser.add_argument("--app-path", default=DEFAULT_APP_PATH) 
    parser.add_argument("--cycles", type=int, default=DEFAULT_CYCLES)
    parser.add_argument("--scans-per-cycle", type=int, default=DEFAULT_SCANS_PER_CYCLE)
    parser.add_argument("--duration-sec", type=int, default=DEFAULT_DURATION_SEC)
    parser.add_argument("--wait-sec", type=int, default=DEFAULT_WAIT_SEC)
    parser.add_argument("--launch-wait-sec", type=int, default=DEFAULT_LAUNCH_WAIT_SEC)
    parser.add_argument("--log", default=DEFAULT_LOG_FILE)
    args = parser.parse_args() 
    log_path = args.log
    log_event(log_path, {
        "event": "run_start",
        "app_path": args.app_path,
        "cycles": args.cycles,
        "scans_per_cycle": args.scans_per_cycle,
        "duration_sec": args.duration_sec,
        "wait_sec": args.wait_sec
    })

    app = None

    try:
        for cycle in range(1, args.cycles + 1):
            app, win = launch_app(args.app_path, args.launch_wait_sec, log_path)
            log_event(log_path, {"event": "cycle_start", "cycle": cycle})

            for scan_i in range(1, args.scans_per_cycle + 1):
                log_event(log_path, {"event": "scan_start", "cycle": cycle, "scan": scan_i})

                ok, err = do_one_scan(win, log_path, scan_index_in_cycle=scan_i, duration_sec=args.duration_sec)
                if not ok:
                    log_event(log_path, {"event": "scan_failed", "cycle": cycle, "scan": scan_i, "error": err})
                    break

                log_event(log_path, {"event": "scan_wait", "cycle": cycle, "scan": scan_i, "wait_sec": args.wait_sec})
                safe_sleep(args.wait_sec, log_path=log_path, label="wait_between_scans")

                log_event(log_path, {"event": "scan_done", "cycle": cycle, "scan": scan_i})

            log_event(log_path, {"event": "cycle_end", "cycle": cycle})

            if app is not None:
                kill_app(app, log_path)
                app = None

    except KeyboardInterrupt: 
        log_event(log_path, {"event": "run_interrupted_by_user"})
    except Exception as e:
        log_event(log_path, {"event": "run_crash", "error": str(e)})
    finally:
        if app is not None:
            kill_app(app, log_path)
        log_event(log_path, {"event": "run_end"})


if __name__ == "__main__": 
    main() 
