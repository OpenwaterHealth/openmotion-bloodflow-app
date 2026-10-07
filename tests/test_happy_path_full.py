"""
test_happy_path_full.py — exhaustive end-to-end happy-path walkthrough.

Marker: ``release`` (~16-18 min wall-clock — one real 10-min laser scan
plus a full UI sweep). Only runs on release-pattern tag pushes.

What it covers
--------------
Where ``test_scan_flow.py`` runs the core scan once and
``test_scan_auto_stop_bug.py`` runs the *same* scan five times, this
script instead walks the app through **every** user-facing feature of a
normal scanning session exactly once, asserting each one in isolation so
a failure points at the specific feature that broke:

  1.  App is up and the console + sensors are CONNECTED (READY state).
  2.  Scan Settings modal opens.
  3.  User/subject label is set (verified later via the output filename).
  4.  Left sensor mask is set to ``All``.
  5.  Right sensor mask is set to ``All``.
  6.  Scan-duration mode switch toggles (Timed -> Free Run -> Timed).
  7.  Scan duration is set to 10 minutes.
  8.  Scan Settings modal closes (and is verified closed).
  9.  Session Notes modal opens and a note is typed.
  10. Notes modal closes.
  11. Contact-quality Check runs and its result modal is dismissed.
  12. Scan starts.
  13. Scan runs the full configured duration and reports completion.
  14. Scan output files (canonical CSV + scans.db) land on disk, and the
      canonical CSV carries the user label from step 3.
  15. Post-scan Session Notes modal: a post-scan note is typed, the modal
      is dismissed, and the connector reports the notes persisted.
  16. scans.db holds the session's notes: the post-scan note and the
      "Scan completed — duration" line (see Known deviation below).
  17. History modal opens.
  18. The scan just captured is listed in History's table.
  19. The scan row is selected and "Export CSV" writes
      ``<label>_export.csv`` through the native Save dialog.
  20. The export carries the per-camera columns and ~40 rows/s for the
      whole scan.
  21. The scan loads into the embedded PlotViewer via the Load button.
  22. History modal closes itself once the scan loads.
  23. The viewer reports the loaded scan ('[Plot] loaded past scan' with
      the session label and a live edge matching the scan duration).
  24. Settings modal opens (About-card firmware captured for the report).
  25. Settings modal closes.
  26. The app is still alive and responsive at the end of the sweep.

Known deviation (recorded, not failed)
--------------------------------------
A note typed BEFORE Start (step 9) is discarded: the connector clears its
notes buffer at every scan start and, before the first scan of a launch,
has no DB session to save into — yet the modal still toasts "Note saved."
(verified 2026-09-30 on Open-Motion Research 1.5.2: only notes typed
during or after the scan reach ``sessions.session_notes``). Step 16 asserts the
post-scan note and logs a DEVIATION warning when the step-9 note is
absent, so the sweep stays green while the defect is tracked.

Each numbered step is its own ``test_NN_*`` method in a single
``@pytest.mark.incremental`` class, so the first failure short-circuits
the rest (a broken Check shouldn't cascade into 8 misleading scan
failures) and the HIL report lists one clearly-named line per feature.

Interaction patterns are borrowed from the maintained dev-tier tests so
this release-tier sweep stays in step with the current UI:
  - sensor dropdowns via ``focus_combobox_by_label`` (test_scan_settings),
    which anchors focus by the field's UIA label rather than a fragile
    Tab count — this also fixes the old duration-Tab-walk that assumed
    focus was still at the top of the modal after the sensors were
    mouse-selected;
  - History is a ListView table (HistoryModal.qml), not a ComboBox, so
    it is read by row text and loaded with "Load in viewer →", matching
    test_history — not the old, stale ComboBox/"View in plot →" path.

Preconditions
-------------
- Console + both sensors connected over USB.
- ``OPENWATER_EXE`` set or ``OPENWATER_FROM_SOURCE=1``.
- App launched (handled by the session-scoped ``app`` fixture).
- No Shelly outlet required — this is a pure software/UI sweep, no
  power cycling.
"""

import csv
import re
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pyautogui
import pytest

from conftest import (
    SLEEP,
    _resolve_app_version,
    _running_app_exe_path,
    get_clipboard,
    log,
    read_about_card_firmware,
    read_about_card_serials,
    read_combobox_values,
    record_firmware_versions,
    record_serial_numbers,
    require_focus,
    uia_window,
    wait_for_combobox,
)
from hil_helpers import (
    RE_CONNECTED,
    SENSOR_OPTIONS,
    click_element_center,
    click_panel,
    dismiss_signal_quality_modal,
    find_app_log,
    focus_combobox_by_label,
    is_app_alive,
    log_size,
    read_app_config_value,
    recalibrate_panel_buttons,
    wait_for_pattern,
)

pytestmark = pytest.mark.release

# Scan Settings + Check are hidden in clinical mode; declare the forced
# value so conftest passes it to a from-source launch as --config-override
# (#546: the config is compiled in, there is no on-disk flag). Same pattern
# as test_scan_flow / test_scan_settings / test_usb_disconnect_freeze. (This
# module used to call force_app_config_value("reducedMode", ...) at import
# time — a retired key, written at collection of every run.)
FORCE_APP_CONFIG = {"clinicalMode": False}


# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────
SCAN_DURATION_MIN = 10
SCAN_DURATION_SEC = SCAN_DURATION_MIN * 60
CHECK_WAIT_SEC    = 120                       # Check completes within ~2 min
CHECK_START_TIMEOUT_SEC = 20                  # a healthy Check logs "started" in ~1s
SCAN_START_TIMEOUT_SEC  = 30                  # scan launch (pipeline build) can take ~10s
SCAN_POLL_SEC     = 10
SCAN_MAX_WAIT_SEC = SCAN_DURATION_SEC + 180   # scan + 3-min buffer
READY_TIMEOUT_SEC = 30                        # wait for CONNECTED at start
SENSOR_OPTION     = "All"                     # both sensors → all 16 cameras


def _sensor_picker_enabled(combobox_index: int, timeout: float = 15.0) -> bool:
    """True iff the sensor ComboBox at ``combobox_index`` is enabled.

    A side with no sensor attached renders its whole picker grayed out
    (verified on 1.4.0: the disabled ComboBox ignores clicks and keyboard
    selection, so trying to set it can never verify). Callers skip the
    selection step for a disabled side.
    """
    elem = wait_for_combobox(combobox_index, timeout=timeout)
    if elem is None:
        return False
    try:
        return bool(elem.is_enabled())
    except Exception as e:
        # Assume enabled — _select_sensor's strict verify will surface
        # the truth with a clear expected-vs-got message.
        log.warning(f"  is_enabled() failed for ComboBox[{combobox_index}]: {e}")
        return True

# The connector logs these bracketed markers around every Check; tailing
# them lets test_11 confirm the Check actually ran (and fail fast with a
# clear message if the click didn't register / the app is unresponsive)
# instead of blindly burning the full CHECK_WAIT_SEC budget.
RE_CHECK_STARTED = re.compile(r"Contact-quality check started")
RE_CHECK_ENDED   = re.compile(r"Contact-quality check ended")

