"""
test_notes_empty_session_toast.py — app#725: no "Note saved." for a note the
app discarded.

A scan stopped within its first second or two records no corrected rows, so
ScanDBSink deletes the session ("recorded no corrected rows — deleting empty
session row"). The post-scan Notes modal still opens for that session; a
note typed there is logged as "Scan notes: no DB session with label …" and
the app shows a green "Note saved." toast anyway. The note is gone and
History is unchanged.

Steps
-----
  1. Start a scan and stop it STOP_DELAYS_S after "Full scan started" is
     logged, one delay per attempt, until the log shows the session being
     deleted (a Stop that lands after the first corrected rows keeps the
     session and is not the #725 case).
  2. The post-scan Notes modal opens: type a note, close it with Escape.
  3. Read the log window since the Stop: the note must either be persisted
     ("Scan notes saved to DB session …") or the app must not claim it was
     ("Toast … [success] … Note saved."). "no DB session" followed by the
     success toast is the bug.

Preconditions
-------------
- Console and both sensors connected, sensors on the phantom (the laser
  fires for about a second per attempt).
- ``NOTES725_MANUAL=1``: step 1 does no clicking and instead waits for the
  operator to Start and Stop by hand (the race that empties the session
  has only been produced by hand so far); steps 2–3 run unchanged.
- Any build: the Notes modal and the toast are the same in Clinical and
  Research.

Marked ``release``: short laser scans, ~2 min.
"""

from __future__ import annotations

import ctypes
import os
import re
import time
from pathlib import Path

import pyautogui
import pytest

from conftest import SLEEP, log, require_focus, uia_window
from hil_helpers import (
    click_element_center, click_panel, find_app_log, get_panel_button_pos, log_size,
    move_window_on_screen, recalibrate_panel_buttons, wait_for_pattern,
)

pytestmark = pytest.mark.release

RE_SCAN_STARTED = re.compile(r"Full scan started")
RE_START_CAPTURE = re.compile(r"startCapture\(")   # ~10 ms before the start line
RE_SCAN_ENDED = re.compile(r"Full scan ended: (\w+) — duration (\d\d:\d\d:\d\d)")
RE_SESSION_DELETED = re.compile(r"ScanDBSink: session (\d+) \((\S+)\) recorded no corrected rows — deleting empty session row")
RE_NO_DB_SESSION = re.compile(r"Scan notes: no DB session with label '([^']+)'")
RE_NOTE_SAVED = re.compile(r"Toast #\d+ \[success\].*Note saved\.")
RE_NOTES_PERSISTED = re.compile(r"Scan notes saved to DB session '([^']+)'")

STOP_DELAYS_S = (0.0, 0.2, 0.4, 0.7, 1.0, 1.9)   # after startCapture(); the hand repro of 2026-10-13 had the stop applied ~0.3 s into streaming
ATTEMPTS = len(STOP_DELAYS_S)
MANUAL = os.environ.get("NOTES725_MANUAL", "") == "1"   # the operator does Start/Stop by hand
MANUAL_WAIT_S = 180
NOTES_EDIT_MIN_H = 200      # logical px: the Notes TextArea is ~300 tall, other Edits ~34
MODAL_TIMEOUT = 30.0
NOTE_TEXT = "note-725 typed after an empty session"

STATE: dict = {}


# ─────────────────────────────────────────────
# helpers (same shapes as test_notes_space_focus.py)
# ─────────────────────────────────────────────
def _window_scale() -> float:
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(uia_window().handle)
        return dpi / 96.0 if dpi else 1.0
    except Exception:
        return 1.0


def _notes_edit_open() -> bool:
    """True while the Notes modal's TextArea (a tall UIA Edit) is on screen."""
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


