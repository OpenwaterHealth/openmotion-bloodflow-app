"""
test_scan_flow.py — end-to-end scan flow happy path.

Marker: ``release`` (~10 min wall-clock; only runs on release-pattern
tag pushes).

What it covers
--------------
The full from-cold-start scan workflow as a single sequential
incremental class:

  1. Open Scan Settings, set duration to 2 min via Tab navigation.
  2. Open the Notes modal and type a session note.
  3. Run the contact-quality Check (waits up to 2 min, dismisses the
     "Good signal quality" modal).
  4. Click Start, wait for the scan to complete (~5 min budget).
  5. Dismiss the auto-opened Session Notes modal.
  6. Open History, verify the latest scan is selected, open it in
     the embedded PlotViewer ("View in plot →"), close everything.

Preconditions
-------------
- Console + at least one sensor connected over USB.
- ``OPENWATER_EXE`` set or ``OPENWATER_FROM_SOURCE=1``.
- App is launched (handled by the session ``app`` fixture).

Why this is in the release tier
-------------------------------
Real laser firing for 2 minutes plus FPGA configuration. Roughly
10 min of wall-clock per run; not appropriate for the dev-tier HIL
chain that fires on every push to ``next``.
"""

import time
from datetime import datetime

import pyautogui
import pytest

from conftest import (
    SLEEP,
    click_by_name,
    ensure_visible,
    get_app_window,
    log,
    require_focus,
    uia_window,
    wait_with_log,
)
from hil_helpers import (
    click_panel,
    dismiss_signal_quality_modal,
)

pytestmark = pytest.mark.release

# This test opens Scan Settings (test_01) to set the 2-min duration.
# Scan Settings is hidden in clinical mode, so force the on-disk flag
# false before the session-scoped ``app`` fixture launches the app.
# Applied by conftest's pytest_collection_finish only when this module
# has selected tests; restored byte-exact at session end. Same pattern
# as test_history / test_scan_settings / test_usb_disconnect_freeze.
FORCE_APP_CONFIG = {"clinicalMode": False}

SCAN_DURATION_MIN = 2
WAIT_AFTER_SCAN = SCAN_DURATION_MIN * 60 + 180  # scan + 3-min buffer
CHECK_WAIT_SEC = 120  # 2 minutes for Check to complete


def _run_check_step(label: str = ""):
    """Click Check, wait up to 2 min, dismiss 'Good signal quality' modal if shown."""
    log.info(f"  Clicking Check and waiting up to {CHECK_WAIT_SEC}s... {label}")
    click_panel("Check")
    elapsed = 0
    while elapsed < CHECK_WAIT_SEC:
        time.sleep(10)
        elapsed += 10
        if dismiss_signal_quality_modal():
            log.info(f"  Signal quality modal dismissed at {elapsed}s.")
            return
        if elapsed % 30 == 0:
            log.info(f"  Check running... {elapsed}/{CHECK_WAIT_SEC}s")
    dismiss_signal_quality_modal()
    log.info("  Check completed.")


@pytest.mark.incremental
class TestScanFlow:
    """End-to-end scan flow: settings -> notes -> scan -> history -> view in plot."""

    def test_01_open_scan_settings(self, app):
        click_panel("Scan\nSettings")

    def test_02_set_duration(self, app):
        """Set scan duration to 2 minutes through the modal's Edit controls.

        The old Tab walk assumed focus started before the User Label; on
        1.5.4-dev.4 it starts inside it, so the "2" landed in Hours and the
        scan ran for 7200 s (2026-10-14). The modal exposes its fields over
        UIA as Edits: "User Label:" + its value, then Hours, Minutes, Seconds.
        """
        from hil_helpers import click_element_center
        require_focus()
        deadline = time.time() + 15
        fields = []
        while time.time() < deadline and len(fields) < 3:
            edits = []
            for e in uia_window().descendants(control_type="Edit"):
                try:
                    edits.append(((e.window_text() or "").strip(), e))
                except Exception:
                    continue
            # the duration fields are the last three Edits (hours, minutes, seconds)
            numeric = [e for t, e in edits if t.replace(":", "").strip().isdigit() or t == ""]
            fields = numeric[-3:] if len(numeric) >= 3 else []
            if len(fields) < 3:
                time.sleep(0.5)
        assert len(fields) == 3, f"Scan Settings duration fields not exposed over UIA ({[t for t, _ in edits]})"
        for elem, value, label in zip(fields, ("0", str(SCAN_DURATION_MIN), "0"), ("Hours", "Minutes", "Seconds")):
            click_element_center(elem, label)
            time.sleep(0.3)
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.1)
            pyautogui.typewrite(value, interval=0.05)
            time.sleep(0.3)
            pyautogui.press("tab")        # commit via focus-loss
            time.sleep(0.3)
        log.info(f"  duration set to 0:{SCAN_DURATION_MIN:02d}:00 through the UIA Edits")

    def test_03_close_scan_settings(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    def test_04_type_note(self, app):
        click_panel("Notes")
        require_focus()
        self.session_note = f"AutoScan_{datetime.now():%Y%m%d_%H%M%S}"
        log.info(f"  typing note: '{self.session_note}'")
        pyautogui.typewrite(self.session_note, interval=0.04)
        time.sleep(SLEEP)

    def test_05_close_notes(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    def test_06_run_check(self, app):
        """Click Check and wait up to 2 min — dismiss 'Good signal quality' modal."""
        _run_check_step()

    def test_07_start_scan(self, app):
        click_panel("Start")

    def test_08_wait_for_scan(self, app):
        wait_with_log(WAIT_AFTER_SCAN,
                      f"{SCAN_DURATION_MIN}-minute scan + 3-minute buffer")

    def test_09_close_post_scan_notes(self, app):
        """Dismiss the auto-opened Notes modal (BloodFlow.qml opens it on scan finish)."""
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    def test_10_open_history(self, app):
        # History is a QML MouseArea sidebar button — use coordinate click,
        # not click_by_name (which searches the UIA tree and finds nothing).
        click_panel("History")

    def test_11_latest_scan_selected(self, app):
        """History.open() sets scanPicker index 0 (latest scan) automatically."""
        pass  # verified by the subsequent view-in-plot step

    def test_12_view_in_plot(self, app):
        """'View in plot →' loads the scan into the embedded PlotViewer
        and closes the History modal itself."""
        # The History rework (app #194) renamed the action: the Button now
        # reads "Load in viewer  →" (or "Load “<label>”  →" once a row is
        # focused). Match by prefix; keep the old label for older builds.
        deadline = time.time() + 10
        clicked = None
        while time.time() < deadline and not clicked:
            for b in uia_window().descendants(control_type="Button"):
                name = (b.window_text() or "").strip()
                if name.startswith("Load") or name == "View in plot →":
                    from hil_helpers import click_element_center
                    click_element_center(b, name)
                    clicked = name
                    break
            time.sleep(0.5)
        assert clicked, "no 'Load …' / 'View in plot →' button in the History modal"
        log.info(f"  clicked '{clicked}'")
        time.sleep(SLEEP)

    def test_13_close_history(self, app):
        """Defensive: History should already have closed itself on
        'View in plot →'; a stray Escape on the main page is a no-op."""
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)