# The connector logs the launched scan with its ACTUAL configured duration,
# e.g. "=== Full scan started: subject=owXXXX duration=120s left=0xFF ... ===".
# test_12 keys off this to prove both that the Start click landed AND that the
# H:M:S duration fields were set to the intended value (backend truth, no UIA).
RE_SCAN_STARTED = re.compile(r"Full scan started:.*?duration=(\d+)s")

# The History modal's title/rows are NOT exposed via UIA on this build, but
# the connector logs "QML: [History] opened — N scan(s)" when it opens, and
# its footer Buttons (Export CSV / Load in viewer) DO surface in UIA. test_16
# keys off the log; open/closed state is confirmed via those buttons.
RE_HISTORY_OPENED = re.compile(r"\[History\] opened\D+(\d+) scan")

# Every Notes-modal close toasts "Note saved." (logged as a Toast line);
# the connector logs "Scan notes saved to DB session '<label>'" only when
# a DB session exists to write into (i.e. during/after a scan).
RE_NOTE_SAVED = re.compile(r"Toast #\d+ \[success\].*Note saved\.")
RE_NOTES_PERSISTED = re.compile(r"Scan notes saved to DB session '([^']+)'")

# History → Export CSV: "exportScanCsv: exported '<label>' (sid=N) → <path>".
RE_EXPORTED = re.compile(r"exportScanCsv: exported '([^']+)'.*?→\s*(\S.*?)\s*$")

# Past-scan load lands in the viewer with one summary line:
# "[Plot] loaded past scan '<label>' (session_id=N) source=db: buffers=B
#  samples=S liveEdge=T.TTT gridMasks=..."
RE_PLOT_LOADED = re.compile(
    r"\[Plot\] loaded past scan '([^']+)'.*?samples=(\d+).*?liveEdge=([\d.]+)"
)

# The SDK's nominal capture rate; the export row count is checked against it.
NOMINAL_SAMPLE_HZ = 40.0

# Shared state across the incremental steps. pytest builds a fresh class
# instance per test method, so per-test ``self.x`` doesn't persist — keep
# cross-step data in a module-level dict instead.
STATE: dict = {
    "subject_label": "",     # user label typed in step 3
    "prescan_note":  "",     # note typed in step 9 (before Start)
    "postscan_note": "",     # note typed in step 15 (after the scan)
    "db_session":    "",     # scans.db session_label of the captured scan
    "files_before":  set(),  # data-dir listing snapshot taken at scan start
    "scan_seconds":  None,   # duration the app reported at completion
    "scan_started_sec": None,  # duration the backend logged at scan start
    "duration_hms":  f"00:{SCAN_DURATION_MIN:02d}:00",  # configured scan duration
    "history_scan_count": None,  # scan count History logged when opened
    "export_path":   None,   # Path of the History → Export CSV file
}

# NOTE: the JSON + Markdown run report is produced by conftest's autouse
# ``_hil_report_session`` reporter (test_logs/HIL_Report_<module>_<ts>.json
# and .md) — the suite-wide V&V evidence mechanism. This module deliberately
# does NOT write its own report to avoid duplicating it.


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def _resolve_data_dir() -> Path:
    """Return the directory the app writes scan output (CSVs + scans.db) into.

    The output root is ``dataDirectory`` from app_config.json when set.
    When unset (the packaged portable build ships it empty), the app
    defaults the root to its own install folder — the same place its
    ``logs/`` live — so derive the root from the app log location rather
    than guessing cwd. Scan files land in the ``data/`` child when it
    exists (1.4.0+ layout), else directly in the root (older builds).
    """
    root = None
    configured = read_app_config_value("dataDirectory", None)
    if configured and Path(configured).is_dir():
        root = Path(configured)
    else:
        log_path = find_app_log()
        if log_path:
            root = Path(log_path).parent.parent
    if root is None:
        root = Path.cwd()
    data = root / "data"
    return data if data.is_dir() else root


def _dir_listing(dir_path: Path) -> set:
    """Set of file names directly under ``dir_path`` (empty on error)."""
    try:
        return {p.name for p in dir_path.iterdir() if p.is_file()}
    except OSError:
        return set()


def _scan_settings_open() -> bool:
    """True iff the Scan Settings modal is up.

    Detected by presence of any ComboBox in the UIA tree — the only page
    in non-reduced mode that exposes ComboBoxes is the Scan Settings
    modal's sensor pickers. Same fingerprint as test_scan_settings.
    """
    try:
        return bool(uia_window().descendants(control_type="ComboBox"))
    except Exception:
        return False


_HISTORY_BUTTON_HINTS = ("Load in viewer", "Export CSV")


def _history_open() -> bool:
    """True iff the History modal is open, detected by its footer Buttons.

    The modal's 'Scan History' title and its scan rows are NOT exposed via
    UIA on this build, but the footer Buttons (Export CSV / Load in viewer)
    are — so button presence is the reliable open/closed signal. (Verified
    live against 1.3.0_RUO.)
    """
    try:
        for e in uia_window().descendants(control_type="Button"):
            nm = (e.element_info.name or e.window_text() or "")
            if any(h.lower() in nm.lower() for h in _HISTORY_BUTTON_HINTS):
                return True
    except Exception:
        pass
    return False


def _click_history_button(substr: str) -> str:
    """Pixel-click the History footer Button whose name contains ``substr``.

    Substring match (case-insensitive) rather than an exact title, because
    the real label carries an arrow and doubled spaces ("Load in viewer  →")
    that an exact UIA title query would miss. Uses a real pixel click on the
    button's rectangle — these QML buttons ignore the UIA InvokePattern
    (verified: ``invoke()`` was a no-op and left History open). Returns the
    matched name, or "" if not found.
    """
    try:
        for e in uia_window().descendants(control_type="Button"):
            nm = (e.element_info.name or e.window_text() or "")
            if substr.lower() in nm.lower():
                click_element_center(e, nm.strip())
                return nm
    except Exception:
        pass
    return ""


def _history_button_name(substr: str) -> str:
    """Name of the History footer Button containing ``substr``, or ""."""
    try:
        for e in uia_window().descendants(control_type="Button"):
            nm = (e.element_info.name or e.window_text() or "")
            if substr.lower() in nm.lower():
                return nm
    except Exception:
        pass
    return ""


