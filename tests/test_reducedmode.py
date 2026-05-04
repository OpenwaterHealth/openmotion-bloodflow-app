"""
Reduced Mode — end-to-end test.

Covers the full Reduced Mode workflow. Sensor dropdowns are a Scan
Settings feature and are NOT tested here — Scan Settings is hidden
while Reduced Mode is active.

Reduced Mode is forced on via ``app_config.json`` at module-import
time (before the session-scoped ``app`` fixture launches the app),
not by clicking the Reduced Mode Enable toggle in the Settings modal
at test run time. The original config value is restored on module
teardown. This mirrors the pattern used by ``test_history`` and
``test_scan_settings`` for forcing the *opposite* state.

Three classes:
  TestReducedMode         (05–21) — keyboard-driven Notes / scan / history flow
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
from hil_helpers import (
    click_panel,
    close_plot_window,
    force_app_config_value,
    move_window_on_screen,
    selected_scan_text,
    visible_modals,
    write_app_config_value,
)

pytestmark = pytest.mark.release

# Force ``reducedMode = true`` on disk at module-import time so any
# fresh launch in this session boots directly into Reduced Mode.
# Restored on module teardown via the autouse fixture below.
#
# Caveat (see utils.force_app_config_value): the app caches its
# config at startup, so if the app was already running with
# reducedMode=false when this module is imported, the running
# instance will stay in non-reduced mode until it is relaunched.
_INITIAL_REDUCED_MODE = force_app_config_value("reducedMode", True)


@pytest.fixture(scope="module", autouse=True)
def _restore_reduced_mode_on_module_teardown():
    yield
    write_app_config_value("reducedMode", _INITIAL_REDUCED_MODE)


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


# Title text rendered by ContactQualityModal as a function of its
# state_ property (see components/ContactQualityModal.qml line ~271).
# Stored exactly as they appear in the QML so the targeted
# ``descendants(title=...)`` query below matches case- and
# whitespace-perfectly. Any-state detection.
_CONTACT_QUALITY_STATE_TITLES = {
    "Checking contact quality\u2026": "checking",  # \u2026 is the QML "…"
    "Good signal quality":            "ok",
    "Contact check failed":           "error",
    "Contact Quality Notification":   "warnings",
}

# Footer buttons shown by ContactQualityModal in preScanMode after
# the check leaves "checking" (line ~505 of the QML: the footer
# RowLayout is ``visible: root.state_ !== "checking" || developerMode``).
# Used as a UIA-friendly fallback when the title Text isn't visible
# to the accessibility tree but the QML Buttons still are.
_CONTACT_QUALITY_PRESCAN_BUTTONS = ("Start Scan", "Dismiss", "Retest")


def _find_modal_state(win) -> str | None:
    """Detect ContactQualityModal's current state using whatever UIA
    will expose. Returns one of ``"checking" | "ok" | "error" |
    "warnings"`` or ``None`` if no fingerprint matches.

    Two-pass strategy because the panel-button calibration showed
    plain QML Text elements often don't surface in this app's UIA
    tree (everything falls back to QML pixel layout):

    1. **Targeted title query**: ``descendants(title=...)`` for each
       state title. Targeted queries hit a different UIA code path
       than the unfiltered descendants walk and tend to find Text
       elements the walk misses.
    2. **Button fallback**: if no title fingerprint matched, look
       for ``Start Scan`` / ``Dismiss`` / ``Retest`` (Button
       control type, which Qt always exposes via UIA). The
       footer-buttons-visible condition (``state_ !== "checking"``)
       means seeing any of these proves we're past ``checking`` —
       we report ``"ok"`` as a best guess so the caller can try the
       Start Scan click. If it turns out to be ``error`` or
       ``warnings`` instead, the click is harmless because the
       button is the same one (it just routes to ``continueRequested``
       which the QML uses for the success path).
    """
    for title, state in _CONTACT_QUALITY_STATE_TITLES.items():
        try:
            if win.descendants(title=title):
                return state
        except Exception:
            continue

    # Title pass missed; try buttons. The "Start Scan" / "Dismiss" /
    # "Retest" footer is preScanMode-only and renders only when
    # state_ != "checking", so any of them being present means the
    # check has finished.
    for btn_name in _CONTACT_QUALITY_PRESCAN_BUTTONS:
        try:
            hits = win.descendants(title=btn_name, control_type="Button")
            if hits:
                return "ok"  # best guess; see docstring rationale
        except Exception:
            continue

    return None


def _wait_for_signal_quality_and_start_scan(timeout: int = 180) -> bool:
    """Wait for ContactQualityModal to reach the ``ok`` state after
    clicking Start in Reduced Mode, then click ``Start Scan``.

    Modal lifecycle: opens in ``checking`` state, runs a brief
    contact-quality check (laser pulse + camera read), then
    transitions to one of:

      ``ok``       → title "Good signal quality"; footer shows
                     ``Dismiss`` / ``Retest`` / ``Start Scan`` (we
                     click ``Start Scan`` to begin the real capture).
      ``error``    → title "Contact check failed"; we abort.
      ``warnings`` → title "Contact Quality Notification"; we abort.

    Returns ``True`` only if ``Start Scan`` was clicked.

    Detection delegates to ``_find_modal_state`` which uses targeted
    UIA ``descendants(title=...)`` queries rather than walking the
    whole tree — the unfiltered walk silently misses plain QML
    ``Text`` elements in this app (same root cause as why
    ``calibrate_panel_buttons`` always falls back to the QML pixel
    layout). Falls back further to QML Button detection because Qt
    Buttons are reliably exposed via UIA even when sibling Text
    elements aren't.

    Callers MUST check the return value (or use
    ``_assert_scan_started``). If we never clicked ``Start Scan``,
    the scan didn't actually start, and any subsequent
    ``wait_with_log(SCAN_WAIT, ...)`` / ``click_panel('Start')``
    (intended as Stop) calls run out of phase — the second Start
    click would then *begin* a scan rather than stop one.
    """
    log.info(
        f"  Waiting up to {timeout}s for ContactQualityModal to reach "
        f"'ok' state..."
    )
    started = time.time()
    deadline = started + timeout
    last_state: str | None = None
    last_log_time = started
    poll_interval = 2

    while time.time() < deadline:
        try:
            win = uia_window()
            current_state = _find_modal_state(win)

            if current_state != last_state:
                log.info(
                    f"  ContactQualityModal: "
                    f"{last_state or 'not visible'} → "
                    f"{current_state or 'not visible'}"
                )
                last_state = current_state
                last_log_time = time.time()

            if current_state == "ok":
                log.info("  ok state — locating 'Start Scan' button")
                try:
                    matches = win.descendants(
                        title="Start Scan", control_type="Button"
                    )
                except Exception:
                    matches = []
                if not matches:
                    # Last-ditch: search by title without control_type filter.
                    try:
                        matches = win.descendants(title="Start Scan")
                    except Exception:
                        matches = []
                if matches:
                    rect = matches[0].rectangle()
                    cx = (rect.left + rect.right) // 2
                    cy = (rect.top + rect.bottom) // 2
                    log.info(f"  Clicking 'Start Scan' at ({cx}, {cy})")
                    pyautogui.click(cx, cy)
                    time.sleep(SLEEP)
                    return True
                log.warning(
                    "  ok state reached but 'Start Scan' button not "
                    "found in UIA — modal likely already dismissed or "
                    "the button hasn't rendered yet. Aborting."
                )
                return False

            if current_state == "error":
                log.warning(
                    "  ContactQualityModal hit 'error' state "
                    "('Contact check failed'): pre-scan check failed. "
                    "Aborting wait."
                )
                return False

            if current_state == "warnings":
                log.warning(
                    "  ContactQualityModal hit 'warnings' state "
                    "('Contact Quality Notification'): one or more "
                    "cameras flagged a contact-quality issue. Aborting."
                )
                return False
        except Exception as e:
            log.warning(f"  signal quality poll failed: {e}")

        if time.time() - last_log_time >= 30:
            elapsed = int(time.time() - started)
            log.info(
                f"  Still waiting (last state="
                f"{last_state or 'not visible'}) {elapsed}/{timeout}s"
            )
            last_log_time = time.time()

        time.sleep(poll_interval)

    elapsed = int(time.time() - started)
    log.warning(
        f"  ContactQualityModal did not reach 'ok' state within "
        f"{timeout}s (last seen state: {last_state or 'not visible'})"
    )
    return False


def _assert_scan_started(timeout: int = 180) -> None:
    """Wait for the contact-quality pre-scan check, click ``Start Scan``,
    and assert the scan actually began.

    Wraps ``_wait_for_signal_quality_and_start_scan`` so every caller
    that performs ``click_panel('Start')`` immediately followed by a
    scan-running ``wait_with_log`` gets a hard failure instead of
    silently running out of phase. The ``incremental`` marker on the
    test classes will then skip the dependent steps cleanly.
    """
    if _wait_for_signal_quality_and_start_scan(timeout=timeout):
        return
    pytest.fail(
        "Pre-scan contact-quality check did not reach 'ok' and we "
        "never clicked 'Start Scan' — see prior WARNING lines for "
        "the modal state-transition trace. The scan did not actually "
        "start, so any subsequent wait_with_log / Stop-click would "
        "run out of phase. Common causes: laser-pulse hardware error "
        "during pre-scan, sensor mask doesn't match physical sensors, "
        "or modal stuck in 'checking' past the timeout."
    )


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
    # The previous Tab+Space fallback assumed the first ComboBox in
    # Settings was Time Window, which is only true in reduced mode. In
    # non-reduced mode the first ComboBox is Default Camera Left Sensor
    # — Tab+Space then opens the Right Sensor dropdown, leaves the
    # modal in a weird focus state, and (more dangerously) the next
    # spurious Space could cycle the Reduced Mode Enable PillSwitch
    # if focus has drifted. Either way the Auto-scale toggle didn't
    # actually flip.
    #
    # Only fall back when we can ANCHOR on the Time Window ComboBox
    # specifically (its label "Time window" is reachable via UIA even
    # when the PillSwitch isn't). If we can't, fail loudly rather than
    # blindly Tab+Space-ing into whatever currently has focus.
    log.info("  Label search missed; trying anchored Time Window → Tab → Space")
    try:
        time_window_label = None
        for elem in win.descendants(title="Time window"):
            time_window_label = elem
            break
        if time_window_label is None:
            for t in seen_texts:
                if t.lower().strip() == "time window":
                    log.info("  'Time window' present in UIA texts but title query missed")
                    break
            raise RuntimeError("'Time window' label not findable via UIA title query")

        # Click ~half-width right of the label — that's where the
        # StyledCombo sits in the FieldRow layout.
        rect = time_window_label.rectangle()
        label_cy = (rect.top + rect.bottom) // 2
        w = get_app_window()
        combo_x = int(w.left + 0.43 * w.width)
        log.info(f"  Clicking Time Window ComboBox at ({combo_x}, {label_cy})")
        pyautogui.click(combo_x, label_cy)
        time.sleep(0.3)
        # Close any popup the click may have opened, then Tab to
        # land on the next focusable item (Auto-scale PillSwitch in
        # reduced mode), Space to flip.
        pyautogui.press("escape")
        time.sleep(0.2)
        pyautogui.press("tab")
        time.sleep(0.2)
        pyautogui.press("space")
        time.sleep(SLEEP)
        log.info("  Anchored fallback engaged Auto-scale toggle")
        return
    except Exception as e:
        log.warning(f"  Anchored Time Window fallback failed: {e}")

    raise RuntimeError(
        "Could not locate the Auto-scale Y-axes toggle. Both the UIA "
        "label search and the anchored Time Window fallback failed; "
        "see the WARNING above for the UIA text dump. Refusing to "
        "blindly Tab+Space — that path historically toggled the wrong "
        "PillSwitch and pushed the running app into reduced mode, "
        "which hid Scan Settings for every downstream test."
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
    _assert_scan_started()
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
    """Run a manual scan in Reduced Mode via the keyboard, verify
    History.

    Reduced Mode is forced on in ``app_config.json`` at module-import
    time (see the module docstring); this class does not toggle it
    via the Settings modal. Scan Settings is NOT tested here — it is
    hidden while Reduced Mode is active.
    """

    # ── Notes: full feature test in Reduced Mode ─────────────────────────

    def test_05_open_notes(self, app):
        """Notes is now at the former Scan Settings position in the reduced sidebar."""
        move_window_on_screen()
        ensure_visible()
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
        # In Reduced Mode, the 'Good signal quality' dialog auto-appears.
        # _assert_scan_started fails the test (and short-circuits the
        # rest of the @incremental class) if the check never reaches
        # 'ok' — preventing test_15/16 from running on a non-existent
        # scan.
        _assert_scan_started()

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
# Mouse-based test class — Reduced Mode is forced on via
# app_config.json at module-import time (see module docstring)
# ─────────────────────────────────────────────
@pytest.mark.incremental
class TestReducedModeMouse:
    """Reduced Mode mouse workflow. Reduced Mode is forced on in
    ``app_config.json`` at module-import time.

    Scan Settings is NOT tested here — it is hidden while Reduced
    Mode is active.
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
        # In Reduced Mode, the 'Good signal quality' dialog auto-appears.
        # _assert_scan_started fails the test (and short-circuits the
        # rest of the @incremental class) if the check never reaches
        # 'ok' — preventing test_26/27 from running on a non-existent
        # scan.
        _assert_scan_started()

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

    Reduced Mode is forced on in ``app_config.json`` at module-import
    time (see the module docstring); all three classes in this module
    run with Reduced Mode already active.
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


