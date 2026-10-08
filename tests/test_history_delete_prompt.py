"""
test_history_delete_prompt.py — History → Delete asks only "Are you sure?"
with no password and no mention of engineering mode
(openmotion-bloodflow-app#626, fixed by #707 for #703, ships in 1.5.4-dev.4).

Before the fix the Clinical build's Delete opened a password prompt
reading "Enter the engineering password to permanently delete…", which
exposed engineering mode to clinical operators and meant handing them
the engineering password. The requirement (2026-10-06) is a confirmation
step, not authentication, in every build.

Steps (nothing is deleted — the prompt is always cancelled):
  1. History opens (log line "[History] opened — N scan(s)"; skips when
     N is 0 because Delete is disabled with nothing to delete).
  2. The newest row is selected; the footer Delete button is enabled.
  3. Delete opens the confirm prompt: Cancel + Delete buttons appear, no
     password prompt button (Unlock) and no new text input appears. A
     screenshot of the prompt is saved to test_logs for the wording,
     which is plain QML Text and not readable over UIA.
  4. Cancel closes the prompt; the log records no deletion.

Preconditions
- App running (either variant) with at least one scan in History.

Marked ``dev``: ~20 s, no scan, no laser.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

import pyautogui
import pytest

from conftest import SLEEP, log, uia_window
from hil_helpers import (
    click_element_center,
    click_panel,
    find_app_log,
    log_size,
    move_window_on_screen,
    wait_for_pattern,
)

pytestmark = pytest.mark.dev

RE_HISTORY_OPENED = re.compile(r"\[History\] opened\D+(\d+) scan")
RE_DELETED = re.compile(r"deleted", re.IGNORECASE)
TEST_LOGS = Path(__file__).resolve().parent / "test_logs"

# Buttons the old password prompt (PasswordPromptModal) exposed.
PASSWORD_PROMPT_BUTTONS = ("Unlock", "View Logs", "Calibrate")

STATE: dict = {}


def _buttons() -> list[str]:
    try:
        return [(b.window_text() or "").strip() for b in uia_window().descendants(control_type="Button")]
    except Exception as e:
        log.warning(f"  UIA button scan failed: {e}")
        return []


def _edit_count() -> int:
    try:
        return len(uia_window().descendants(control_type="Edit"))
    except Exception:
        return -1


def _click_button_containing(substr: str) -> str:
    """Pixel-click the first Button whose name contains ``substr``."""
    for b in uia_window().descendants(control_type="Button"):
        name = (b.window_text() or "").strip()
        if substr in name:
            click_element_center(b, name)
            time.sleep(SLEEP)
            return name
    return ""


@pytest.mark.incremental
class TestHistoryDeleteConfirm:
    """Delete prompt is a plain confirmation in every build."""

    def test_01_history_opens_with_scans(self, app):
        """History opens and lists at least one scan (skips on an empty History)."""
        move_window_on_screen()
        log_path = find_app_log()
        assert log_path, "app log not found"
        STATE["log"] = log_path
        STATE["off"] = log_size(log_path)
        click_panel("History")
        line = wait_for_pattern(RE_HISTORY_OPENED, log_path, STATE["off"], 10)
        assert line, "no '[History] opened' line within 10 s of clicking History"
        n = int(RE_HISTORY_OPENED.search(line).group(1))
        log.info(f"  History lists {n} scan(s)")
        if n == 0:
            pytest.skip("History is empty — Delete is disabled with nothing to delete")
        time.sleep(SLEEP)

    def test_02_row_selected_delete_enabled(self, app):
        """Clicking the newest row selects it; the footer Delete button shows '(1)'."""
        # Rows are plain Text (not in UIA); the search TextField is an Edit.
        # Its 34 px logical height gives the DPI scale and the first row sits
        # 73 logical px below it (header bar 30 + spacing 12×2 + margin 2 + row/2).
        edits = [e.rectangle() for e in uia_window().descendants(control_type="Edit")]
        assert edits, "History search field not exposed over UIA; cannot locate rows"
        search = min(edits, key=lambda r: r.top)
        scale = (search.bottom - search.top) / 34.0
        pyautogui.click(int(search.left + 10 * scale), int(search.bottom + 73 * scale))
        time.sleep(1.5)
        STATE["edits_before"] = _edit_count()   # search + read-only notes pane
        delete = next((b for b in _buttons() if "Delete" in b), "")
        log.info(f"  footer Delete button reads {delete!r}; edits visible: {STATE['edits_before']}")
        assert "(1)" in delete, f"row not selected — Delete button reads {delete!r}"

    def test_03_delete_opens_plain_confirm(self, app):
        """Delete opens Cancel/Delete confirm; no password button and no new text input appear."""
        name = _click_button_containing("Delete")
        assert name, "History Delete button not found"
        time.sleep(1)
        btns = _buttons()
        edits_now = _edit_count()
        TEST_LOGS.mkdir(exist_ok=True)
        shot = TEST_LOGS / f"history_delete_confirm_{datetime.now():%Y%m%d_%H%M%S}.png"
        pyautogui.screenshot(str(shot))
        log.info(f"  buttons with prompt open: {btns}; edits: {edits_now}; screenshot -> {shot}")
        assert "Cancel" in btns and "Delete" in btns, f"confirm prompt not open (buttons: {btns})"
        assert not any(b in PASSWORD_PROMPT_BUTTONS for b in btns), (
            f"a password prompt opened instead of a plain confirm (buttons: {btns})"
        )
        assert edits_now == STATE["edits_before"], (
            f"a text input appeared with the prompt ({edits_now} vs {STATE['edits_before']}) — "
            "that is a password field"
        )

    def test_04_cancel_closes_without_deleting(self, app):
        """Cancel closes the prompt; the log records no deletion; History closes."""
        assert _click_button_containing("Cancel"), "Cancel button not found"
        time.sleep(1)
        btns = _buttons()
        assert "Cancel" not in btns, "confirm prompt still open after Cancel"
        text = Path(STATE["log"]).read_bytes()[STATE["off"]:].decode("utf-8", errors="replace")
        assert not RE_DELETED.search(text), "the log records a deletion — Cancel must delete nothing"
        click_panel("History")   # toggle closed
        time.sleep(SLEEP)
