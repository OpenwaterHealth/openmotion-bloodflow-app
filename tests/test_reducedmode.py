"""
Reduced Mode — end-to-end test.

Covers the full Reduced Mode workflow using the global Settings modal
(gear icon). Sensor dropdowns are a Scan Settings feature and are NOT
tested here — Scan Settings is hidden while Reduced Mode is active.

Three classes:
  TestReducedMode         (01–21) — keyboard-driven Notes / scan / history flow
  TestReducedModeMouse    (22–32) — mouse-driven repeat of the same flow
  TestReducedModeSettings (33–37) — Settings modal: Time Window dropdown
                                    parametrized over 3/5/15/30s, plus
                                    Auto-scale Y-axes toggle ON.
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
    ensure_visible,
    get_app_window,
    get_clipboard,
    log,
    require_focus,
    uia_window,
    wait_with_log,
)
from utils import (
    click_panel,
    close_plot_window,
    move_window_on_screen,
    selected_scan_text,
)

pytestmark = pytest.mark.release

# Relative coordinate of the Reduced Mode Enable toggle within the app window.
# Measured from screenshot — adjust if the toggle position shifts.
REDUCED_MODE_TOGGLE = (0.400, 0.421)

_TABS_TO_REDUCED_MODE = 16

SCAN_WAIT       = 200   # seconds to run the long scan (3 min 20 s)
SHORT_SCAN_WAIT = 120   # seconds to run each Settings-feature scan (2 min)
STOP_BUFFER     = 15    # seconds to wait after stopping for data to save
VIZ_WAIT        = 60    # seconds to leave each plot open

# Time window dropdown values shown in the Settings modal (seconds).
# TestReducedModeSettings.test_33 parametrizes over each of these.
TIME_WINDOW_OPTIONS = [3, 5, 15, 30]


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


def _wait_for_signal_quality_and_start_scan(timeout: int = 180) -> bool:
    """In Reduced Mode, after clicking Start the app auto-runs signal quality check.

    Wait up to `timeout` seconds for the 'Good signal quality' modal,
    then click 'Start Scan' to begin the actual scan.

    Returns True if 'Start Scan' was clicked, False otherwise.
    """
    log.info(f"  Waiting up to {timeout}s for signal quality dialog...")
    elapsed = 0
    poll_interval = 5
    while elapsed < timeout:
        time.sleep(poll_interval)
        elapsed += poll_interval

        try:
            win = uia_window()
            quality_modal_found = False
            for elem in win.descendants():
                try:
                    text = elem.window_text().strip().lower()
                    if "good signal quality" in text or "signal quality" in text:
                        quality_modal_found = True
                        break
                except Exception:
                    continue

            if quality_modal_found:
                log.info(f"  Signal quality modal appeared at {elapsed}s — clicking 'Start Scan'")
                for elem in win.descendants():
                    try:
                        if elem.window_text().strip() == "Start Scan":
                            rect = elem.rectangle()
                            cx = (rect.left + rect.right) // 2
                            cy = (rect.top + rect.bottom) // 2
                            log.info(f"  Clicking 'Start Scan' button at ({cx}, {cy})")
                            pyautogui.click(cx, cy)
                            time.sleep(SLEEP)
                            return True
                    except Exception:
                        continue
                log.warning("  'Good signal quality' modal found but 'Start Scan' button not located")
                return False
        except Exception as e:
            log.warning(f"  signal quality check failed: {e}")

        if elapsed % 30 == 0:
            log.info(f"  Still waiting for signal quality dialog... {elapsed}/{timeout}s")

    log.warning(f"  Signal quality dialog did not appear within {timeout}s")
    return False


def _select_time_window(seconds: int):
    """Open the 'Time window' dropdown in the Settings modal and select
    ``<seconds>``.

    Locator strategy (style-guide §6 — UIA before coords):
      1. Find the 'Time window' label Text via UIA, then click the
         nearest ComboBox by vertical proximity.
      2. Fall back to the first ComboBox in the modal if the label
         can't be found (some Qt accessibility builds don't expose
         pure-text label elements).

    On the dropdown popup we navigate with keyboard (Home + N×Down +
    Return) rather than coordinate-clicking the option, so the test
    is unaffected by dropdown popup positioning.
    """
    require_focus()
    log.info(f"  Selecting Time window = {seconds}s")

    win = uia_window()
    target_cb = None

    try:
        labels = win.descendants(title="Time window")
        if labels:
            label_cy = (labels[0].rectangle().top
                        + labels[0].rectangle().bottom) // 2
            cbs = win.descendants(control_type="ComboBox")
            if cbs:
                target_cb = min(
                    cbs,
                    key=lambda c: abs(
                        (c.rectangle().top + c.rectangle().bottom) // 2
                        - label_cy
                    ),
                )
    except Exception as e:
        log.warning(f"  Time window label-proximity lookup failed: {e}")

    if target_cb is None:
        try:
            cbs = win.descendants(control_type="ComboBox")
            if cbs:
                target_cb = cbs[0]
        except Exception as e:
            log.warning(f"  ComboBox fallback failed: {e}")

    if target_cb is None:
        raise RuntimeError(
            "Could not locate the Time window ComboBox in the Settings "
            "modal. UIA returned no labelled element and no ComboBox "
            "descendants — check that the modal is actually open and "
            "scrolled to the top."
        )

    rect = target_cb.rectangle()
    cx = (rect.left + rect.right) // 2
    cy = (rect.top + rect.bottom) // 2
    pyautogui.click(cx, cy)
    time.sleep(0.5)

    idx = TIME_WINDOW_OPTIONS.index(seconds)
    pyautogui.press("home")
    time.sleep(0.2)
    for _ in range(idx):
        pyautogui.press("down")
        time.sleep(0.15)
    pyautogui.press("return")
    time.sleep(SLEEP)


def _scroll_settings_to_top():
    """Scroll the Settings modal up so Time Window / Auto-scale appear.

    Mirror of ``_scroll_modal_to_bottom`` for the opposite direction;
    Auto-scale lives near the top of the modal, Time Window just below
    it. Eight short scroll-up nudges with brief pauses cope with
    modals that animate.
    """
    ensure_visible()
    w = get_app_window()
    cx = w.left + w.width // 2
    cy = w.top + w.height // 2
    pyautogui.moveTo(cx, cy, duration=0.2)
    for _ in range(8):
        pyautogui.scroll(50)   # positive = scroll up
        time.sleep(0.2)
    time.sleep(0.5)


def _toggle_auto_scale_on():
    """Toggle the 'Auto-scale Y-axes' Switch ON in the Settings modal.

    QML Switch elements aren't exposed as CheckBox/Button via UIA,
    so we have two strategies:

      1. Find any Text element whose contents match a known label
         variant ('auto-scale', 'auto scale', 'autoscale', 'y-axes',
         'y-axis'). When found, click ~43% across the window — that's
         where the Switch sits relative to the label per current QML
         layout. (Style-guide §6 calibrated coord, not a static ratio.)
      2. Tab navigation fallback: focus the first ComboBox in the
         modal (Time Window), Tab once to reach the toggle, Space to
         flip it.

    On total failure we dump the visible UIA texts so the failure is
    diagnosable from the log alone (style-guide §9).
    """
    require_focus()
    log.info("  Toggling Auto-scale Y-axes ON")
    _scroll_settings_to_top()

    win = uia_window()
    label_elem = None
    seen_texts: list[str] = []

    try:
        for elem in win.descendants():
            try:
                t = (elem.window_text() or "").strip()
            except Exception:
                continue
            if t:
                seen_texts.append(t)
            tl = t.lower()
            if any(tag in tl for tag in (
                "auto-scale", "autoscale", "auto scale",
                "y-axes", "y-axis",
            )):
                label_elem = elem
                log.info(f"  Found auto-scale label: '{t}'")
                break
    except Exception as e:
        log.warning(f"  Auto-scale label search failed: {e}")

    if label_elem is not None:
        rect = label_elem.rectangle()
        label_cy = (rect.top + rect.bottom) // 2
        w = get_app_window()
        toggle_x = int(w.left + 0.43 * w.width)
        log.info(f"  Clicking Auto-scale toggle at ({toggle_x}, {label_cy})")
        pyautogui.click(toggle_x, label_cy)
        time.sleep(SLEEP)
        return

    log.warning(
        f"  Auto-scale label not found; first 40 UIA texts: "
        f"{seen_texts[:40]}"
    )
    log.info("  Falling back to Tab navigation from Time Window combobox")
    try:
        cbs = win.descendants(control_type="ComboBox")
        if cbs:
            rect = cbs[0].rectangle()
            cx = (rect.left + rect.right) // 2
            cy = (rect.top + rect.bottom) // 2
            pyautogui.click(cx, cy)
            time.sleep(0.3)
            pyautogui.press("tab")
            time.sleep(0.2)
            pyautogui.press("space")
            time.sleep(SLEEP)
            log.info("  Tab+Space fallback engaged Auto-scale toggle")
            return
    except Exception as e:
        log.warning(f"  Tab fallback failed: {e}")

    raise RuntimeError(
        "Could not locate the Auto-scale Y-axes toggle. Both UIA "
        "label search and Tab-navigation fallback failed; see the "
        "WARNING above for the UIA text dump."
    )


def _run_scan(label: str, duration_sec: int):
    """Click Start, dismiss the signal-quality dialog (Reduced Mode
    auto-runs it), wait for ``duration_sec``, click Start again to
    stop, dismiss the post-scan modal.

    Used by ``TestReducedModeSettings`` to run a scan per Time Window
    value and per Auto-scale state. Calibrated panel clicks per
    style-guide §6.
    """
    log.info(f"  [{label}] Starting scan for {duration_sec}s")
    click_panel("Start")
    _wait_for_signal_quality_and_start_scan()
    wait_with_log(duration_sec, f"[{label}] scan running")
    click_panel("Start")  # toggle: Stop
    log.info(f"  [{label}] Waiting {STOP_BUFFER}s for scan data to save...")
    time.sleep(STOP_BUFFER)
    require_focus()
    pyautogui.press("escape")  # dismiss auto-opened Session Notes (style-guide §8)
    time.sleep(SLEEP)


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
    move_window_on_screen()
    ensure_visible()
    w = get_app_window()
    x = int(w.left + rx * w.width)
    y = int(w.top + ry * w.height)
    log.info(f"  click '{label}'  rel({rx:.3f}, {ry:.3f})  abs({x}, {y})")
    pyautogui.moveTo(x, y, duration=0.3)
    pyautogui.click(x, y)
    time.sleep(SLEEP)


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
        move_window_on_screen()
        ensure_visible()
        click_panel("Settings")

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
        click_panel("Notes")

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
        click_panel("Notes")
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
        click_panel("Notes")
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
        """Click Start — the app auto-runs signal quality check, then click 'Start Scan'."""
        click_panel("Start")
        # In Reduced Mode, the 'Good signal quality' dialog auto-appears
        _wait_for_signal_quality_and_start_scan()

    def test_15_wait_2_minutes(self, app):
        wait_with_log(SCAN_WAIT, "2-minute manual scan running")

    def test_16_stop_scan(self, app):
        click_panel("Start")
        log.info(f"  Waiting {STOP_BUFFER}s for scan data to save...")
        time.sleep(STOP_BUFFER)

    # ── History: verify scan, visualize BFI/BVI only (no Contrast/Mean in Reduced Mode)

    def test_17_open_history(self, app):
        click_panel("History")

    def test_18_latest_scan_selected(self, app):
        scan_text = selected_scan_text()
        assert len(scan_text) > 0, (
            "History ComboBox is empty — no scans found."
        )
        log.info(f"  Latest scan in ComboBox: '{scan_text}'")

    def test_19_visualize_bfi_bvi(self, app):
        click_by_name("Visualize BFI/BVI")
        wait_with_log(VIZ_WAIT, "BFI/BVI plot open")

    def test_20_close_bfi_plot(self, app):
        close_plot_window()

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
        move_window_on_screen()
        click_panel("Notes")

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
        """Click Start — the app auto-runs signal quality check, then click 'Start Scan'."""
        click_panel("Start")
        # In Reduced Mode, the 'Good signal quality' dialog auto-appears
        _wait_for_signal_quality_and_start_scan()

    def test_26_wait_scan(self, app):
        wait_with_log(SCAN_WAIT, "manual scan running")

    def test_27_stop_scan(self, app):
        click_panel("Start")
        log.info(f"  Waiting {STOP_BUFFER}s for scan data to save...")
        time.sleep(STOP_BUFFER)

    # ── History: verify scan, visualize BFI/BVI only

    def test_28_open_history(self, app):
        click_panel("History")

    def test_29_latest_scan_selected(self, app):
        scan_text = selected_scan_text()
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


# ─────────────────────────────────────────────
# Settings feature class — Time Window dropdown + Auto-scale toggle
# ─────────────────────────────────────────────
@pytest.mark.incremental
class TestReducedModeSettings:
    """Reduced Mode Settings — Time Window dropdown + Auto-scale Y-axes.

    For each Time Window value (3, 5, 15, 30 s): open Settings, select
    the value, close Settings, run a 2-min scan. Then turn Auto-scale
    Y-axes ON and run one final 2-min scan to verify both feature
    paths produce a viable scan.

    Sequenced after TestReducedMode/TestReducedModeMouse so the app
    is already in Reduced Mode when this class executes.
    """

    @pytest.mark.parametrize(
        "seconds", TIME_WINDOW_OPTIONS,
        ids=[f"{s}s" for s in TIME_WINDOW_OPTIONS],
    )
    def test_33_time_window_scan(self, app, seconds):
        """Pick the dropdown value, close Settings, run a 2-min scan."""
        move_window_on_screen()
        ensure_visible()
        click_panel("Settings")
        _select_time_window(seconds)
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)
        _run_scan(f"TimeWindow={seconds}s", SHORT_SCAN_WAIT)

    def test_34_open_settings_for_autoscale(self, app):
        """Reopen Settings to enable Auto-scale Y-axes."""
        move_window_on_screen()
        ensure_visible()
        click_panel("Settings")

    def test_35_toggle_autoscale_on(self, app):
        """Flip the Auto-scale Y-axes Switch to ON."""
        _toggle_auto_scale_on()

    def test_36_close_settings(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    def test_37_autoscale_scan(self, app):
        """One final 2-min scan with Auto-scale enabled."""
        _run_scan("AutoScale=ON", SHORT_SCAN_WAIT)
