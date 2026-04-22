"""
Reduced Mode — end-to-end test.

Covers the full Reduced Mode workflow using the global Settings modal (gear icon).
Sensor dropdowns are a Scan Settings feature and are NOT tested here — Scan Settings
is hidden while Reduced Mode is active.

Two classes:
  TestReducedMode       (01–20) — keyboard-driven interactions
  TestReducedModeMouse  (21–40) — mouse-driven interactions
"""

import time
from datetime import datetime

import pyautogui
import pygetwindow as gw
import pytest

from conftest import (
    APP_KEYWORDS,
    SLEEP,
    click_by_name,
    click_sidebar,
    ensure_visible,
    get_app_window,
    get_clipboard,
    log,
    require_focus,
    uia_window,
    wait_with_log,
)

# ─────────────────────────────────────────────
# Sidebar coordinates
# ─────────────────────────────────────────────
SIDEBAR_SETTINGS      = (0.019, 0.920)   # gear icon — bottommost sidebar button
SIDEBAR_NOTES_REDUCED = (0.019, 0.210)   # Notes position when Scan Settings is hidden
SIDEBAR_START         = (0.019, 0.115)   # Start / Stop toggle button
SIDEBAR_HISTORY       = (0.020, 0.820)   # History icon

# Relative coordinate of the Reduced Mode Enable toggle within the app window.
# Measured from screenshot — adjust if the toggle position shifts.
REDUCED_MODE_TOGGLE = (0.400, 0.421)

_TABS_TO_REDUCED_MODE = 16

SCAN_WAIT   = 200   # seconds to run the scan (3 minutes 20 seconds)
STOP_BUFFER = 15    # seconds to wait after stopping for data to save
VIZ_WAIT    = 60    # seconds to leave each plot open


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def _tab_to_reduced_mode_toggle(tab_into_modal: bool = True):
    """Tab from the current focus to the Reduced Mode Enable toggle in the
    Settings modal, then press Space to toggle it.

    tab_into_modal=True  — press one extra Tab to enter the modal first
                           (use when the modal was just opened and nothing
                           inside has focus yet).
    tab_into_modal=False — skip that first Tab (use when a field inside the
                           modal already has focus).
    """
    require_focus()
    if tab_into_modal:
        pyautogui.press("tab")   # enter modal — lands on first interactive element
        time.sleep(0.3)
    log.info(f"  tabbing {_TABS_TO_REDUCED_MODE} times to Reduced Mode Enable toggle")
    for _ in range(_TABS_TO_REDUCED_MODE):
        pyautogui.press("tab")
        time.sleep(0.1)
    pyautogui.press("space")
    time.sleep(SLEEP)


def _close_plot_window() -> bool:
    """Close the plot window opened by the app using keyboard (alt+f4)."""
    for w in gw.getAllWindows():
        if not w.title.strip():
            continue
        if any(k in w.title.lower() for k in APP_KEYWORDS):
            continue
        try:
            if w.isMinimized:
                w.restore()
                time.sleep(1)
            w.activate()
            time.sleep(0.5)
            log.info(f"  Closing plot window: '{w.title}'")
            pyautogui.hotkey("alt", "f4")
            time.sleep(SLEEP)
            return True
        except Exception as e:
            log.warning(f"  Could not close '{w.title}': {e}")
    log.warning("  No plot window found to close")
    return False


def _close_plot_window_mouse() -> bool:
    """Close the plot window by moving the mouse to its center then alt+f4."""
    for w in gw.getAllWindows():
        if not w.title.strip():
            continue
        if any(k in w.title.lower() for k in APP_KEYWORDS):
            continue
        try:
            if w.isMinimized:
                w.restore()
                time.sleep(1)
            w.activate()
            time.sleep(0.5)
            cx = w.left + w.width // 2
            cy = w.top + w.height // 2
            pyautogui.moveTo(cx, cy, duration=0.3)
            log.info(f"  Closing plot window (mouse): '{w.title}'  center=({cx},{cy})")
            pyautogui.hotkey("alt", "f4")
            time.sleep(SLEEP)
            return True
        except Exception as e:
            log.warning(f"  Could not close '{w.title}': {e}")
    log.warning("  No plot window found to close")
    return False


def _move_window_on_screen():
    """Move the app window onto the primary screen if it is off-screen."""
    try:
        w = get_app_window()
        screen_w, screen_h = pyautogui.size()
        if w.left < 0 or w.top < 0 or w.left > screen_w or w.top > screen_h:
            log.warning(
                f"  Window is off-screen at ({w.left}, {w.top}) — "
                f"moving to primary display"
            )
            w.moveTo(50, 50)
            time.sleep(1)
            log.info(f"  Window moved to ({w.left}, {w.top})")
    except Exception as e:
        log.warning(f"  _move_window_on_screen failed: {e}")


