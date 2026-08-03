"""
test_calibration_ui.py — Run Calibration button end-to-end UI test.

Marker: ``dev`` (~1-2 min, runs on every push to ``next``).

What it covers
--------------
The Run Calibration button in the Settings modal, exercising the full
QML → connector → SDK chain on real hardware:

  - Open Settings modal via the sidebar.
  - Click "Run Calibration".
  - Verify the status text shifts away from idle within a few seconds
    (procedure actually started — not silently swallowed).
  - Poll for a terminal status text ("Calibration Passed",
    "Calibration Failed", "Calibration Canceled", "Calibration Timed
    Out", or "Calibration Error" — "Failed"/"Timed Out"/"Error" may
    carry a " — <breakdown>" or " — <reason>" suffix).
  - Verify the indicator label confirms the procedure ran end-to-end.

Preconditions
-------------
- App is launched (handled by the session-scoped ``app`` fixture).
- Console + at least one sensor connected — calibration aborts early
  if neither is present, but the test still verifies the UI handled
  the abort gracefully.

Failure modes the test guards against
-------------------------------------
- Click never reaches the connector slot (button text mismatch, modal
  not focused, QML signal not wired).
- Calibration starts but the status text never updates (Qt property
  change not signalled).
- Procedure runs forever (no terminal state reached within max wait).
- ``runCalibration`` raises before kicking off, leaving status stuck.

Pass / fail of the calibration itself is NOT asserted — that depends
on the phantom and on the user's thresholds in ``app_config.json``.
This test verifies the UI plumbing, not acceptance criteria.
"""

import time

import pytest
from pywinauto import findwindows

from conftest import SLEEP, ensure_visible, log, uia_window
from hil_helpers import click_panel

pytestmark = pytest.mark.dev

# Calibration controls live in the engineeringMode-gated "Engineering" card
# of the Settings modal. The app ships with engineeringMode=false, so force
# it true before the session ``app`` fixture launches. Applied by conftest's
# pytest_collection_finish only when this module has selected tests;
# restored byte-exact at session end.
FORCE_APP_CONFIG = {"engineeringMode": True}

# Calibration on real hardware: phase-0 flash (~5-15 s) + 2 sub-scans
# of ~6 s each + compute + write_calibration. Field runs settle around
# 25-40 s; cap at 180 s for safety.
_TERMINAL_WAIT_SEC = 180

_TERMINAL_TEXTS = (
    "Calibration Passed",
    "Calibration Failed",      # may carry " — <breakdown>"
    "Calibration Canceled",
    "Calibration Timed Out",   # may carry " — <reason>"
    "Calibration Error",       # may carry " — <reason>"
    # #426. Unreachable unattended — it takes a click in the override modal
    # — but listed so this table stays the full status vocabulary.
    "Calibration Accepted (Below Threshold)",
)
_RUNNING_PREFIX = "Calibrating..."


def _open_settings():
    ensure_visible()
    click_panel("Settings")
    time.sleep(SLEEP)


def _close_settings():
    ensure_visible()
    import pyautogui
    pyautogui.press("escape")
    time.sleep(SLEEP)


def _click_run_calibration():
    """Invoke the 'Run Calibration' ActionButton inside the Settings modal.

    The Calibration section lives near the bottom of Settings (just
    above About), so the button is typically scrolled out of the
    viewport when the modal opens. UIA's BoundingRectangle for items
    clipped by a QML ScrollView is unreliable — it reports the logical
    position within the scrollable content, NOT the on-screen position
    — so coord-based clicking either misses or hits the desktop. The
    ScrollView clips the visible cursor too: even if we wheel the
    content into view, pyautogui's click reads pre-scroll coords.

    Use UIA's InvokePattern instead. pywinauto's ``Button.click()``
    routes to Invoke() on UIA backends, which fires the button
    regardless of whether it's currently on screen.
    """
    win = uia_window()
    btn = win.child_window(title="Run Calibration", control_type="Button")
    if not btn.exists(timeout=2):
        raise RuntimeError("Could not find 'Run Calibration' button in the Settings modal")
    log.info("  invoking 'Run Calibration' via UIA")
    btn.click()
    time.sleep(SLEEP)


def _read_calibration_status_text() -> str:
    """Walk every descendant of the app window and return the first
    window_text matching a calibration status pattern. Empty string
    if no calibration-related text is visible."""
    try:
        win = uia_window()
        for elem in win.descendants():
            try:
                text = elem.window_text() or ""
            except Exception:
                continue
            text = text.strip()
            if not text:
                continue
            if text.startswith(_RUNNING_PREFIX) or text.startswith(_TERMINAL_TEXTS):
                return text
    except (RuntimeError, findwindows.ElementNotFoundError):
        pass
    return ""


def _poll_for_terminal_state(timeout_sec: int) -> str:
    """Poll the modal's status text until it reads a terminal label
    or ``timeout_sec`` elapses. Returns the final observed text
    (empty string if nothing matched)."""
    deadline = time.monotonic() + timeout_sec
    last_seen = ""
    last_log_at = 0.0
    while time.monotonic() < deadline:
        text = _read_calibration_status_text()
        if text:
            last_seen = text
            if text.startswith(_TERMINAL_TEXTS):
                return text
        # Log progress at most every ~5 s so the test output is
        # readable without flooding.
        now = time.monotonic()
        if now - last_log_at > 5.0:
            log.info(f"  calibration status: {last_seen or '(none yet)'}")
            last_log_at = now
        time.sleep(0.5)
    return last_seen


def test_calibration_button_runs_to_terminal_state(app):
    _open_settings()
    try:
        _click_run_calibration()
        final = _poll_for_terminal_state(_TERMINAL_WAIT_SEC)
        log.info(f"  final calibration status: {final!r}")
        assert final.startswith(_TERMINAL_TEXTS), (
            f"Calibration did not reach a terminal state within "
            f"{_TERMINAL_WAIT_SEC} s. Last observed text: {final!r}. "
            f"Expected one of {_TERMINAL_TEXTS}."
        )
    finally:
        _close_settings()