def _wait_fast(rx: re.Pattern, log_path: Path, off: int, timeout: float) -> str | None:
    """Poll the log tail every 50 ms (wait_for_pattern's cadence is too slow
    for a Stop that has to land within ~2 s of the start line)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with open(log_path, "rb") as fh:
            fh.seek(off)
            tail = fh.read().decode("utf-8", errors="replace")
        m = rx.search(tail)
        if m:
            return tail[m.start():].splitlines()[0]
        time.sleep(0.05)
    return None


def _start_and_stop_at_once(log_path: Path, stop_delay_s: float) -> tuple[str, str, bool]:
    """Click Start (and the Clinical 'Start Scan' gate when it appears), then
    click Stop ``stop_delay_s`` after 'Full scan started' is logged — a
    direct click at the calibrated Start/Stop position, no focus/settle
    delays. Whether the session ends up empty depends on where the Stop
    lands inside the trigger start-up (a 0.5 s Stop still produced 36
    frames and rows; the hand repro at 1.9 s produced 12 frames and none),
    so the caller sweeps several delays. Returns
    (how, duration, session_deleted) from the 'Full scan ended' line."""
    off = log_size(log_path)
    stop_xy = get_panel_button_pos("Start")   # Start toggles to Stop while scanning
    click_panel("Start")
    deadline, gate = time.time() + 240, False
    started = None
    while time.time() < deadline and not started:
        started = _wait_fast(RE_START_CAPTURE, log_path, off, 0.5)
        if not started and not gate and _click_button("Start Scan", timeout=0.3):
            log.info("  contact-quality gate: clicked 'Start Scan'")
            gate = True
            pyautogui.moveTo(*stop_xy)        # park the pointer on Stop
    assert started, "no 'startCapture(' line within 240 s of clicking Start"
    t_start = time.time()
    if stop_delay_s:
        time.sleep(stop_delay_s)
    pause, pyautogui.PAUSE = pyautogui.PAUSE, 0
    try:
        pyautogui.click(*stop_xy)
    finally:
        pyautogui.PAUSE = pause
    log.info(f"  Stop clicked {time.time() - t_start:.2f} s after startCapture()")
    assert wait_for_pattern(RE_SCAN_STARTED, log_path, off, 30), "no 'Full scan started' line after the Stop click"
    ended = wait_for_pattern(RE_SCAN_ENDED, log_path, off, 90)
    assert ended, "no 'Full scan ended' line within 90 s of clicking Stop"
    m = RE_SCAN_ENDED.search(ended)
    text = log_path.read_text(encoding="utf-8", errors="replace")[off:]
    deleted = RE_SESSION_DELETED.search(text)
    log.info(f"  scan {m.group(1)} after {m.group(2)}; session deleted as empty: {bool(deleted)}")
    STATE["off_after_stop"] = off
    return m.group(1), m.group(2), bool(deleted)


# ─────────────────────────────────────────────
# tests
# ─────────────────────────────────────────────
@pytest.mark.incremental
class TestNotesEmptySessionToast:

    def test_01_stop_before_any_rows(self, app):
        """Start then Stop at once, until the app deletes the session as empty."""
        move_window_on_screen()
        require_focus()
        # The session fixture calibrates the sidebar while the window is still
        # settling; the Start position it cached can be hundreds of px off.
        pos = recalibrate_panel_buttons()
        log.info(f"  sidebar recalibrated: Start at {pos.get('Start')}")
        log_path = find_app_log()
        assert log_path, "app log not found"
        STATE["log"] = log_path
        if MANUAL:
            # The 12-frame race has only been hit by hand so far (2/2 on
            # 2026-10-13; 0/18 scripted): wait for the operator to Start and
            # Stop, and take the first scan whose session the app deletes.
            off = log_size(log_path)
            STATE["off_after_stop"] = off
            log.info(f"  MANUAL: press Start, then Stop within about a second — waiting up to {MANUAL_WAIT_S} s")
            deadline = time.time() + MANUAL_WAIT_S
            while time.time() < deadline:
                text = log_path.read_text(encoding="utf-8", errors="replace")[off:]
                m = RE_SESSION_DELETED.search(text)
                if m and RE_SCAN_ENDED.search(text[m.start():]):
                    log.info(f"  session {m.group(1)} ({m.group(2)}) deleted as empty — reproduced by hand")
                    STATE["reproduced"] = True
                    return
                if RE_SCAN_ENDED.search(text):
                    log.info("  a scan ended but kept its session; try again (Stop sooner)")
                    off = log_size(log_path)
                    STATE["off_after_stop"] = off
                time.sleep(0.5)
            pytest.skip(f"no empty-session scan within {MANUAL_WAIT_S} s of manual Start/Stop")
        for attempt, delay in enumerate(STOP_DELAYS_S, start=1):
            how, duration, deleted = _start_and_stop_at_once(log_path, delay)
            if deleted:
                log.info(f"  attempt {attempt}: empty session reproduced ({how} after {duration})")
                STATE["reproduced"] = True
                return
            # The session survived (rows were recorded): close the post-scan
            # Notes modal that opens for it and try again a little faster.
            log.info(f"  attempt {attempt}: scan kept its session ({duration}); retrying")
            if _wait_notes_edit(True, 15):
                time.sleep(1)
                pyautogui.press("escape")
                _wait_notes_edit(False, 10)
            time.sleep(3)
        pytest.skip(f"could not stop a scan before its first corrected rows in {ATTEMPTS} attempts")

    def test_02_note_for_deleted_session(self, app):
        """The post-scan Notes modal opens for the deleted session: type a note and close it."""
        if not STATE.get("reproduced"):
            pytest.skip("empty session not reproduced in step 1")
        log_path = STATE["log"]
        assert _wait_notes_edit(True, MODAL_TIMEOUT), \
            "the post-scan Notes modal did not open after the stopped scan"
        time.sleep(1)
        pyautogui.typewrite(NOTE_TEXT, interval=0.03)
        time.sleep(0.5)
        STATE["off_note"] = log_size(log_path)
        pyautogui.press("escape")
        assert not _wait_notes_edit(False, 10), "Notes modal still open after Escape"
        time.sleep(3)

    def test_03_no_success_toast_for_a_discarded_note(self, app):
        """Either the note was persisted, or the app did not say it was."""
        if not STATE.get("reproduced"):
            pytest.skip("empty session not reproduced in step 1")
        log_path = STATE["log"]
        text = log_path.read_text(encoding="utf-8", errors="replace")[STATE["off_after_stop"]:]
        persisted = RE_NOTES_PERSISTED.search(text)
        no_session = RE_NO_DB_SESSION.findall(text)
        toast = RE_NOTE_SAVED.search(text)
        log.info(f"  persisted={bool(persisted)} 'no DB session' lines={len(no_session)} 'Note saved.' toast={bool(toast)}")
        for line in text.splitlines():
            if RE_NO_DB_SESSION.search(line) or RE_NOTE_SAVED.search(line) or RE_NOTES_PERSISTED.search(line) \
                    or RE_SESSION_DELETED.search(line):
                log.info(f"    {line.strip()[:150]}")
        if persisted:
            log.info("  the note was persisted — the app kept the session after all")
            return
        assert not (no_session and toast), (
            "app#725: the session was deleted as empty, the note was refused "
            f"('Scan notes: no DB session with label {no_session[-1]!r}'), and the app still showed "
            "the success toast 'Note saved.'"
        )