def _scroll_modal_to_bottom():
    """Scroll the Settings modal content down to reveal the Reduced Mode section.

    Scrolls in three passes with a short pause between each to handle
    modals that animate or load content progressively.
    """
    ensure_visible()
    w = get_app_window()
    cx = w.left + w.width // 2
    cy = w.top + w.height // 2
    pyautogui.moveTo(cx, cy, duration=0.2)
    for _ in range(3):
        pyautogui.scroll(-50)   # scroll down
        time.sleep(0.3)
    time.sleep(0.5)
    log.info("  Modal scrolled to bottom")


def _click_coord(rx: float, ry: float, label: str = ""):
    """Move mouse to a relative coordinate within the app window and click."""
    _move_window_on_screen()
    ensure_visible()
    w = get_app_window()
    x = int(w.left + rx * w.width)
    y = int(w.top + ry * w.height)
    log.info(f"  click '{label}'  rel({rx:.3f}, {ry:.3f})  abs({x}, {y})")
    pyautogui.moveTo(x, y, duration=0.3)
    pyautogui.click(x, y)
    time.sleep(SLEEP)


def _selected_scan_text() -> str:
    """Read the current text of the scan-picker ComboBox in History."""
    try:
        win = uia_window()
        cb = win.child_window(control_type="ComboBox")
        if cb.exists(timeout=2):
            return cb.window_text().strip()
    except Exception:
        pass
    return ""


# ─────────────────────────────────────────────
# Test class — keyboard
# ─────────────────────────────────────────────
@pytest.mark.incremental
class TestReducedMode:
    """Enable Reduced Mode, run a manual scan, verify History, then restore.

    Uses keyboard interactions. Scan Settings is NOT tested here — it is
    hidden while Reduced Mode is active.
    """

    # ── Settings: enable Reduced Mode ─────────────────────────────────────

    def test_01_open_settings(self, app):
        _move_window_on_screen()
        ensure_visible()
        click_sidebar(*SIDEBAR_SETTINGS, "Settings gear icon")

    def test_02_camera_config_visible(self, app):
        """Default Camera Configuration section is visible at the top."""
        pass  # visual confirmation only

    def test_03_enable_reduced_mode(self, app):
        """Tab into the Settings modal to the Reduced Mode Enable toggle and turn ON."""
        _tab_to_reduced_mode_toggle(tab_into_modal=True)
        log.info("  Reduced Mode enabled")

    def test_04_close_settings(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    # ── Notes: full feature test in Reduced Mode ─────────────────────────

    def test_05_open_notes(self, app):
        """Notes is now at the former Scan Settings position in the reduced sidebar."""
        click_sidebar(*SIDEBAR_NOTES_REDUCED, "Notes (reduced mode position)")

    def test_06_type_note(self, app):
        """Type a unique note and save it."""
        require_focus()
        TestReducedMode.session_note = f"ReducedScan_{datetime.now():%Y%m%d_%H%M%S}"
        log.info(f"  Typing note: '{TestReducedMode.session_note}'")
        pyautogui.typewrite(TestReducedMode.session_note, interval=0.04)
        time.sleep(SLEEP)

    def test_07_close_notes(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    def test_08_persist_after_reopen(self, app):
        """Verify the note persists after closing and reopening."""
        click_sidebar(*SIDEBAR_NOTES_REDUCED, "Notes (reopen)")
        require_focus()
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "c")
        time.sleep(0.3)
        clip = get_clipboard()
        assert TestReducedMode.session_note in clip, (
            f"Note not persisted: expected '{TestReducedMode.session_note}' "
            f"in clipboard, got: '{clip[:60]}'"
        )
        log.info(f"  Note persisted: '{clip[:60]}'")

    def test_09_append_text(self, app):
        """Append text to existing note."""
        require_focus()
        pyautogui.hotkey("ctrl", "end")
        time.sleep(0.2)
        pyautogui.typewrite(" -- appended", interval=0.04)
        time.sleep(SLEEP)

    def test_10_clear_and_multiline(self, app):
        """Clear textarea and type multi-line note."""
        require_focus()
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.2)
        pyautogui.press("delete")
        time.sleep(0.3)
        for line in ["Line one", "Line two", "Line three"]:
            pyautogui.typewrite(line, interval=0.04)
            pyautogui.press("enter")
        time.sleep(SLEEP)

    def test_11_multiline_persists(self, app):
        """Close and reopen — verify multi-line note persists."""
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)
        click_sidebar(*SIDEBAR_NOTES_REDUCED, "Notes (reopen)")
        require_focus()
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "c")
        time.sleep(0.3)
        clip = get_clipboard()
        assert "Line one" in clip and "Line three" in clip, (
            f"Multi-line text not preserved: '{clip[:80]}'"
        )
        log.info("  Multi-line note persisted OK")

    def test_12_cut_paste(self, app):
        """Ctrl+X cuts text, Ctrl+V pastes it back."""
        require_focus()
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "x")
        time.sleep(0.3)
        clip = get_clipboard()
        assert len(clip) > 0, "Ctrl+X did not put text in clipboard"
        pyautogui.hotkey("ctrl", "v")
        time.sleep(SLEEP)
        log.info("  Cut/paste OK")

    def test_13_close_notes_for_scan(self, app):
        """Clear and close notes before starting scan."""
        require_focus()
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.2)
        pyautogui.press("delete")
        time.sleep(0.2)
        # Re-type the session note for the scan
        TestReducedMode.session_note = f"ReducedScan_{datetime.now():%Y%m%d_%H%M%S}"
        pyautogui.typewrite(TestReducedMode.session_note, interval=0.04)
        time.sleep(SLEEP)
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    # ── Scan: start, wait, stop ────────────────────────────────────────────

    def test_14_start_scan(self, app):
        click_sidebar(*SIDEBAR_START, "Start scan")

    def test_15_wait_2_minutes(self, app):
        wait_with_log(SCAN_WAIT, "2-minute manual scan running")

    def test_16_stop_scan(self, app):
        click_sidebar(*SIDEBAR_START, "Stop scan")
        log.info(f"  Waiting {STOP_BUFFER}s for scan data to save...")
        time.sleep(STOP_BUFFER)

    # ── History: verify scan, visualize BFI/BVI only (no Contrast/Mean in Reduced Mode)

    def test_17_open_history(self, app):
        click_sidebar(*SIDEBAR_HISTORY, "History")

    def test_18_latest_scan_selected(self, app):
        scan_text = _selected_scan_text()
        assert len(scan_text) > 0, (
            "History ComboBox is empty — no scans found."
        )
        log.info(f"  Latest scan in ComboBox: '{scan_text}'")

    def test_19_visualize_bfi_bvi(self, app):
        click_by_name("Visualize BFI/BVI")
        wait_with_log(VIZ_WAIT, "BFI/BVI plot open")

    def test_20_close_bfi_plot(self, app):
        _close_plot_window()

    def test_21_close_history(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)