# ─────────────────────────────────────────────
# Modal exclusivity — does the app keep only one modal open at a time?
# ─────────────────────────────────────────────
#
# Regression guard for the modal-stacking bug originally observed
# during test_33: opening the Settings modal, then clicking Start
# before closing it, left the Settings modal visible *behind* the
# freshly-opened ContactQualityModal because nothing in the
# onStartStopClicked path closed open modals first.
#
# The fix introduced ModalManager (components/ModalManager.qml),
# which BloodFlow.qml uses for every icon-bar action:
#   * Toggle buttons (Settings/Notes/History/Log/ScanSettings) call
#     ``modalManager.toggle(target)`` — which closes whatever else is
#     open before opening ``target``.
#   * Action buttons (Start/Check) call
#     ``modalManager.closeCurrent()`` before performing the action,
#     ensuring no underlying modal stays buried.
# Modals can opt out via a ``dismissable`` property
# (e.g. ContactQualityModal.dismissable is false during ``checking``,
# pre-scan gating, or live-scan warnings).
#
# This test opens Settings, clicks Start, and asserts that at no
# point during the post-Start window are two modals simultaneously
# visible. If it fails, the ModalManager wiring in BloodFlow.qml has
# regressed.
class TestModalExclusivity:
    """Detector test for the bug where two modals end up stacked.

    Standalone class (not @incremental) so it can run independently
    of the keyboard / mouse / settings-feature flows. Cleanup tries
    to dismiss the contact-quality modal and any leftover overlay so
    subsequent tests in the session aren't poisoned, but the
    detection assertion itself is the point.
    """

    def _force_clean_modal_state(self, max_escape_presses: int = 6) -> None:
        """Spam Escape until ``visible_modals()`` reports an empty
        set (or we run out of presses). Idempotent — pressing Escape
        with no modal open is harmless."""
        require_focus()
        for i in range(max_escape_presses):
            if not visible_modals():
                return
            pyautogui.press("escape")
            time.sleep(0.4)
        leftover = visible_modals()
        if leftover:
            log.warning(
                f"  could not dismiss all modals after "
                f"{max_escape_presses} Escapes; still visible: "
                f"{sorted(leftover)}"
            )

    def _try_dismiss_contact_quality(self, timeout_sec: int = 140) -> bool:
        """Wait up to ``timeout_sec`` for ContactQualityModal to leave
        the ``checking`` state (no Dismiss button is rendered while
        checking — see ContactQualityModal.qml line 505 footer
        visibility), then click ``Dismiss``. Returns True on success.
        """
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                win = uia_window()
                for elem in win.descendants():
                    try:
                        if elem.window_text().strip() == "Dismiss":
                            rect = elem.rectangle()
                            cx = (rect.left + rect.right) // 2
                            cy = (rect.top + rect.bottom) // 2
                            log.info(
                                f"  clicking ContactQuality 'Dismiss' "
                                f"at ({cx}, {cy})"
                            )
                            pyautogui.click(cx, cy)
                            time.sleep(SLEEP)
                            return True
                    except Exception:
                        continue
            except Exception as e:
                log.warning(f"  Dismiss-button search failed: {e}")
            time.sleep(2)
        log.warning(
            f"  ContactQuality 'Dismiss' button never appeared within "
            f"{timeout_sec}s — leaving modal in current state"
        )
        return False

    def test_38_settings_then_start_does_not_stack_modals(self, app):
        """Open Settings, click Start, expect only one modal visible
        at any point during the contact-quality pre-scan check.

        In reduced mode ``onStartStopClicked`` opens
        ContactQualityModal without first dismissing whatever modal
        is already on screen — so Settings stays at z=9998 underneath
        ContactQualityModal at z=9999. This test polls
        ``visible_modals()`` over a 25-second window after Start and
        fails if any single snapshot reports more than one modal.

        25 s spans the full ``checking → ok`` transition (the check
        itself takes ~9 s; we add buffer for slow first-after-launch
        bench startup), and we sample every 1.5 s — fine enough to
        catch the stacked state across the entire post-checking
        period when the QML buttons actually surface in UIA.

        Why poll instead of single-shot: the QML
        ContactQualityModal renders no UIA-friendly elements during
        its ``checking`` state (the title is a plain ``Text`` —
        invisible to UIA in this app — and the footer ``RowLayout``
        is hidden via ``visible: state_ !== 'checking'`` so the
        ``Start Scan`` / ``Dismiss`` buttons don't render until the
        check finishes). A snapshot taken in the first ~9 s after
        Start would correctly detect Settings but miss
        ContactQuality, even though the user can see it on screen.
        """
        move_window_on_screen()
        ensure_visible()

        self._force_clean_modal_state()
        before = visible_modals()
        assert not before, (
            f"Expected no modal visible before the test, but "
            f"{sorted(before)} were already open. A prior test left "
            f"the UI dirty."
        )

        click_panel("Settings")
        time.sleep(SLEEP)
        with_settings = visible_modals()
        assert with_settings == {"Settings"}, (
            f"Expected only the Settings modal after click, got "
            f"{sorted(with_settings)}."
        )
        log.info(f"  Settings opened cleanly: visible_modals()={sorted(with_settings)}")

        # Click Start without dismissing Settings first. In reduced
        # mode this fires the contact-quality pre-scan check, which
        # opens ContactQualityModal at z=9999 over Settings at z=9998.
        click_panel("Start")

        snapshots: list[tuple[float, set[str]]] = []
        poll_window_sec = 25
        poll_interval_sec = 1.5
        worst: set[str] = set()
        started = time.time()
        deadline = started + poll_window_sec

        try:
            while time.time() < deadline:
                snap = visible_modals()
                snapshots.append((time.time() - started, snap))
                if len(snap) > len(worst):
                    worst = set(snap)
                if len(snap) >= 2:
                    log.warning(
                        f"  Multi-modal detected at t={time.time() - started:.1f}s: "
                        f"{sorted(snap)}"
                    )
                    break
                time.sleep(poll_interval_sec)

            log.info(
                f"  Polled visible_modals() {len(snapshots)} times over "
                f"{time.time() - started:.1f}s while Settings was open and "
                f"Start was clicked. Worst snapshot: {sorted(worst)}."
            )
            for t_offset, snap in snapshots:
                log.info(f"    t={t_offset:5.1f}s  modals={sorted(snap)}")

            assert len(worst) <= 1, (
                f"Multiple modals visible at the same time: {sorted(worst)}. "
                f"Expected the icon-bar Start handler in BloodFlow.qml "
                f"to call modalManager.closeCurrent() before opening "
                f"ContactQualityModal, so any modal already on screen "
                f"(here: Settings) gets saved + hidden first. Either "
                f"the closeCurrent() call was removed, or "
                f"ModalManager's `modals` list no longer includes the "
                f"underlying modal, or the underlying modal exposes "
                f"dismissable=false when it shouldn't."
            )
        finally:
            log.info("  cleanup: dismissing contact-quality + any leftover modal")
            self._try_dismiss_contact_quality(timeout_sec=140)
            self._force_clean_modal_state()