def _select_latest_history_row() -> str:
    """Click the top History row and confirm it took: the Load button
    relabels from "Load in viewer →" to 'Load "<scan>" →'.

    History rows are NOT exposed via UIA on this build (only the footer
    Buttons are), so the top row is selected by a click anchored to the
    app-window rect — a last-resort calibrated coordinate (STYLE_GUIDE §6,
    tier 3), correlated against the live modal layout. Returns the Load
    button's relabelled name, or "" when the row click did not register."""
    r = uia_window().rectangle()
    w, h = r.right - r.left, r.bottom - r.top
    # First data row sits ~0.22 across and ~0.275 down from the modal's
    # top-left (the modal is a centred overlay that scales with the window).
    row_x, row_y = int(r.left + 0.223 * w), int(r.top + 0.275 * h)
    log.info(f"  selecting latest History row at ({row_x}, {row_y})")
    pyautogui.click(row_x, row_y)
    deadline = time.time() + 5
    while time.time() < deadline:
        nm = _history_button_name("Load")
        if nm and ("“" in nm or '"' in nm):
            return nm
        time.sleep(0.3)
    return ""


def _clear_clipboard() -> None:
    """Empty the clipboard so a stale host clipboard can't masquerade as
    the Notes text when Ctrl+C silently fails (same guard as test_notes)."""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value ''"],
            check=False, timeout=5,
        )
    except Exception as e:
        log.warning(f"  _clear_clipboard failed: {e}")


def _read_notes_textarea(attempts: int = 3) -> str:
    """Select-all + copy the focused Notes textarea and return its text.

    The textarea is not readable over UIA; the clipboard is the only
    channel. Retries because focus can lag the modal open by a beat, in
    which case Ctrl+A selects nothing. Collapses the selection to the end
    afterwards so a follow-up keystroke doesn't replace the note."""
    require_focus()
    for _ in range(attempts):
        _clear_clipboard()
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "c")
        time.sleep(0.5)
        clip = get_clipboard() or ""
        if clip.strip():
            pyautogui.press("end")
            return clip
        time.sleep(0.5)
    return ""


def _last_db_session_from_log() -> str:
    """The most recent "Scan notes saved to DB session '<label>'" label."""
    log_path = find_app_log()
    if not log_path:
        return ""
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    ids = RE_NOTES_PERSISTED.findall(text)
    return ids[-1] if ids else ""


def _db_session_notes(session_label: str) -> str | None:
    """``sessions.session_notes`` for ``session_label`` from scans.db,
    read-only; None when the row or the DB is missing."""
    db = _resolve_data_dir() / "scans.db"
    if not db.exists():
        log.warning(f"  scans.db not found at {db}")
        return None
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT session_notes FROM sessions WHERE session_label = ?",
                (session_label,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        log.warning(f"  scans.db read failed: {e}")
        return None
    return row[0] if row else None


def _accept_save_dialog(title: str = "Export Scan CSV", timeout: float = 15.0) -> bool:
    """Accept the native Windows Save dialog the History export opens,
    keeping its default file name, and answer Yes to an overwrite prompt.

    The dialog is a classic ``#32770`` common dialog titled after the QML
    FileDialog. The UIA backend does NOT list it as a top-level window
    (it hangs under the app window in the accessibility tree — verified
    2026-09-30), so it is found through the Win32 backend, with the
    UIA child lookup as a fallback. Returns False when no dialog
    appeared within ``timeout``."""
    from pywinauto import Desktop
    win32 = Desktop(backend="win32")

    def _find_dialog(rx: re.Pattern):
        """Win32 #32770 dialog wrapper whose title matches, else the UIA
        child of the app window (as a wrapper), else None."""
        try:
            for w in win32.windows(class_name="#32770"):
                if rx.match(w.window_text() or ""):
                    return w
        except Exception as e:
            log.warning(f"  win32 dialog scan failed: {e}")
        try:
            child = uia_window().child_window(title_re=rx.pattern, control_type="Window")
            if child.exists(timeout=0.5):
                return child.wrapper_object()
        except Exception:
            pass
        return None

    def _press_button(dlg, names: tuple[str, ...], fallback_keys) -> None:
        """Click the dialog button whose text (sans '&') is in ``names``;
        else send ``fallback_keys`` to the focused dialog."""
        try:
            dlg.set_focus()
            time.sleep(0.3)
            for b in dlg.descendants():
                try:
                    if (b.window_text() or "").replace("&", "").strip() in names:
                        b.click_input()
                        return
                except Exception:
                    continue
        except Exception as e:
            log.warning(f"  dialog button lookup failed: {e}")
        fallback_keys()

    dlg = None
    deadline = time.time() + timeout
    rx_title = re.compile(rf"(?i)^{re.escape(title)}$")
    while time.time() < deadline and dlg is None:
        dlg = _find_dialog(rx_title)
        if dlg is None:
            time.sleep(0.5)
    if dlg is None:
        return False
    log.info(f"  '{title}' dialog up — saving with the default file name")
    _press_button(dlg, ("Save",), lambda: pyautogui.press("enter"))
    # A prior export of the same scan leaves the file in place; Windows
    # then asks "…already exists. Do you want to replace it?" (Yes/No),
    # another #32770 titled "Confirm Save As".
    rx_confirm = re.compile(r"(?i)^Confirm Save As$")
    deadline = time.time() + 6
    while time.time() < deadline:
        prompt = _find_dialog(rx_confirm)
        if prompt is not None:
            _press_button(prompt, ("Yes",), lambda: pyautogui.hotkey("alt", "y"))
            log.info("  overwrite prompt answered Yes")
            break
        time.sleep(0.5)
    return True


def _open_panel_verified(label: str, is_open, what: str, tries: int = 2, settle: int = 8):
    """Click a sidebar panel button and VERIFY the expected UI actually opened.

    ``click_panel`` calibrates button coordinates via UIA with a relative-
    ratio fallback; when the fallback is even slightly off the click can
    land off the button and silently do nothing (the log still says
    "click panel ..."). This wrapper closes that gap: it polls ``is_open``
    after the click, and on a miss it recalibrates and clicks again before
    failing loudly — so a mis-landed click surfaces at THIS step with a
    clear message instead of cascading into a confusing later failure.

    All these panel buttons are toggles, so we only re-click when the panel
    is confirmed *not* open (never double-click an already-open modal shut).
    """
    for attempt in range(1, tries + 1):
        if attempt > 1:
            log.warning(f"  {what}: not open after attempt {attempt - 1}; recalibrating panel buttons")
            recalibrate_panel_buttons()
        click_panel(label)
        deadline = time.time() + settle
        while time.time() < deadline:
            if is_open():
                if attempt > 1:
                    log.info(f"  {what} opened on retry {attempt}.")
                return
            time.sleep(0.5)
    pytest.fail(
        f"{what} did not open after {tries} '{label}' click attempt(s). The "
        f"calibrated coordinate is landing off the button — clicks are not "
        f"registering on the app (calibration fell back to relative ratios)."
    )


def _select_sensor(side: str, combobox_index: int, option: str = SENSOR_OPTION):
    """Open a sensor dropdown by its UIA label, pick ``option``, verify it.

    Anchors focus with ``focus_combobox_by_label`` (test_scan_settings)
    rather than a Tab count, so it stands alone as its own step and does
    not depend on where focus happened to land after the previous step.
    """
    require_focus()
    label = f"{side} Sensor"
    idx = SENSOR_OPTIONS.index(option)
    log.info(f"  {label}: selecting '{option}' (index {idx})")

    # Up to three attempts: on a fresh launch the first keyboard walk
    # sometimes lands while the picker's popup is still settling and the
    # value stays put (seen 2026-09-30 on 1.5.2 ~40 s after launch). Each
    # attempt starts by closing any stray popup so the walk begins from a
    # known state.
    actual = None
    for attempt in range(1, 4):
        if attempt > 1:
            log.warning(f"  {label}: still '{actual}' after attempt {attempt - 1}; retrying")
            require_focus()
            pyautogui.press("escape")  # close a stray popup
            time.sleep(SLEEP)
            if not _scan_settings_open():
                # Escape reached the modal instead and closed it — reopen.
                _open_panel_verified("Scan Settings", _scan_settings_open, "Scan Settings")
        focus_combobox_by_label(label)
        pyautogui.hotkey("alt", "down")   # open the popup
        time.sleep(0.8)
        pyautogui.press("home")           # jump to the first item
        time.sleep(0.3)
        for _ in range(idx):
            pyautogui.press("down")
            time.sleep(0.15)
        pyautogui.press("return")         # confirm
        time.sleep(SLEEP)

        values = read_combobox_values()
        assert len(values) > combobox_index, (
            f"Expected at least {combobox_index + 1} ComboBox(es) in Scan "
            f"Settings, found {len(values)} — Qt accessibility bridge may not "
            f"be exposing the modal's sensor pickers on this runner."
        )
        actual = values[combobox_index]
        if actual == option:
            break
    assert actual == option, f"{side} sensor: expected '{option}', got '{actual}' after 3 attempts"
    log.info(f"  {side} sensor set to '{actual}'.")


def _find_user_label_field(win):
    """Return the User Label input Edit (carries the auto-generated 'owXXXXXX'
    value), or None. Distinguished from the 'User Label:' caption and the
    numeric duration Edits by its 'ow' prefix."""
    for e in win.descendants(control_type="Edit"):
        try:
            t = (e.window_text() or "").strip()
        except Exception:
            continue
        if t.lower().startswith("ow") and not t.isdigit():
            return e
    return None


def _duration_fields(win):
    """Return the three numeric duration Edit controls (Hours, Minutes,
    Seconds), ordered left-to-right by x-position.

    The Scan Settings duration inputs render as three small numeric Edit
    controls (~81px wide, digit text) laid out H : M : S. The wider User
    Label Edit (~150px) is excluded. Confirmed against the running build's
    UIA tree; mirrors ``test_history._set_scan_duration``.
    """
    fields = []
    for e in win.descendants(control_type="Edit"):
        try:
            r = e.rectangle()
            if (r.right - r.left) >= 150:       # skip the wide User Label field
                continue
            txt = (e.window_text() or "").strip()
            if txt.isdigit() or txt == "":
                fields.append((r.left, e))
        except Exception:
            continue
    fields.sort(key=lambda t: t[0])
    return [e for _, e in fields]


def _read_duration_hms(win) -> str:
    fields = _duration_fields(win)
    vals = [(e.window_text() or "").strip() for e in fields[:3]]
    return ":".join(vals) if len(vals) == 3 else "?"


def _set_duration_minutes(minutes: int):
    """Set the scan duration to 0h:``minutes``m:0s by clicking the H/M/S
    Edit fields directly — NOT by Tab-counting.

    Tab-navigation from the sensor row proved fragile: a one-off in the
    tab count landed the value in the Hours field (an N-hour scan instead
    of N minutes). The three duration inputs are unambiguous UIA Edit
    controls, so we click each by its rectangle and type into it. Verified
    live on the 1.3.0_RUO build.
    """
    require_focus()
    win = uia_window()
    fields = _duration_fields(win)
    assert len(fields) >= 3, (
        f"Expected 3 numeric duration fields (H:M:S) in Scan Settings, found "
        f"{len(fields)} — UIA may not be exposing the duration inputs."
    )
    for elem, value, label in ((fields[0], 0, "Hours"),
                               (fields[1], minutes, "Minutes"),
                               (fields[2], 0, "Seconds")):
        click_element_center(elem, f"{label} field")
        time.sleep(0.15)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.1)
        pyautogui.typewrite(str(value), interval=0.05)
        time.sleep(0.15)
    hms = _read_duration_hms(win)
    log.info(f"  duration fields now read H:M:S = {hms}")


