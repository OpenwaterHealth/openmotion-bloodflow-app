"""
test_notes_space_focus.py — Space opens the timestamped Notes modal on
every press during a scan, with no click into the app in between
(openmotion-bloodflow-app#517, fixed by #615 / cherry-picked in #665,
on ``next`` for 1.5.4; verification build 1.5.4-dev.2+).

Before the fix the first Space → type → Escape worked, but hiding the
Notes modal did not take keyboard focus away from its TextArea: the next
Space was typed into the invisible textarea and the window-level
shortcut never fired until the operator clicked inside the app. The fix
hands focus back to the plot viewer whenever the last modal closes.

This drives the real app during a live scan (the shortcut is enabled only
while the trigger is ON and no modal is open), with pyautogui keystrokes
only — never a mouse click inside the window between cycles, which is
exactly what masked the bug. The modal's TextArea is a UIA Edit, so its
appearance and disappearance are observed over UIA; its text is read
back through the clipboard (select-all + copy), as test_happy_path_full
does; every close toasts "Note saved." (logged as a Toast line); the
notes land in ``sessions.session_notes`` of scans.db at scan end.

Steps:
  1. Start a scan (clinical gate handled); wait for "Full scan started".
  2. Three cycles of Space → modal (new Edit) → type "space-note N" →
     read back (timestamp prefix + text) → Escape → Edit gone + "Note
     saved." toast. No mouse click anywhere between cycles.
  3. A fourth cycle closed with the card's ✕ (a pixel click on the
     card's top-right corner, computed from the card layout: 600×450
     logical, centred with the icon-bar inset), then Space once more
     reopens the modal; Escape.
  4. Stop the scan (Start toggles to Stop); the post-scan Notes modal's
     text is read back and closed; "Scan notes saved to DB session" is
     logged; all four timestamped notes are present in order — in the
     scans.db row on Research, in the post-scan modal text on Clinical
     (its DB is SQLCipher-encrypted and cannot be read by the test).

Preconditions
- App running (either variant), console + at least one sensor, phantom.
- Scan Settings as left by the operator (duration irrelevant: the scan is
  stopped by the test).

Marked ``release``: runs a laser scan, ~1.5 min.
"""

from __future__ import annotations

import ctypes
import re
import sqlite3
import time
from pathlib import Path

import pyautogui
import pytest

from conftest import SLEEP, get_clipboard, log, require_focus, uia_window
from hil_helpers import (
    click_element_center,
    click_panel,
    find_app_log,
    log_size,
    move_window_on_screen,
    read_app_config_value,
    wait_for_pattern,
)

pytestmark = pytest.mark.release

RE_SCAN_STARTED = re.compile(r"Full scan started")
RE_SCAN_ENDED = re.compile(r"Full scan ended")
RE_NOTE_SAVED = re.compile(r"Toast #\d+ \[success\].*Note saved\.")
RE_NOTES_PERSISTED = re.compile(r"Scan notes saved to DB session '([^']+)'")
RE_STAMP = re.compile(r"\[\d\d:\d\d:\d\d / \d\d:\d\d:\d\d\] - ")

NOTE_PREFIX = "space-note"
CYCLES_WITH_ESCAPE = 3
CARD_W, CARD_H, ICON_BAR_INSET = 600, 450, 104      # NotesModal.qml
MODAL_TIMEOUT = 6.0

STATE: dict = {}


# ─────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────
NOTES_EDIT_MIN_H = 200     # logical px: the Notes TextArea is ~300 tall, other Edits ~34


def _notes_edit_open() -> bool:
    """True while the Notes modal's TextArea (a tall UIA Edit) is on screen.
    Counting Edits is unreliable: the modal overlay hides the main-screen
    Edits from UIA on the Clinical build, so the count may not change."""
    try:
        s = _window_scale()
        for e in uia_window().descendants(control_type="Edit"):
            r = e.rectangle()
            if (r.bottom - r.top) / s >= NOTES_EDIT_MIN_H:
                return True
    except Exception as e:
        log.warning(f"  UIA Edit scan failed: {e}")
    return False


def _wait_notes_edit(want_open: bool, timeout: float) -> bool:
    """Poll until the Notes TextArea is (not) on screen; return the final state."""
    deadline = time.time() + timeout
    state = _notes_edit_open()
    while time.time() < deadline and state != want_open:
        time.sleep(0.25)
        state = _notes_edit_open()
    return state


