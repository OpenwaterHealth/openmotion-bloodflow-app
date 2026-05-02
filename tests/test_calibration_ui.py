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
    "Calibration Failed", or "Calibration Aborted").
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
from utils import click_panel

pytestmark = pytest.mark.dev

# Calibration on real hardware: phase-0 flash (~5-15 s) + 2 sub-scans
# of ~6 s each + compute + write_calibration. Field runs settle around
# 25-40 s; cap at 180 s for safety.
_TERMINAL_WAIT_SEC = 180

_TERMINAL_TEXTS = (
    "Calibration Passed",
    "Calibration Failed",
    "Calibration Aborted",
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
    """Click the 'Run Calibration' ActionButton inside the Settings modal."""
    win = uia_window()
    # The Settings modal's ActionButton renders as a UIA Button with
    # the literal label text. Search descendants — covers both Button
    # and Custom control types depending on the QML→UIA bridge.
    for ct in ("Button", "Custom", "Pane", "Group"):
        try:
            btn = win.child_window(title="Run Calibration", control_type=ct)
            if btn.exists(timeout=2):
                rect = btn.rectangle()
                cx = (rect.left + rect.right) // 2
                cy = (rect.top + rect.bottom) // 2
                log.info(f"  click 'Run Calibration' (type={ct}) at ({cx}, {cy})")
                import pyautogui
                pyautogui.moveTo(cx, cy, duration=0.3)
                pyautogui.click(cx, cy)
                time.sleep(SLEEP)
                return
        except Exception:
            continue

    # Fallback: descendants by name
    matches = win.descendants(title="Run Calibration")
    if matches:
        elem = matches[0]
        rect = elem.rectangle()
        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        log.info(f"  click 'Run Calibration' (descendant) at ({cx}, {cy})")
        import pyautogui
        pyautogui.moveTo(cx, cy, duration=0.3)
        pyautogui.click(cx, cy)
        time.sleep(SLEEP)
        return
    raise RuntimeError("Could not find 'Run Calibration' button in the Settings modal")


def _read_calibration_status_text() -> str:
    """Walk every descendant of the app window (regardless of
    control_type) and return the first window_text that matches one
    of the calibration status patterns.

    QML's Label element inconsistently surfaces through the UIA
    bridge — sometimes as Text, sometimes as Custom / Pane / Group —
    so a control_type filter loses the status label on real runs.
    Iterating all descendants is slower but reliable. Empty string
    if no calibration-related text is visible.
    """
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
            if text.startswith(_RUNNING_PREFIX) or text in _TERMINAL_TEXTS:
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
            if text in _TERMINAL_TEXTS:
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

        # Confirm the click actually triggered the procedure. Within a
        # couple of seconds the status text should switch into either
        # the running counter ("Calibrating... (Ns / 600s)") OR — if
        # phase 0 already failed — directly to a terminal label.
        kicked_off = False
        kickoff_deadline = time.monotonic() + 10.0
        while time.monotonic() < kickoff_deadline:
            text = _read_calibration_status_text()
            if text and (text.startswith(_RUNNING_PREFIX) or text in _TERMINAL_TEXTS):
                log.info(f"  calibration kickoff confirmed: status={text!r}")
                kicked_off = True
                break
            time.sleep(0.3)
        assert kicked_off, (
            "Click on 'Run Calibration' did not produce a status update "
            "within 10 s — the connector slot may not be wired or the "
            "QML property binding may be broken."
        )

        # Wait for terminal state.
        final = _poll_for_terminal_state(_TERMINAL_WAIT_SEC)
        log.info(f"  final calibration status: {final!r}")
        assert final in _TERMINAL_TEXTS, (
            f"Calibration did not reach a terminal state within "
            f"{_TERMINAL_WAIT_SEC} s. Last observed text: {final!r}. "
            f"Expected one of {_TERMINAL_TEXTS}."
        )
    finally:
        _close_settings()