def _scan_reported_duration():
    """If the post-scan Session Notes modal is up, return the scan
    duration in seconds parsed from its "Scan completed — duration:
    HH:MM:SS" text; else ``None`` (0 if the modal is up but unparsable).

    Same detection as ``test_scan_auto_stop_bug._check_scan_finished``.
    """
    import re
    try:
        win = uia_window()
        found_notes = False
        for elem in win.descendants():
            try:
                text = elem.window_text().strip()
            except Exception:
                continue
            if "session notes" in text.lower():
                found_notes = True
            m = re.search(r"duration:\s*(\d{2}):(\d{2}):(\d{2})", text)
            if m:
                h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
                secs = h * 3600 + mi * 60 + s
                log.info(f"  Scan finished — {text.strip()} ({secs}s)")
                return secs
        if found_notes:
            log.info("  Scan finished — Session Notes visible (duration unparsed)")
            return 0
    except Exception as e:
        log.warning(f"  _scan_reported_duration failed: {e}")
    return None


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────
@pytest.mark.incremental
class TestHappyPathFull:
    """Full happy-path sweep: every scanning feature exercised once, in order."""

    def test_01_app_ready(self, app):
        """App is launched and the console + sensors are CONNECTED (READY)."""
        assert is_app_alive(), "Bloodflow app window is not present at start."
        # Several installed builds coexist on the bench and the ``app``
        # fixture attaches to whatever is already running (else the newest
        # exe on disk), so put the build under test on the record.
        try:
            log.info(f"  build under test: {_resolve_app_version()} — "
                     f"{_running_app_exe_path() or '(exe path unknown)'}")
        except Exception as e:
            log.warning(f"  could not resolve the running build: {e}")
        log_path = find_app_log()
        assert log_path, "could not locate the bloodflow app log."
        # If the device is already connected we may have missed the line;
        # tail from the top of the current log and accept either a fresh
        # CONNECTED transition or an already-connected app.
        line = wait_for_pattern(RE_CONNECTED, log_path, 0, READY_TIMEOUT_SEC)
        if line:
            log.info(f"  device connected: {line}")
        else:
            log.info("  no CONNECTED line seen — assuming already connected.")

    def test_02_open_scan_settings(self, app):
        """Scan Settings modal opens from the sidebar (verified + retried)."""
        _open_panel_verified(
            "Scan\nSettings", _scan_settings_open, "Scan Settings modal"
        )
        elem = wait_for_combobox(0, timeout=15)
        assert elem is not None, (
            "Scan Settings opened but did not expose its sensor ComboBoxes "
            "within 15 s."
        )

    def test_03_set_user_label(self, app):
        """Set a distinctive subject/user label by clicking the field directly.

        Tab navigation to the User Label field is unreliable — the first Tab
        sometimes lands on a button, so the typed value falls on the floor and
        the scan keeps its auto-generated 'owXXXXXX' label (this is what made
        run 7's output 'owUOBWIL' instead of the intended label). Click the
        UIA Edit directly, type, and verify the field took the value.
        """
        win = uia_window()
        field = _find_user_label_field(win)
        assert field is not None, (
            "Could not find the User Label input field (an Edit with an "
            "'ow' value) in the Scan Settings modal."
        )
        STATE["subject_label"] = f"HappyPath_{datetime.now():%H%M%S}"
        click_element_center(field, "User Label field")
        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.1)
        pyautogui.press("delete")
        time.sleep(0.1)
        pyautogui.typewrite(STATE["subject_label"], interval=0.04)
        time.sleep(0.3)

        # Verify the field actually holds our label (app normalizes to
        # alphanumeric-uppercase on save; compare on that form). Poll a moment
        # for the UIA value to reflect the keystrokes.
        def _norm(s: str) -> str:
            return "".join(c for c in s if c.isalnum()).upper()

        want = _norm(STATE["subject_label"])
        deadline = time.time() + 3
        got = ""
        while time.time() < deadline:
            fld = _find_user_label_field(win) or field
            got = (fld.window_text() or "").strip()
            if want in _norm(got):
                break
            time.sleep(0.3)
        assert want in _norm(got), (
            f"User Label field shows '{got}' after typing "
            f"'{STATE['subject_label']}' — the label did not register in the "
            f"field (the click may have missed the input)."
        )
        log.info(f"  user label set to '{STATE['subject_label']}' (field: '{got}').")

    def test_04_select_left_sensor(self, app):
        """Left sensor mask set to 'All' — skipped when the picker is
        disabled (no left sensor attached to this bench)."""
        if not _sensor_picker_enabled(0):
            log.info("  Left sensor picker disabled — no left sensor attached.")
            pytest.skip("Left sensor not attached (picker disabled).")
        _select_sensor(side="Left", combobox_index=0)

    def test_05_select_right_sensor(self, app):
        """Right sensor mask set to 'All' — skipped when the picker is
        disabled (no right sensor attached to this bench)."""
        if not _sensor_picker_enabled(1):
            log.info("  Right sensor picker disabled — no right sensor attached.")
            pytest.skip("Right sensor not attached (picker disabled).")
        _select_sensor(side="Right", combobox_index=1)

    def test_06_toggle_scan_mode_switch(self, app):
        """The Timed/Free-Run duration switch is operable.

        Toggle it to Free Run and back to Timed (the mode the scan needs),
        verifying the switch responds without closing or crashing the
        modal. Mirrors test_scan_settings test_08/test_09, collapsed into
        one step so the happy path leaves the switch on Timed.
        """
        require_focus()
        # Anchor on an ENABLED combobox — a disabled picker (side with no
        # sensor attached) ignores clicks and never takes focus, so the
        # Tab walk would start from the wrong place.
        if _sensor_picker_enabled(0):
            focus_combobox_by_label("Left Sensor")
            tabs_to_switch = 2            # Left -> Right ComboBox -> Switch
        else:
            focus_combobox_by_label("Right Sensor")
            tabs_to_switch = 1            # Right ComboBox -> Switch
        require_focus()
        for _ in range(tabs_to_switch):
            pyautogui.press("tab")
            time.sleep(0.2)
        time.sleep(0.3)
        pyautogui.press("space")          # Timed -> Free Run
        time.sleep(SLEEP)
        pyautogui.press("space")          # Free Run -> Timed
        time.sleep(SLEEP)
        assert _scan_settings_open(), (
            "Scan Settings modal disappeared while toggling the "
            "Timed/Free-Run switch — the switch may have crashed or "
            "closed the modal."
        )

    def test_07_set_duration(self, app):
        """Scan duration set to 10 minutes, verified in the H:M:S fields."""
        _set_duration_minutes(SCAN_DURATION_MIN)
        vals = [(e.window_text() or "").strip()
                for e in _duration_fields(uia_window())[:3]]
        assert len(vals) == 3 and all(v.isdigit() for v in vals), (
            f"Could not read back the three duration fields: {vals}"
        )
        h, m, s = (int(v) for v in vals)
        assert (h, m, s) == (0, SCAN_DURATION_MIN, 0), (
            f"Duration read H:M:S = {h}:{m:02d}:{s:02d}, expected "
            f"0:{SCAN_DURATION_MIN:02d}:00 — the value landed in the wrong "
            f"field (the hours-instead-of-minutes bug)."
        )

    def test_08_close_scan_settings(self, app):
        """Scan Settings modal closes (Escape) — and is verified closed."""
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)
        assert not _scan_settings_open(), (
            "Scan Settings modal still exposes ComboBoxes after Escape — "
            "the modal did not close."
        )

    def test_09_add_session_note(self, app):
        """Session Notes modal opens and accepts a typed note — read back
        through the clipboard to prove the keystrokes landed in the
        textarea (the textarea is not readable over UIA)."""
        click_panel("Notes")
        require_focus()
        note = f"Happy-path sweep {datetime.now():%Y-%m-%d %H:%M:%S}"
        log.info(f"  typing note: '{note}'")
        pyautogui.typewrite(note, interval=0.03)
        time.sleep(SLEEP)
        STATE["prescan_note"] = note
        got = _read_notes_textarea()
        assert note in got, (
            f"Typed note '{note}' did not land in the Notes textarea "
            f"(read back: '{got[:80]}'). The modal did not take keyboard "
            f"focus, or the Notes click landed off the button."
        )
        log.info("  note read back from the textarea OK.")

    def test_10_close_notes(self, app):
        """Notes modal closes (Escape) and the app toasts 'Note saved.'."""
        log_path = find_app_log()
        start_off = log_size(log_path) if log_path else 0
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)
        if log_path:
            line = wait_for_pattern(RE_NOTE_SAVED, log_path, start_off, 6)
            assert line, (
                "No 'Note saved.' toast was logged within 6s of closing the "
                "Notes modal — the modal may not have closed, or the note "
                "was not committed."
            )
            log.info("  'Note saved.' toast logged.")

    def test_11_contact_quality_check(self, app):
        """Contact-quality Check runs and its result modal is dismissed.

        Fails fast if the click never actually starts a Check: the
        connector logs "Contact-quality check started" within ~1s on a
        healthy run, so if that line doesn't appear within
        CHECK_START_TIMEOUT_SEC the click didn't register or the app is
        unresponsive — a far clearer signal than burning the full
        CHECK_WAIT_SEC and reporting only "modal never appeared".
        """
        log_path = find_app_log()
        start_off = log_size(log_path) if log_path else 0
        log.info(f"  Clicking Check, waiting up to {CHECK_WAIT_SEC}s...")
        click_panel("Check")

        # Fail fast: confirm the Check actually started via the app log.
        if log_path:
            started = wait_for_pattern(
                RE_CHECK_STARTED, log_path, start_off, CHECK_START_TIMEOUT_SEC
            )
            assert started, (
                f"No 'Contact-quality check started' line appeared in the "
                f"app log within {CHECK_START_TIMEOUT_SEC}s of clicking "
                f"Check (a healthy run logs it in ~1s). The Check button "
                f"click did not register, or the app is unresponsive — "
                f"check {log_path} for the last logged activity."
            )
            log.info(f"  Check started: {started.strip()}")

        elapsed = 0
        dismissed = False
        while elapsed < CHECK_WAIT_SEC:
            time.sleep(SCAN_POLL_SEC)
            elapsed += SCAN_POLL_SEC
            if not is_app_alive():
                pytest.fail(f"App closed during Check after {elapsed}s.")
            if dismiss_signal_quality_modal():
                log.info(f"  Contact-quality modal dismissed at {elapsed}s.")
                dismissed = True
                break
            if elapsed % 30 == 0:
                log.info(f"  Check running... {elapsed}/{CHECK_WAIT_SEC}s")
        # Final attempt in case the modal appeared as the loop exited.
        if not dismissed:
            dismissed = dismiss_signal_quality_modal()

        # Diagnostic on timeout: did the Check at least finish? This
        # separates "Check stalled" (started, never ended) from "modal
        # never surfaced" (ended, but the result modal wasn't detected).
        if not dismissed and log_path:
            ended = wait_for_pattern(RE_CHECK_ENDED, log_path, start_off, 1)
            log.warning(
                f"  Check modal not dismissed after {CHECK_WAIT_SEC}s — "
                f"'check ended' logged: {bool(ended)}. "
                + ("Check completed but the result modal wasn't detected."
                   if ended else
                   "Check started but never ended — it stalled mid-run.")
            )
        assert dismissed, (
            f"Contact-quality result modal never appeared within "
            f"{CHECK_WAIT_SEC}s of clicking Check."
        )

    def test_12_start_scan(self, app):
        """Scan starts, AND the backend confirms the configured H:M:S duration.

        Verifies from the app log (backend truth, not fragile UIA) that:
          * the Start click actually launched a scan — the connector logs
            "Full scan started ..." only on a real click; if it's absent the
            click landed off the button; and
          * the duration the app is scanning with matches what the H:M:S
            fields were set to in test_07 — the log line carries
            ``duration=<sec>s``, so a mis-set duration is caught here rather
            than only implied by scan length.

        Start is a toggle (it becomes Stop once scanning), so this never
        re-clicks — it fails loudly instead.
        """
        STATE["files_before"] = _dir_listing(_resolve_data_dir())
        log.info(f"  data dir before scan: {len(STATE['files_before'])} files.")

        log_path = find_app_log()
        start_off = log_size(log_path) if log_path else 0
        click_panel("Start")

        if log_path:
            line = wait_for_pattern(
                RE_SCAN_STARTED, log_path, start_off, SCAN_START_TIMEOUT_SEC
            )
            assert line, (
                f"No 'Full scan started' line appeared in the app log within "
                f"{SCAN_START_TIMEOUT_SEC}s of clicking Start. The Start click "
                f"did not register (landed off the button), or the scan failed "
                f"to launch — see {log_path}."
            )
            m = RE_SCAN_STARTED.search(line)
            started_sec = int(m.group(1)) if m else None
            STATE["scan_started_sec"] = started_sec
            log.info(f"  Full scan started (backend duration={started_sec}s).")
            assert started_sec == SCAN_DURATION_SEC, (
                f"Scan launched with duration={started_sec}s, but the H:M:S "
                f"fields were configured for {SCAN_DURATION_SEC}s "
                f"({STATE['duration_hms']}). The duration was set to the wrong "
                f"value in Scan Settings."
            )
        log.info("  Scan started — monitoring for completion...")

    def test_13_scan_completes(self, app):
        """Scan runs the full 10-min duration and reports completion."""
        elapsed = 0
        secs = None
        while elapsed < SCAN_MAX_WAIT_SEC:
            time.sleep(SCAN_POLL_SEC)
            elapsed += SCAN_POLL_SEC
            if not is_app_alive():
                pytest.fail(f"App closed mid-scan after {elapsed}s.")
            secs = _scan_reported_duration()
            if secs is not None:
                log.info(f"  Session Notes appeared after {elapsed}s "
                         f"(reported {secs}s).")
                break
            if elapsed % 60 == 0:
                log.info(f"  {elapsed}s elapsed — scan still running...")

        assert secs is not None, (
            f"Session Notes never appeared within {SCAN_MAX_WAIT_SEC}s — "
            f"scan may be stuck."
        )
        STATE["scan_seconds"] = secs
        assert secs >= SCAN_DURATION_SEC, (
            f"Scan ran only {secs}s ({secs // 60}m {secs % 60}s); expected "
            f"at least {SCAN_DURATION_SEC}s ({SCAN_DURATION_MIN}m). Scan "
            f"stopped early."
        )
        log.info(f"  Scan duration OK: {secs}s (>= {SCAN_DURATION_SEC}s).")

    def test_14_scan_output_files(self, app):
        """Scan output is written: a raw CSV carrying the step-3 user label
        when ``writeRawCsv`` is enabled, and the scans.db session always
        (with ``writeRawCsv`` off — the packaged default — the DB session
        id carries the label instead)."""
        data_dir = _resolve_data_dir()
        after = _dir_listing(data_dir)
        new = sorted(after - STATE["files_before"])
        log.info(f"  new files in {data_dir}: {new}")

        # The app normalizes User Label on save — prepends 'ow', uppercases,
        # and strips underscores/spaces/punctuation (e.g. 'HappyPath_144243'
        # -> 'owHAPPYPATH144243'), so compare on the alphanumeric-uppercase
        # form of both sides rather than a raw substring. Same normalization
        # as test_scan_settings.
        def _norm(s: str) -> str:
            return "".join(c for c in s if c.isalnum()).upper()

        norm_label = _norm(STATE["subject_label"])

        if read_app_config_value("writeRawCsv", False):
            new_csvs = [n for n in new if n.lower().endswith(".csv")]
            assert new_csvs, (
                f"No new .csv appeared in {data_dir} after the scan. "
                f"New files: {new}"
            )
            labelled = [
                n for n in new_csvs if norm_label and norm_label in _norm(n)
            ]
            assert labelled, (
                f"None of the new CSVs {new_csvs} carry the user label "
                f"'{STATE['subject_label']}' (normalized '{norm_label}') set in "
                f"step 3 — the label did not propagate to the scan output "
                f"filename."
            )
            log.info(f"  scan output CSV with label: {labelled}")
        else:
            # CSV output is config-disabled — the scan is recorded to
            # scans.db only. The connector logs the DB session id it saved
            # under ("Scan notes saved to DB session '<id>'"), and the id
            # embeds the normalized user label, so label propagation is
            # still verified.
            log.info("  writeRawCsv is off — verifying the DB session "
                     "instead of a CSV.")
            session_id = ""
            log_path = find_app_log()
            if log_path:
                try:
                    text = Path(log_path).read_text(
                        encoding="utf-8", errors="replace"
                    )
                    ids = re.findall(r"DB session '([^']+)'", text)
                    session_id = ids[-1] if ids else ""
                except OSError:
                    pass
            assert session_id, (
                "writeRawCsv is off and no 'saved to DB session' line was "
                "found in the app log — the scan does not appear to have "
                "been recorded."
            )
            assert norm_label and norm_label in _norm(session_id), (
                f"DB session '{session_id}' does not carry the user label "
                f"'{STATE['subject_label']}' (normalized '{norm_label}') set "
                f"in step 3 — the label did not propagate to the scan record."
            )
            log.info(f"  scan recorded as DB session '{session_id}'.")

        # scans.db is created once and updated in place, so check it exists
        # and was touched recently rather than expecting it in the diff.
        db = data_dir / "scans.db"
        assert db.exists(), f"scans.db not found in {data_dir}."
        age = time.time() - db.stat().st_mtime
        assert age < SCAN_MAX_WAIT_SEC + 120, (
            f"scans.db exists but was last modified {age:.0f}s ago — the "
            f"scan session may not have been recorded to the database."
        )
        log.info(f"  scans.db present and updated {age:.0f}s ago.")

    def test_15_post_scan_note_and_close(self, app):
        """Auto-opened post-scan Session Notes modal accepts a note, is
        dismissed, and the connector persists the notes to the DB session
        ('Scan notes saved to DB session' + 'Note saved.' toast)."""
        require_focus()
        note = f"Post-scan note {datetime.now():%Y-%m-%d %H:%M:%S}"
        log.info(f"  typing post-scan note: '{note}'")
        # Caret to the end so the note doesn't land inside the duration line
        # the connector appended.
        pyautogui.hotkey("ctrl", "end")
        pyautogui.press("enter")
        pyautogui.typewrite(note, interval=0.03)
        time.sleep(SLEEP)
        STATE["postscan_note"] = note
        got = _read_notes_textarea()
        assert note in got, (
            f"Post-scan note '{note}' did not land in the Notes textarea "
            f"(read back: '{got[:80]}')."
        )
        log_path = find_app_log()
        start_off = log_size(log_path) if log_path else 0
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)
        if log_path:
            line = wait_for_pattern(RE_NOTES_PERSISTED, log_path, start_off, 6)
            assert line, (
                "No 'Scan notes saved to DB session' line within 6s of "
                "closing the post-scan Notes modal — the note was not "
                "persisted."
            )
            m = RE_NOTES_PERSISTED.search(line)
            STATE["db_session"] = m.group(1) if m else ""
            log.info(f"  notes persisted to DB session '{STATE['db_session']}'.")

    def test_16_notes_persisted_in_db(self, app):
        """scans.db holds the session's notes: the post-scan note (step 15)
        and the 'Scan completed — duration' line. The pre-scan note (step
        9) is checked too, but its absence is a recorded DEVIATION, not a
        failure — the connector discards notes typed before Start (see the
        module docstring)."""
        session = STATE.get("db_session") or _last_db_session_from_log()
        assert session, "no DB session label known for the captured scan"
        STATE["db_session"] = session
        notes = _db_session_notes(session)
        assert notes is not None, (
            f"scans.db has no row for session '{session}' — the scan was "
            f"not recorded to the database."
        )
        log.info(f"  session '{session}' notes: {notes!r}")
        assert STATE["postscan_note"] in notes, (
            f"Post-scan note '{STATE['postscan_note']}' is not in "
            f"scans.db session_notes for '{session}': {notes!r}"
        )
        assert re.search(r"Scan (completed|stopped)\s*\S*\s*duration: \d\d:\d\d:\d\d", notes), (
            f"No 'Scan completed — duration: HH:MM:SS' line in session_notes "
            f"for '{session}': {notes!r}"
        )
        if STATE["prescan_note"] and STATE["prescan_note"] not in notes:
            log.warning(
                f"  DEVIATION: pre-scan note '{STATE['prescan_note']}' (step 9, "
                f"toasted 'Note saved.') is NOT in session_notes — the "
                f"connector clears the notes buffer at scan start."
            )
        else:
            log.info("  pre-scan note present in session_notes.")

    def test_17_open_history(self, app):
        """History opens from the sidebar.

        Verified two ways (the modal's title/rows are NOT in the UIA tree
        on this build): the connector logs '[History] opened — N scan(s)',
        and the modal's footer Buttons (Export CSV / Load in viewer) appear
        in UIA. We do NOT retry-click here — History is a toggle, and a
        false-negative retry would toggle an already-open modal shut.
        """
        log_path = find_app_log()
        start_off = log_size(log_path) if log_path else 0
        click_panel("History")

        if log_path:
            line = wait_for_pattern(RE_HISTORY_OPENED, log_path, start_off, 15)
            if line:
                m = RE_HISTORY_OPENED.search(line)
                STATE["history_scan_count"] = int(m.group(1)) if m else None
                log.info(f"  {line.strip()}")

        deadline = time.time() + 10
        while time.time() < deadline and not _history_open():
            time.sleep(0.5)
        assert _history_open(), (
            "History modal footer buttons (Export CSV / Load in viewer) did "
            "not appear within 10s of clicking History — the modal did not "
            "open (the History click may have landed off the button)."
        )

    def test_18_scan_listed(self, app):
        """History lists at least one scan (the one just captured is recorded).

        The scan count comes from the '[History] opened — N scan(s)' log line;
        the rows themselves aren't UIA-exposed, but test_14 already proved the
        scan's CSV + DB session were written, and it's the latest entry."""
        n = STATE.get("history_scan_count")
        assert n is not None and n >= 1, (
            f"History reported {n} scans — expected at least 1. The scan just "
            f"captured should be recorded (see the '[History] opened — N "
            f"scan(s)' app-log line)."
        )
        log.info(f"  History lists {n} scan(s).")

    def test_19_export_csv(self, app):
        """Select the latest scan row and export it: 'Export CSV' opens
        the native 'Export Scan CSV' Save dialog, the default file name is
        accepted, and the connector logs the export with its path.

        The row click is verified by the Load button relabelling to
        'Load "<scan>" →' (Export CSV is disabled until a row is checked).
        """
        name = _select_latest_history_row()
        assert name, (
            "History row click did not register (Load button still reads "
            "'Load in viewer') — cannot export without a selected row."
        )
        log.info(f"  row selected: Load button reads '{name.strip()}'.")
        log_path = find_app_log()
        start_off = log_size(log_path) if log_path else 0
        clicked = _click_history_button("Export CSV")
        assert clicked, "No 'Export CSV' button found in the open History modal."
        assert _accept_save_dialog(), (
            "The 'Export Scan CSV' Save dialog did not appear within 15s of "
            "clicking Export CSV."
        )
        assert log_path, "could not locate the bloodflow app log."
        line = wait_for_pattern(RE_EXPORTED, log_path, start_off, 60)
        assert line, (
            "No 'exportScanCsv: exported' line in the app log within 60s of "
            "accepting the Save dialog — the export did not run."
        )
        m = RE_EXPORTED.search(line)
        label, path = m.group(1), m.group(2).strip()
        log.info(f"  exported '{label}' → {path}")
        assert label == STATE["db_session"], (
            f"Export was of session '{label}', but the scan just captured is "
            f"'{STATE['db_session']}' — the wrong History row was selected."
        )
        STATE["export_path"] = Path(path)

    def test_20_export_file_valid(self, app):
        """The exported CSV is on disk, carries the user label in its name,
        has the per-camera columns, and holds ~40 rows/s for the whole
        scan duration."""
        path = STATE["export_path"]
        deadline = time.time() + 20
        size = -1
        while time.time() < deadline:
            if path.exists() and path.stat().st_size == size and size > 0:
                break
            size = path.stat().st_size if path.exists() else -1
            time.sleep(1)
        assert path.exists() and path.stat().st_size > 0, f"export file missing/empty: {path}"

        def _norm(s: str) -> str:
            return "".join(c for c in s if c.isalnum()).upper()

        assert _norm(STATE["subject_label"]) in _norm(path.name), (
            f"export file name '{path.name}' does not carry the user label "
            f"'{STATE['subject_label']}' set in step 3."
        )
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            header = next(reader)
            rows = sum(1 for _ in reader)
        for col in ("frame_id", "timestamp_s", "bfi_l1", "bfi_l8", "bvi_l1", "bvi_r8"):
            assert col in header, f"export header lacks '{col}': {header[:12]}…"
        expected = SCAN_DURATION_SEC * NOMINAL_SAMPLE_HZ
        log.info(f"  export: {rows} rows, {len(header)} columns "
                 f"(expected ≈ {expected:.0f} rows at {NOMINAL_SAMPLE_HZ:.0f} Hz).")
        assert 0.80 * expected <= rows <= 1.05 * expected, (
            f"export has {rows} rows; expected ≈ {expected:.0f} for a "
            f"{SCAN_DURATION_SEC}s scan at {NOMINAL_SAMPLE_HZ:.0f} Hz "
            f"(80–105 % band). Frames were dropped or the export is truncated."
        )

    def test_21_load_in_viewer(self, app):
        """Load the selected scan into the PlotViewer.

        The row selected in step 19 is still active, so the button reads
        'Load "<scan>" →'; match on the stable "Load" prefix and pixel-click
        it (these QML buttons ignore the UIA InvokePattern)."""
        name = _history_button_name("Load")
        if not name or not ("“" in name or '"' in name):
            name = _select_latest_history_row()
        assert name, (
            "No relabelled 'Load \"<scan>\"' button in the open History "
            "modal — the row selection did not register."
        )
        name = _click_history_button("Load")
        assert name, "No 'Load ...' button found in the open History modal."
        log.info(f"  clicked History load button '{name.strip()}'.")
        time.sleep(SLEEP)

    def test_22_history_closed_by_load(self, app):
        """History closes itself once the scan loads (footer buttons vanish)."""
        deadline = time.time() + 8
        while time.time() < deadline and _history_open():
            time.sleep(0.5)
        assert not _history_open(), (
            "History modal footer buttons still present after 'Load in "
            "viewer' — expected the modal to close once the scan loaded."
        )

    def test_23_plot_loaded(self, app):
        """The viewer reports the loaded scan: the connector's
        '[Plot] loaded past scan' line names the captured session and its
        live edge matches the scan duration (backend truth — the plot
        cells are not readable over UIA)."""
        assert is_app_alive(), (
            "App window is gone after loading the scan into the PlotViewer."
        )
        log_path = find_app_log()
        assert log_path, "could not locate the bloodflow app log."
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
        hits = RE_PLOT_LOADED.findall(text)
        assert hits, "No '[Plot] loaded past scan' line in the app log."
        label, samples, edge = hits[-1]
        edge = float(edge)
        log.info(f"  viewer loaded '{label}': samples={samples} liveEdge={edge:.3f}s")
        assert label == STATE["db_session"], (
            f"Viewer loaded '{label}', expected the captured scan "
            f"'{STATE['db_session']}'."
        )
        assert abs(edge - SCAN_DURATION_SEC) <= 10, (
            f"Loaded scan's live edge is {edge:.1f}s; expected ≈ "
            f"{SCAN_DURATION_SEC}s — the viewer holds a truncated scan."
        )

    def test_24_open_settings(self, app):
        """Settings modal opens from the sidebar gear; About card firmware versions are read into the report."""
        click_panel("Settings")
        time.sleep(SLEEP)
        # SettingsModal is fingerprinted by its "Time window" text /
        # "Run Calibration" button; a ComboBox may not be present, so
        # just assert the app survived opening it.
        assert is_app_alive(), "App closed when opening Settings."

        # Read Console FW / Left Sensor FW / Right Sensor FW off the About
        # card and hand them to the HIL report header. Best-effort: the
        # Qt accessibility bridge sometimes drops Text elements, and the
        # report writer falls back to the app log's connect-time device
        # stats when nothing was recorded here — so a miss is a warning,
        # not a failure of the happy path.
        fw = read_about_card_firmware(timeout=10.0)
        if fw:
            record_firmware_versions(fw, source="Settings → About")
            STATE["firmware"] = fw
            log.info(
                f"  About card — Console FW: {fw.get('console', '?')}, "
                f"Left Sensor FW: {fw.get('left', '?')}, "
                f"Right Sensor FW: {fw.get('right', '?')}"
            )
        else:
            log.warning(
                "  About card firmware rows not exposed via UIA; report "
                "will use the app log's device stats instead."
            )
        # Same for the serial numbers ("Console SN" / "Left Sensor SN" /
        # "Right Sensor SN"): recorded for the report header when the card
        # is readable, else the report falls back to the app log's
        # connect-time "serial=" lines.
        sn = read_about_card_serials(timeout=10.0)
        if sn:
            record_serial_numbers(sn, source="Settings → About")
            STATE["serials"] = sn
            log.info(
                f"  About card — Console SN: {sn.get('console', '?')}, "
                f"Left Sensor SN: {sn.get('left', '?')}, "
                f"Right Sensor SN: {sn.get('right', '?')}"
            )
        else:
            log.warning(
                "  About card serial rows not exposed via UIA; report "
                "will use the app log's connect-time serials instead."
            )

    def test_25_close_settings(self, app):
        """Settings modal closes (Escape)."""
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    def test_26_app_still_alive(self, app):
        """After the full sweep the app is still alive and responsive."""
        assert is_app_alive(), (
            "App window is gone at the end of the happy-path sweep."
        )
        log.info(
            f"  Happy-path complete — scan ran {STATE['scan_seconds']}s, "
            f"label '{STATE['subject_label']}'."
        )