def _click_button(name: str, timeout: float = 10.0) -> bool:
    """Pixel-click a UIA Button by exact name (QML Buttons ignore Invoke)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for b in uia_window().descendants(control_type="Button"):
                if (b.window_text() or "").strip() == name:
                    click_element_center(b, name)
                    time.sleep(SLEEP)
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _press_start_and_wait_for_scan(log_path: Path, start_off: int, timeout: float = 240.0) -> str:
    """Click Start; on a Clinical build click 'Start Scan' in the contact-
    quality gate when it appears; return the 'Full scan started' line."""
    click_panel("Start")
    deadline = time.time() + timeout
    clicked_gate = False
    while time.time() < deadline:
        line = wait_for_pattern(RE_SCAN_STARTED, log_path, start_off, 2)
        if line:
            return line
        if not clicked_gate and _click_button("Start Scan", timeout=0.5):
            log.info("  contact-quality gate: clicked 'Start Scan'")
            clicked_gate = True
    pytest.fail(f"no 'Full scan started' line within {timeout:.0f} s of clicking Start")


def _clear_clipboard() -> None:
    import subprocess
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value ''"],
                       check=False, timeout=5)
    except Exception as e:
        log.warning(f"  _clear_clipboard failed: {e}")


def _read_notes_textarea(attempts: int = 3) -> str:
    """Select-all + copy the focused Notes textarea (not readable over UIA)."""
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


def _window_scale() -> float:
    """Physical px per logical px of the app window (per-monitor DPI)."""
    try:
        hwnd = uia_window().handle
        dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
        return dpi / 96.0 if dpi else 1.0
    except Exception:
        return 1.0


def _notes_close_x_pos() -> tuple[int, int]:
    """Screen position of the Notes card's ✕ (a Text in a Rectangle, not a
    UIA Button): card 600×450 logical, centred in the window with the
    icon-bar inset offset, ✕ 28 px at 10 px from the card's top-right."""
    rect = uia_window().rectangle()
    s = _window_scale()
    win_w = (rect.right - rect.left) / s
    win_h = (rect.bottom - rect.top) / s
    card_w = min(win_w - ICON_BAR_INSET - 40, CARD_W)
    card_left = win_w / 2 + ICON_BAR_INSET / 2 - card_w / 2
    card_top = win_h / 2 - CARD_H / 2
    x = rect.left + (card_left + card_w - 10 - 14) * s
    y = rect.top + (card_top + 10 + 14) * s
    return int(x), int(y)


def _open_with_space(log_path: Path, retries: int = 3) -> None:
    """Press Space and wait for the modal's TextArea. Retries the keypress
    only while no modal opened (trigger not ON yet)."""
    for attempt in range(retries):
        pyautogui.press("space")
        if _wait_notes_edit(True, MODAL_TIMEOUT if attempt == 0 else 3.0):
            return
        log.warning(f"  Space press {attempt + 1}: the Notes textarea did not appear")
    pytest.fail(
        "Space did not open the Notes modal: no textarea appeared — the shortcut did not fire "
        "(focus stranded in the hidden textarea = #517, or the trigger is not ON)"
    )


def _close_and_confirm(log_path: Path, how: str) -> None:
    """Close the open modal by Escape or the ✕; wait for the textarea to go
    and the 'Note saved.' toast to be logged."""
    off = log_size(log_path)
    if how == "escape":
        pyautogui.press("escape")
    else:
        x, y = _notes_close_x_pos()
        log.info(f"  clicking the Notes ✕ at ({x}, {y})")
        pyautogui.click(x, y)
    assert not _wait_notes_edit(False, MODAL_TIMEOUT), f"Notes modal still open after {how}"
    line = wait_for_pattern(RE_NOTE_SAVED, log_path, off, 6)
    assert line, f"no 'Note saved.' toast logged within 6 s of closing with {how}"


