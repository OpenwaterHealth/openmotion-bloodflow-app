"""
test_scan_zero_duration.py — issue #82 regression: 0:00:00 silently
saves and the modal claims the duration is set, but the actual
configured duration is whatever the BloodFlow page falls back to.

What this test guards
---------------------
After commitDurationFields() runs (on modal close), entering all-zero
H:M:S in Timed mode must:

  1. Reset the duration to 0:01:00 (one minute) instead of accepting 0
  2. Surface a warning Text in the modal explaining the override

Both halves of the fix matter:
  - Without (1), Issue #82 stays open: the user gets a silent
    fallback to whatever the parent page chose (1 hour) with no idea
    why they didn't get a zero-second scan
  - Without (2), the user has no way to learn the rule from the UI

Pre-conditions
--------------
- App launched (session-scoped ``app`` fixture)
- No hardware required — this is a pure-UI test of QML state
"""

import time

import pyautogui
import pytest

from conftest import SLEEP, ensure_visible, log, require_focus, uia_window
from utils import click_panel, focus_combobox_by_label

pytestmark = pytest.mark.dev

_WARNING_TEXT = "Scan duration cannot be 0 seconds — reset to 1 minute."


def _open_scan_settings():
    ensure_visible()
    click_panel("Scan\nSettings")
    time.sleep(SLEEP)


def _close_scan_settings():
    ensure_visible()
    pyautogui.press("escape")
    time.sleep(SLEEP)


def _focus_hours_field():
    """Tab from the Left Sensor combo through to the Hours TextField.
    Order in the modal: Left CB -> Right CB -> Timed/Continuous Switch
    -> Hours -> Minutes -> Seconds. Mirrors the existing
    test_scan_settings.py tab walk."""
    focus_combobox_by_label("Left Sensor")
    require_focus()
    pyautogui.press("tab")  # Left CB -> Right CB
    time.sleep(0.2)
    pyautogui.press("tab")  # Right CB -> Switch
    time.sleep(0.2)
    pyautogui.press("tab")  # Switch -> Hours
    time.sleep(0.3)


def _type_zero_into_focused_field():
    """Replace whatever's in the focused TextField with a single 0.
    The field's IntValidator clamps to 0..59 (or 0..99 for hours);
    typing '0' into all three produces 0:00:00 cumulatively."""
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.typewrite("0", interval=0.05)


def _read_visible_texts(limit: int = 80) -> list[str]:
    """Return up to ``limit`` non-empty window_text values from the
    app's UIA tree — used to confirm the warning text surfaced after
    commitDurationFields() ran."""
    out: list[str] = []
    try:
        win = uia_window()
        for elem in win.descendants():
            try:
                t = (elem.window_text() or "").strip()
            except Exception:
                continue
            if t and len(t) < 200:
                out.append(t)
                if len(out) >= limit:
                    break
    except Exception:
        pass
    return out


def test_zero_duration_resets_to_one_minute_with_warning(app):
    _open_scan_settings()
    try:
        _focus_hours_field()
        _type_zero_into_focused_field()       # Hours = 0
        pyautogui.press("tab"); time.sleep(0.2)
        _type_zero_into_focused_field()       # Minutes = 0
        pyautogui.press("tab"); time.sleep(0.2)
        _type_zero_into_focused_field()       # Seconds = 0
        time.sleep(SLEEP)
    finally:
        _close_scan_settings()

    # Reopen so we can read the post-commit state. commitDurationFields
    # ran during _close_scan_settings; the rejection + reset is now
    # reflected in the field text and the warning Text is visible.
    _open_scan_settings()
    try:
        visible = _read_visible_texts()
        log.info(f"  scan-settings UIA texts (post-reset): {visible}")

        assert _WARNING_TEXT in visible, (
            f"Issue #82 fix missing: zero-duration warning text "
            f"{_WARNING_TEXT!r} not found in the modal after entering "
            f"0:00:00 and reopening. UIA-visible texts: {visible}"
        )

        # The Hours / Minutes / Seconds fields should show 0 / 01 / 00
        # after the reset. Minutes is the load-bearing one — that's the
        # value commitDurationFields() promotes to.
        assert "01" in visible, (
            f"Issue #82 fix missing: after entering 0:00:00, the "
            f"Minutes field should now show '01' (the 1-minute fallback). "
            f"UIA-visible texts: {visible}"
        )
    finally:
        _close_scan_settings()
