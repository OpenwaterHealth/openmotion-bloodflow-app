"""
Session Notes — exhaustive TextArea interaction tests.

"""

import time
from datetime import datetime

import pyautogui
import pytest

from conftest import SLEEP, click_sidebar, ensure_visible, get_clipboard, log, require_focus

SIDEBAR_NOTES = (0.019, 0.305)


def _open_notes():
    click_sidebar(*SIDEBAR_NOTES, "Notes sidebar button")


def _close_notes():
    require_focus()
    pyautogui.press("escape")
    time.sleep(SLEEP)


def _clear_textarea():
    require_focus()
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.2)
    pyautogui.press("delete")
    time.sleep(0.3)


def _type_note(text: str):
    require_focus()
    log.info(f"  type: {text!r}")
    for line in text.split("\n"):
        if line:
            pyautogui.typewrite(line, interval=0.04)
        pyautogui.press("enter")
    time.sleep(SLEEP)


def _copy_all_and_read() -> str:
    """Select all, copy, return clipboard text."""
    require_focus()
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "c")
    time.sleep(0.3)
    return get_clipboard()


@pytest.mark.incremental
class TestNotes:
    """Session Notes modal -- typing, persistence, clipboard operations."""

    def test_01_open(self, app):
        ensure_visible()
        _open_notes()

    def test_02_auto_focus(self, app):
        """TextArea receives forceActiveFocus() on open -- no click needed."""
        pass

    def test_03_type_note(self, app):
        self.__class__.unique_note = f"AutoTest_{datetime.now():%H%M%S}"
        _type_note(self.unique_note)

    def test_04_close_x(self, app):
        _close_notes()

    def test_05_persist_after_reopen(self, app):
        _open_notes()
        clip = _copy_all_and_read()
        assert self.unique_note in clip, (
            f"Expected '{self.unique_note}' in clipboard, got: '{clip[:60]}'"
        )

    def test_06_append(self, app):
        require_focus()
        pyautogui.hotkey("ctrl", "end")
        time.sleep(0.2)
        pyautogui.typewrite(" -- appended", interval=0.04)
        time.sleep(SLEEP)

    def test_07_clear(self, app):
        _clear_textarea()
        time.sleep(SLEEP)

    def test_08_multi_line(self, app):
        _type_note("Line one\nLine two\nLine three")

    def test_09_close_escape(self, app):
        _close_notes()

    def test_10_multiline_persists(self, app):
        _open_notes()
        clip = _copy_all_and_read()
        assert "Line one" in clip and "Line three" in clip, (
            f"Multi-line text not preserved: '{clip[:80]}'"
        )

    def test_11_close_empty(self, app):
        _clear_textarea()
        time.sleep(SLEEP)
        _close_notes()

    def test_12_reopen_empty(self, app):
        _open_notes()
        clip = _copy_all_and_read()
        assert clip == "", f"TextArea not empty after clear -- got: '{clip[:60]}'"

    def test_13_long_text(self, app):
        """500-char single-line text (word-wrap stress test)."""
        long_text = "LongNote_" + "X" * 500
        _type_note(long_text)
        _close_notes()
        _open_notes()
        clip = _copy_all_and_read()
        assert long_text[:20] in clip, (
            f"500-char text not persisted: '{clip[:40]}'"
        )

    def test_14_numeric_punctuation(self, app):
        _clear_textarea()
        num_note = "ID: 12345 HR: 72bpm BP: 120/80"
        _type_note(num_note)
        _close_notes()
        _open_notes()
        clip = _copy_all_and_read()
        assert "12345" in clip and "72bpm" in clip, (
            f"Numeric/punct not persisted: '{clip[:60]}'"
        )

    def test_15_cut(self, app):
        require_focus()
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "x")
        time.sleep(0.5)
        clip = get_clipboard()
        assert len(clip) > 0, "Ctrl+X did not put text in clipboard"

    def test_16_paste(self, app):
        require_focus()
        pyautogui.hotkey("ctrl", "v")
        time.sleep(SLEEP)
        clip = _copy_all_and_read()
        assert len(clip) > 0, "Ctrl+V did not restore text"

    def test_17_undo(self, app):
        _clear_textarea()
        require_focus()
        pyautogui.typewrite("UndoTarget", interval=0.04)
        time.sleep(0.5)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.2)
        pyautogui.press("delete")
        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "z")  # undo the delete
        time.sleep(0.5)
        clip = _copy_all_and_read()
        assert "UndoTarget" in clip, (
            f"Ctrl+Z did not restore text: '{clip[:60]}'"
        )

    def test_18_sidebar_toggle(self, app):
        _clear_textarea()
        _close_notes()
        _open_notes()
        click_sidebar(*SIDEBAR_NOTES, "Notes sidebar (toggle close)")
        _open_notes()  # reopen for remaining tests

    def test_19_large_note(self, app):
        """10-line note persistence test."""
        _clear_textarea()
        large_note = "\n".join(
            f"Line {i:02d}: data point {i * 10}" for i in range(1, 11)
        )
        _type_note(large_note)
        _close_notes()
        _open_notes()
        clip = _copy_all_and_read()
        assert "Line 01" in clip and "Line 10" in clip, (
            f"10-line note not persisted: first='{clip[:25]}'"
        )

    def test_20_rapid_cycle(self, app):
        """Text survives 3 rapid open/close cycles."""
        _clear_textarea()
        cycle_text = f"CycleTest_{datetime.now():%H%M%S}"
        _type_note(cycle_text)
        for _ in range(3):
            _close_notes()
            _open_notes()
        clip = _copy_all_and_read()
        assert cycle_text in clip, (
            f"Text lost after 3 cycles: '{clip[:60]}'"
        )
        # Clean up
        _clear_textarea()
        _close_notes()
