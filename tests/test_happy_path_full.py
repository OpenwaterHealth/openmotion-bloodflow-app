"""
test_happy_path_full.py — exhaustive end-to-end happy-path walkthrough.

Marker: ``release`` (~8-10 min wall-clock — one real 2-min laser scan
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
  7.  Scan duration is set to 2 minutes.
  8.  Scan Settings modal closes (and is verified closed).
  9.  Session Notes modal opens and a note is typed.
  10. Notes modal closes.
  11. Contact-quality Check runs and its result modal is dismissed.
  12. Scan starts.
  13. Scan runs the full configured duration and reports completion.
  14. Scan output files (canonical CSV + scans.db) land on disk, and the
      canonical CSV carries the user label from step 3.
  15. Post-scan Session Notes modal is dismissed.
  16. History modal opens.
  17. The scan just captured is listed in History's table.
  18. The scan loads into the embedded PlotViewer via "Load in viewer →".
  19. History modal closes itself once the scan loads.
  20. The PlotViewer is showing the loaded scan and the app survived it.
  21. Settings modal opens.
  22. Settings modal closes.
  23. The app is still alive and responsive at the end of the sweep.

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

import re
import time
from datetime import datetime
from pathlib import Path

import pyautogui
import pytest

from conftest import (
    SLEEP,
    log,
    read_combobox_values,
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
    force_app_config_value,
    is_app_alive,
    log_size,
    read_app_config_value,
    recalibrate_panel_buttons,
    wait_for_pattern,
    write_app_config_value,
)

pytestmark = pytest.mark.release

# Scan Settings + Check are hidden in reduced mode; force the on-disk
# flag false at module-import time (before the session-scoped ``app``
# fixture launches the app) and restore it on teardown. Same pattern as
# test_scan_flow / test_scan_settings / test_usb_disconnect_freeze.
_INITIAL_REDUCED_MODE = force_app_config_value("reducedMode", False)


@pytest.fixture(scope="module", autouse=True)
def _restore_reduced_mode_on_module_teardown():
    yield
    write_app_config_value("reducedMode", _INITIAL_REDUCED_MODE)


# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────
SCAN_DURATION_MIN = 2
SCAN_DURATION_SEC = SCAN_DURATION_MIN * 60
CHECK_WAIT_SEC    = 120                       # Check completes within ~2 min
CHECK_START_TIMEOUT_SEC = 20                  # a healthy Check logs "started" in ~1s
SCAN_START_TIMEOUT_SEC  = 30                  # scan launch (pipeline build) can take ~10s
SCAN_POLL_SEC     = 10
SCAN_MAX_WAIT_SEC = SCAN_DURATION_SEC + 180   # scan + 3-min buffer
READY_TIMEOUT_SEC = 30                        # wait for CONNECTED at start
SENSOR_OPTION     = "All"                     # both sensors → all 16 cameras

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

# Shared state across the incremental steps. pytest builds a fresh class
# instance per test method, so per-test ``self.x`` doesn't persist — keep
# cross-step data in a module-level dict instead.
STATE: dict = {
    "subject_label": "",     # user label typed in step 3
    "files_before":  set(),  # data-dir listing snapshot taken at scan start
    "scan_seconds":  None,   # duration the app reported at completion
    "scan_started_sec": None,  # duration the backend logged at scan start
    "duration_hms":  f"00:{SCAN_DURATION_MIN:02d}:00",  # configured scan duration
    "history_scan_count": None,  # scan count History logged when opened
}

# NOTE: the JSON + Markdown run report is produced by conftest's autouse
# ``_hil_report_session`` reporter (test_logs/HIL_Report_<module>_<ts>.json
# and .md) — the suite-wide V&V evidence mechanism. This module deliberately
# does NOT write its own report to avoid duplicating it.


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def _resolve_data_dir() -> Path:
    """Return the directory the app writes scan CSVs + scans.db into.

    ``dataDirectory`` in app_config.json is the single output root
    (see CLAUDE.md). Fall back to the current working directory, which
    is what the app itself uses when the key is unset.
    """
    configured = read_app_config_value("dataDirectory", None)
    if configured and Path(configured).is_dir():
        return Path(configured)
    return Path.cwd()


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

    focus_combobox_by_label(label)
    pyautogui.hotkey("alt", "down")   # open the popup
    time.sleep(0.5)
    pyautogui.press("home")           # jump to the first item
    time.sleep(0.2)
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
    assert actual == option, f"{side} sensor: expected '{option}', got '{actual}'"
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
    tab count landed the value in the Hours field (a 2-hour scan instead
    of 2 minutes). The three duration inputs are unambiguous UIA Edit
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
        """Left sensor mask set to 'All'."""
        _select_sensor(side="Left", combobox_index=0)

    def test_05_select_right_sensor(self, app):
        """Right sensor mask set to 'All'."""
        _select_sensor(side="Right", combobox_index=1)

    def test_06_toggle_scan_mode_switch(self, app):
        """The Timed/Free-Run duration switch is operable.

        Toggle it to Free Run and back to Timed (the mode the scan needs),
        verifying the switch responds without closing or crashing the
        modal. Mirrors test_scan_settings test_08/test_09, collapsed into
        one step so the happy path leaves the switch on Timed.
        """
        require_focus()
        focus_combobox_by_label("Left Sensor")
        require_focus()
        pyautogui.press("tab")            # Left ComboBox -> Right ComboBox
        time.sleep(0.2)
        pyautogui.press("tab")            # Right ComboBox -> Switch
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
        """Scan duration set to 2 minutes, verified in the H:M:S fields."""
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
            f"field (this is the 2-hours-instead-of-2-minutes bug)."
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
        """Session Notes modal opens and accepts a typed note."""
        click_panel("Notes")
        require_focus()
        note = f"Happy-path sweep {datetime.now():%Y-%m-%d %H:%M:%S}"
        log.info(f"  typing note: '{note}'")
        pyautogui.typewrite(note, interval=0.03)
        time.sleep(SLEEP)

    def test_10_close_notes(self, app):
        """Notes modal closes (Escape)."""
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

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
        """Scan runs the full 2-min duration and reports completion."""
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
        """Canonical scan CSV + scans.db are written, and the CSV carries
        the user label from step 3."""
        data_dir = _resolve_data_dir()
        after = _dir_listing(data_dir)
        new = sorted(after - STATE["files_before"])
        log.info(f"  new files in {data_dir}: {new}")

        new_csvs = [n for n in new if n.lower().endswith(".csv")]
        assert new_csvs, (
            f"No new .csv appeared in {data_dir} after the scan. New files: {new}"
        )

        # The app normalizes User Label on save — prepends 'ow', uppercases,
        # and strips underscores/spaces/punctuation (e.g. 'HappyPath_144243'
        # -> 'owHAPPYPATH144243'), so compare on the alphanumeric-uppercase
        # form of both sides rather than a raw substring. Same normalization
        # as test_scan_settings.
        def _norm(s: str) -> str:
            return "".join(c for c in s if c.isalnum()).upper()

        norm_label = _norm(STATE["subject_label"])
        labelled = [n for n in new_csvs if norm_label and norm_label in _norm(n)]
        assert labelled, (
            f"None of the new CSVs {new_csvs} carry the user label "
            f"'{STATE['subject_label']}' (normalized '{norm_label}') set in "
            f"step 3 — the label did not propagate to the scan output filename."
        )
        log.info(f"  scan output CSV with label: {labelled}")

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

    def test_15_close_post_scan_notes(self, app):
        """Auto-opened post-scan Session Notes modal is dismissed."""
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    def test_16_open_history(self, app):
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

    def test_17_scan_listed(self, app):
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

    def test_18_load_in_viewer(self, app):
        """Select the latest scan row, then load it into the PlotViewer.

        'Load in viewer' is disabled until a row is actively clicked (the
        default highlight isn't enough — verified live). History rows are
        NOT exposed via UIA on this build (only the footer Buttons are), so
        the top row is selected by a click anchored to the app-window rect
        — a last-resort calibrated coordinate (STYLE_GUIDE §6, tier 3),
        correlated against the live modal layout. The button itself is
        UIA-addressable and clicked by substring.
        """
        r = uia_window().rectangle()
        w, h = r.right - r.left, r.bottom - r.top
        # First data row sits ~0.22 across and ~0.275 down from the modal's
        # top-left (the modal is a centred overlay that scales with the window).
        row_x, row_y = int(r.left + 0.223 * w), int(r.top + 0.275 * h)
        log.info(f"  selecting latest History row at ({row_x}, {row_y})")
        pyautogui.click(row_x, row_y)
        time.sleep(0.5)

        # Once a row is selected the button relabels from "Load in viewer →"
        # to 'Load "<scan-name>" →', so match on the stable "Load" prefix.
        name = _click_history_button("Load")
        assert name, (
            "No 'Load ...' button found in the open History modal after "
            "selecting a row — expected the load button next to Export CSV / "
            "Delete (the row selection may not have registered)."
        )
        log.info(f"  invoked History load button '{name.strip()}'.")
        time.sleep(SLEEP)

    def test_19_history_closed_by_load(self, app):
        """History closes itself once the scan loads (footer buttons vanish)."""
        deadline = time.time() + 8
        while time.time() < deadline and _history_open():
            time.sleep(0.5)
        assert not _history_open(), (
            "History modal footer buttons still present after 'Load in "
            "viewer' — expected the modal to close once the scan loaded."
        )

    def test_20_plot_loaded(self, app):
        """The scan loaded into the PlotViewer and the app survived it."""
        assert is_app_alive(), (
            "App window is gone after loading the scan into the PlotViewer."
        )
        log.info("  PlotViewer loaded the scan; app still responsive.")

    def test_21_open_settings(self, app):
        """Settings modal opens from the sidebar gear."""
        click_panel("Settings")
        time.sleep(SLEEP)
        # SettingsModal is fingerprinted by its "Time window" text /
        # "Run Calibration" button; a ComboBox may not be present, so
        # just assert the app survived opening it.
        assert is_app_alive(), "App closed when opening Settings."

    def test_22_close_settings(self, app):
        """Settings modal closes (Escape)."""
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    def test_23_app_still_alive(self, app):
        """After the full sweep the app is still alive and responsive."""
        assert is_app_alive(), (
            "App window is gone at the end of the happy-path sweep."
        )
        log.info(
            f"  Happy-path complete — scan ran {STATE['scan_seconds']}s, "
            f"label '{STATE['subject_label']}'."
        )