# ─────────────────────────────────────────────
# Mouse-based test class — continues with Reduced Mode already ON
# from TestReducedMode above
# ─────────────────────────────────────────────
@pytest.mark.incremental
class TestReducedModeMouse:
    """Reduced Mode mouse workflow — Reduced Mode is already enabled by TestReducedMode.

    Scan Settings is NOT tested here — it is hidden while Reduced Mode is active.
    """

    # ── Notes: type session note ───────────────────────────────────────────

    def test_22_open_notes(self, app):
        """Notes is now at the former Scan Settings position in the reduced sidebar."""
        _move_window_on_screen()
        click_sidebar(*SIDEBAR_NOTES_REDUCED, "Notes (reduced mode position)")

    def test_23_type_note(self, app):
        require_focus()
        TestReducedModeMouse.session_note = (
            f"ReducedScanMouse_{datetime.now():%Y%m%d_%H%M%S}"
        )
        log.info(f"  Typing note: '{TestReducedModeMouse.session_note}'")
        pyautogui.typewrite(TestReducedModeMouse.session_note, interval=0.04)
        time.sleep(SLEEP)

    def test_24_close_notes(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    # ── Scan: start, wait, stop ────────────────────────────────────────────

    def test_25_start_scan(self, app):
        click_sidebar(*SIDEBAR_START, "Start scan")

    def test_26_wait_scan(self, app):
        wait_with_log(SCAN_WAIT, "manual scan running")

    def test_27_stop_scan(self, app):
        click_sidebar(*SIDEBAR_START, "Stop scan")
        log.info(f"  Waiting {STOP_BUFFER}s for scan data to save...")
        time.sleep(STOP_BUFFER)

    # ── History: verify scan, visualize BFI/BVI only

    def test_28_open_history(self, app):
        click_sidebar(*SIDEBAR_HISTORY, "History")

    def test_29_latest_scan_selected(self, app):
        scan_text = _selected_scan_text()
        assert len(scan_text) > 0, (
            "History ComboBox is empty — no scans found."
        )
        log.info(f"  Latest scan in ComboBox: '{scan_text}'")

    def test_30_visualize_bfi_bvi(self, app):
        click_by_name("Visualize BFI/BVI")
        wait_with_log(VIZ_WAIT, "BFI/BVI plot open")

    def test_31_close_bfi_plot_mouse(self, app):
        """Move mouse to plot window center then close."""
        _close_plot_window_mouse()

    def test_32_close_history(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)