def _db_session_notes(session_label: str) -> str | None:
    data_dir = read_app_config_value("dataDirectory") or ""
    db = Path(data_dir) / "data" / "scans.db" if data_dir else None
    if db is None or not db.exists():
        # packaged portable build: data/ next to the exe, same as the log's parent/..
        lp = find_app_log()
        if lp:
            db = Path(lp).resolve().parent.parent / "data" / "scans.db"
    if db is None or not db.exists():
        log.warning(f"  scans.db not found ({db})")
        return None
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT session_notes FROM sessions WHERE session_label = ?",
                              (session_label,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        log.warning(f"  scans.db read failed: {e}")
        return None
    return row[0] if row else None


# ─────────────────────────────────────────────
# tests
# ─────────────────────────────────────────────
@pytest.mark.incremental
class TestNotesSpaceFocus:
    """Space reopens the Notes modal after every close, no click needed."""

    def test_01_scan_started(self, app):
        """A scan starts; the trigger comes on a moment later."""
        move_window_on_screen()
        require_focus()
        log_path = find_app_log()
        assert log_path, "app log not found"
        STATE["log"] = log_path
        STATE["off"] = log_size(log_path)
        assert not _notes_edit_open(), "a Notes modal is already open before the scan"
        line = _press_start_and_wait_for_scan(log_path, STATE["off"])
        log.info(f"  {line.strip()[:120]}")
        time.sleep(4)   # trigger ON + first frames; the shortcut needs triggerState === "ON"

    def test_02_space_type_escape_three_times(self, app):
        """Three Space → type → Escape cycles with no mouse click in between; each reads back and toasts."""
        log_path = STATE["log"]
        notes = []
        for n in range(1, CYCLES_WITH_ESCAPE + 1):
            _open_with_space(log_path)
            text = f"{NOTE_PREFIX} {n}"
            pyautogui.typewrite(text, interval=0.03)
            time.sleep(0.3)
            got = _read_notes_textarea()
            log.info(f"  cycle {n}: textarea reads back {got.splitlines()[-1][:80]!r}")
            assert text in got, f"cycle {n}: typed note did not land in the textarea (read back {got[-80:]!r})"
            assert RE_STAMP.search(got.splitlines()[-1]), f"cycle {n}: no [elapsed / wall-clock] stamp on the new line"
            _close_and_confirm(log_path, "escape")
            notes.append(text)
            time.sleep(0.5)
        STATE["notes"] = notes
        log.info(f"  {CYCLES_WITH_ESCAPE} Space cycles OK, all closed with Escape")

    def test_03_close_with_x_then_space_again(self, app):
        """A fourth note closed with the card's ✕; Space still reopens the modal afterwards."""
        log_path = STATE["log"]
        _open_with_space(log_path)
        text = f"{NOTE_PREFIX} 4"
        pyautogui.typewrite(text, interval=0.03)
        time.sleep(0.3)
        got = _read_notes_textarea()
        assert text in got, f"typed note did not land in the textarea (read back {got[-80:]!r})"
        _close_and_confirm(log_path, "x")
        STATE["notes"].append(text)
        _open_with_space(log_path)
        log.info("  Space reopened the modal after the ✕ close")
        _close_and_confirm(log_path, "escape")

    def test_04_stop_scan_notes_persisted(self, app):
        """Stop the scan; the post-scan Notes modal is closed; all four notes are in scans.db in order."""
        log_path = STATE["log"]
        off = log_size(log_path)
        click_panel("Start")        # Start toggles to Stop while scanning
        line = wait_for_pattern(RE_SCAN_ENDED, log_path, off, 60)
        assert line, "no 'Full scan ended' line within 60 s of clicking Stop"
        # The Notes modal opens by itself once the scan's notes are finalised.
        post_text = ""
        if _wait_notes_edit(True, 30):
            time.sleep(1)
            post_text = _read_notes_textarea()
            log.info(f"  post-scan notes modal shows {len(post_text.splitlines())} line(s)")
            _close_and_confirm(log_path, "escape")
        persisted = wait_for_pattern(RE_NOTES_PERSISTED, log_path, off, 20)
        assert persisted, "no 'Scan notes saved to DB session' line after the scan"
        label = RE_NOTES_PERSISTED.search(persisted).group(1)
        db_notes = _db_session_notes(label)
        if db_notes is None:
            # Clinical builds keep scans.db SQLCipher-encrypted (HMAC keyed per
            # user), so the row cannot be read here; the post-scan modal shows
            # the notes the connector finalised and saved, so check that text.
            assert post_text, f"scans.db not readable and no post-scan Notes modal text for {label!r}"
            log.info(f"  scans.db not readable (encrypted clinical DB); checking the post-scan modal text")
            notes_text = post_text
        else:
            log.info(f"  session {label!r} notes in DB:\n{db_notes}")
            notes_text = db_notes
        positions = [notes_text.find(t) for t in STATE["notes"]]
        assert all(p >= 0 for p in positions), f"missing notes: {[t for t, p in zip(STATE['notes'], positions) if p < 0]}"
        assert positions == sorted(positions), "notes are out of order"
        assert len(RE_STAMP.findall(notes_text)) >= len(STATE["notes"]), "fewer timestamps than notes"
        log.info(f"  all {len(STATE['notes'])} timestamped notes present, in order")
