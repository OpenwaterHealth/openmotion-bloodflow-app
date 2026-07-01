"""
test_calibration_target_isolation.py — verify per-target calibration
isolation on the console EEPROM (issue #117).

Marker: ``release`` (~6-8 min — three full calibrations, three app
restarts, three console-JSON dumps).

What it covers
--------------
The Calibration target dropdown in Settings selects ``Both`` / ``Left`` /
``Right``. Per-target writes must update only the chosen module's row
in the (2, 8) ``C_min`` / ``C_max`` / ``I_min`` / ``I_max`` arrays
stored under ``"calibration"`` in the console's EEPROM JSON. The other
module's row must remain byte-identical.

Real phantom calibration drifts run-to-run, so we cannot byte-compare
same-side values across iterations. We can byte-compare the *untouched*
side, which is what this test does, in addition to numerical validity
checks on the side that was just written.

Procedure (one method, three iterations):
  1. ``Both``  — relaunch, target=Both, calibrate. Close app, dump
     console JSON, snapshot the ``calibration`` block as the baseline.
  2. ``Left``  — relaunch, target=Left, calibrate. Close app, dump.
     • Right module row must equal the baseline right row byte-for-byte.
     • Left module row must satisfy ``parse_calibration`` (finite,
       shape (2, 8), ``C_max > C_min``, ``I_max > I_min``).
  3. ``Right`` — relaunch, target=Right, calibrate. Close app, dump.
     • Left module row must equal the step-2 left row byte-for-byte.
     • Right module row must satisfy ``parse_calibration``.

Skip / fail behavior
--------------------
- ``Calibration Aborted`` (e.g., no phantom on the bench) → skip the
  iteration with a clear message; nothing was written, so isolation
  isn't observable.
- ``Calibration Failed`` is still a valid datapoint: the EEPROM write
  in phase 3 of ``CalibrationWorkflow`` (write_calibration) precedes
  the validation phase, so the JSON has been updated regardless of the
  Pass/Fail verdict.
- ``Calibration Passed`` is the happy path.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import psutil
import pyautogui
import pytest
from pywinauto import findwindows

from conftest import (
    PROJECT_ROOT,
    SLEEP,
    ensure_visible,
    log,
    uia_window,
)
from hil_helpers import (
    RE_CONNECTED,
    click_panel,
    find_app_log,
    force_app_config_value,
    recalibrate_panel_buttons,
    wait_for_pattern,
    write_app_config_value,
)

pytestmark = pytest.mark.release

# Calibration controls live in the engineeringMode-gated "Engineering" card
# of the Settings modal. The app ships with engineeringMode=false, so force
# it true on disk before the test's app relaunches read the config;
# restore the original value when the module finishes.
_INITIAL_ENGINEERING_MODE = force_app_config_value("engineeringMode", True)


@pytest.fixture(scope="module", autouse=True)
def _restore_engineering_mode():
    yield
    write_app_config_value("engineeringMode", _INITIAL_ENGINEERING_MODE)

# Calibration on real hardware: phase-0 flash (~5–15 s) + 2 sub-scans
# of ~6 s each + compute + write + validation. Field runs settle
# around 25–45 s; cap at 180 s for safety. Same value as
# ``test_calibration_ui._TERMINAL_WAIT_SEC`` so the budgets stay
# consistent across the calibration tests.
_TERMINAL_WAIT_SEC = 180

_TERMINAL_TEXTS = (
    "Calibration Passed",
    "Calibration Failed",
    "Calibration Aborted",
)
_RUNNING_PREFIX = "Calibrating..."

# Wall-clock budget for the SDK to reach CONNECTED after each relaunch.
_APP_CONNECT_TIMEOUT = 60

# RowLayout layout in components/SettingsModal.qml's Calibration card:
#   ActionButton(width=160) — spacing(12) — StyledCombo(width=110) — …
# Combo's horizontal center, measured from the Run Calibration button's
# right edge, is ``spacing + combo_width / 2 = 12 + 55 = 67`` px. Vertical
# center is the button's vertical center.
_COMBO_X_OFFSET_FROM_BTN_RIGHT = 67

# Calibration JSON keys (mirror omotion.Calibration constants — but kept
# local so the test does not depend on those import names).
_CAL_KEY = "calibration"
_C_MIN = "C_min"
_C_MAX = "C_max"
_I_MIN = "I_min"
_I_MAX = "I_max"
_ALL_ARRAY_KEYS = (_C_MIN, _C_MAX, _I_MIN, _I_MAX)
_EXPECTED_SHAPE = (2, 8)  # (modules, cams_per_module)


# ─────────────────────────────────────────────
# App lifecycle helpers
# ─────────────────────────────────────────────
# Inline rather than reusing conftest's session-scoped ``app`` fixture
# — we relaunch the app three times within a single method.
def _from_source_mode() -> bool:
    return os.environ.get("OPENWATER_FROM_SOURCE", "").lower() in (
        "1", "true", "yes",
    )


def _find_exe() -> str | None:
    env = os.environ.get("OPENWATER_EXE", "")
    if env and os.path.exists(env):
        return env
    patterns = [
        r"C:\Users\*\Documents\OpenMotion\**\Open-Motion.exe",
        r"C:\Users\*\Desktop\**\Open-Motion.exe",
        r"C:\Program Files\**\Open-Motion.exe",
        r"C:\Program Files (x86)\**\Open-Motion.exe",
    ]
    matches: list[str] = []
    for p in patterns:
        matches.extend(glob.glob(p, recursive=True))
    return max(matches, key=os.path.getmtime) if matches else None


def _kill_bloodflow_processes() -> int:
    killed = 0
    from_source = _from_source_mode()
    for proc in psutil.process_iter(["name", "cmdline", "pid"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if from_source:
                cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                match = (
                    "python" in name
                    and "main.py" in cmdline
                    and "openmotion-bloodflow-app" in cmdline
                )
            else:
                match = "openwater" in name
            if not match:
                continue
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                proc.kill()
            killed += 1
            log.info(f"  killed bloodflow pid={proc.info['pid']}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return killed


def _launch_app() -> None:
    if _from_source_mode():
        main_py = PROJECT_ROOT / "main.py"
        if not main_py.exists():
            pytest.fail(f"main.py not found at {main_py}")
        log.info(f"  launching from source: {sys.executable} {main_py}")
        subprocess.Popen([sys.executable, str(main_py)], cwd=str(PROJECT_ROOT))
    else:
        exe = _find_exe()
        if not exe:
            pytest.fail("Open-Motion.exe not found and OPENWATER_EXE unset")
        log.info(f"  launching: {exe}")
        subprocess.Popen([exe])


def _wait_for_app_window(timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ensure_visible():
            return True
        time.sleep(1)
    return False


def _wait_for_connect(timeout: int = _APP_CONNECT_TIMEOUT) -> str | None:
    log_path = find_app_log()
    if log_path is None:
        return None
    return wait_for_pattern(RE_CONNECTED, log_path, 0, timeout)


def _restart_app(label: str) -> None:
    """Kill, relaunch, wait-for-window, recalibrate panel, wait-for-connect."""
    log.info(f"  [{label}] relaunching app")
    _kill_bloodflow_processes()
    # The console UART takes a moment to be fully released after the
    # bloodflow process exits; the next launch's connection attempt
    # races the OS handle teardown otherwise.
    time.sleep(2)
    _launch_app()
    assert _wait_for_app_window(timeout=30), (
        f"[{label}] app window did not appear within 30 s of launch"
    )
    recalibrate_panel_buttons()
    line = _wait_for_connect(timeout=_APP_CONNECT_TIMEOUT)
    assert line, (
        f"[{label}] app did not reach CONNECTED within "
        f"{_APP_CONNECT_TIMEOUT} s of relaunch"
    )
    log.info(f"  [{label}] connected: {line.strip()}")
    time.sleep(SLEEP)  # let camera auto-config settle


# ─────────────────────────────────────────────
# Settings modal helpers
# ─────────────────────────────────────────────
def _open_settings() -> None:
    ensure_visible()
    click_panel("Settings")
    time.sleep(SLEEP)


def _close_settings() -> None:
    ensure_visible()
    pyautogui.press("escape")
    time.sleep(SLEEP)


def _scroll_settings_to_bottom() -> None:
    """Scroll the Settings modal's ScrollView all the way to the bottom.

    The Calibration card lives near the bottom of Settings. UIA's
    ``BoundingRectangle`` for items clipped by a QML ScrollView reports
    the logical *content* position rather than the on-screen position,
    so a click at ``btn.rectangle()`` coords lands wherever the card
    *would* be if the viewport were tall enough — typically off the
    bottom of the window, on the desktop. Scrolling so the card is
    actually in the viewport reconciles logical and on-screen coords
    before we read positions.
    """
    ensure_visible()
    win = uia_window()
    rect = win.rectangle()
    cx = (rect.left + rect.right) // 2
    cy = (rect.top + rect.bottom) // 2
    # Park the cursor over the modal so the wheel events route to its
    # ScrollView and not whatever lives behind the app.
    pyautogui.moveTo(cx, cy)
    # Each scroll click is ~120 px on Windows; 30 clicks of -300
    # comfortably exceeds the height of the Settings modal even on a
    # 4K display.
    for _ in range(30):
        pyautogui.scroll(-300)
    time.sleep(0.4)


def _find_run_calibration_button():
    """Return the UIA element for 'Run Calibration', or None if not present.

    The button is anchored by its ``Run Calibration`` accessible title
    — same pattern as ``test_calibration_ui._click_run_calibration``.
    """
    try:
        win = uia_window()
        btn = win.child_window(title="Run Calibration", control_type="Button")
        if btn.exists(timeout=3):
            return btn
    except (RuntimeError, findwindows.ElementNotFoundError):
        pass
    return None


def _find_calibration_target_combo():
    """Locate the calibration target ComboBox in the Settings modal.

    Identified by content: it is the only combo in the app whose
    currentText is one of ``"Both"`` / ``"Left"`` / ``"Right"``.
    """
    win = uia_window()
    for cb in win.descendants(control_type="ComboBox"):
        try:
            text = (cb.window_text() or "").strip()
        except Exception:
            continue
        if text in ("Both", "Left", "Right"):
            return cb
    return None


def _select_target(target_label: str) -> None:
    """Drive the calibration target combo by SetFocus + arrow keys.

    Earlier coord-based attempts to click the dropdown items missed —
    the dropdown popup's UIA rectangle is unreliable when the Settings
    ScrollView has clipped the parent card, and the popup itself opens
    above/below depending on screen-edge proximity. SetFocus is
    coord-free (UIA pattern), and arrow keys on a focused QtQuick.Controls
    ComboBox change the currentIndex synchronously without needing the
    popup to be open at all.
    """
    if target_label not in ("Both", "Left", "Right"):
        raise ValueError(f"unknown target label {target_label!r}")

    combo = _find_calibration_target_combo()
    if combo is None:
        raise RuntimeError(
            "calibration target combo not found — expected a ComboBox "
            "whose value is one of Both/Left/Right"
        )
    log.info(f"  found target combo; current value: {combo.window_text()!r}")

    # Focus via UIA — no screen coords involved.
    try:
        combo.set_focus()
    except Exception as e:
        log.warning(f"  combo.set_focus() raised: {e}")
    time.sleep(0.2)

    # Reset to index 0 (Both): Up presses past the top are no-ops, so
    # 5 presses guarantee we're at the top regardless of the prior
    # selection. Then Down N times to land on the target.
    target_idx = {"Both": 0, "Left": 1, "Right": 2}[target_label]
    for _ in range(5):
        pyautogui.press("up")
    time.sleep(0.1)
    for _ in range(target_idx):
        pyautogui.press("down")
    time.sleep(0.2)

    # If the dropdown popup is open, the highlighted item still needs
    # an Enter to commit on some Qt versions. Read currentText; if it
    # already matches, skip Enter (which would otherwise re-trigger
    # actionable focus). Otherwise press Enter and re-check.
    new_text = (combo.window_text() or "").strip()
    if new_text != target_label:
        pyautogui.press("enter")
        time.sleep(0.3)
        new_text = (combo.window_text() or "").strip()
    if new_text != target_label:
        # Close any popup before raising so teardown isn't blocked
        pyautogui.press("escape")
        raise RuntimeError(
            f"failed to select target {target_label!r}; "
            f"combo currentText is {new_text!r}"
        )
    log.info(f"  selected calibration target = {target_label}")


def _click_run_calibration() -> None:
    btn = _find_run_calibration_button()
    if btn is None:
        raise RuntimeError("Run Calibration button not found in Settings modal")
    log.info("  invoking 'Run Calibration' via UIA")
    btn.click()
    time.sleep(SLEEP)


def _read_calibration_status_text() -> str:
    try:
        win = uia_window()
        for elem in win.descendants():
            try:
                text = (elem.window_text() or "").strip()
            except Exception:
                continue
            if not text:
                continue
            if text.startswith(_RUNNING_PREFIX) or text in _TERMINAL_TEXTS:
                return text
    except (RuntimeError, findwindows.ElementNotFoundError):
        pass
    return ""


def _poll_for_terminal_state(timeout_sec: int) -> str:
    deadline = time.monotonic() + timeout_sec
    last_seen = ""
    last_log_at = 0.0
    while time.monotonic() < deadline:
        text = _read_calibration_status_text()
        if text:
            last_seen = text
            if text in _TERMINAL_TEXTS:
                return text
        now = time.monotonic()
        if now - last_log_at > 5.0:
            log.info(f"  calibration status: {last_seen or '(none yet)'}")
            last_log_at = now
        time.sleep(0.5)
    return last_seen


# ─────────────────────────────────────────────
# Console JSON dump
# ─────────────────────────────────────────────
def _dump_console_calibration(out_path: Path) -> dict:
    """Open a fresh SDK connection, read the console EEPROM JSON, write
    a forensic copy to ``out_path``, and return the ``"calibration"``
    sub-dict.

    The bloodflow app **must** be killed before calling this. The
    console UART is held exclusively by whichever process opens it
    first, so calling this with the app running will hang or fail.

    Read-side of the dump runs in-process against the installed
    ``omotion`` package rather than spawning ``scripts/test_user_config.py``
    — that script imports a stale ``omotion.Interface`` /
    ``acquire_motion_interface()`` API that no longer exists in the SDK.
    """
    from omotion import MotionInterface

    log.info("  opening SDK interface to dump console JSON")
    interface = MotionInterface()
    # Don't block in start() — it only waits 2 s by default for already-attached
    # devices. Run the proper wait below so we get a useful timeout message.
    interface.start(wait=False)
    try:
        # Console enumeration + ping handshake is ~5 s normally; 30 s
        # covers the post-power-cycle reconnect storm.
        ready = interface.wait_for_ready(
            console=True, sensors=0, timeout=30.0,
        )
        if not ready or not interface.console.is_connected():
            raise RuntimeError(
                "SDK console did not reach CONNECTED within 30 s during dump"
            )
        config = interface.console.read_config()
        if config is None:
            raise RuntimeError("interface.console.read_config() returned None")
        # Forensic copy on disk so a failing assertion still has the
        # raw dump available in tmp_path.
        out_path.write_text(json.dumps(config.json_data, indent=2))
        cal = config.json_data.get(_CAL_KEY)
        if cal is None:
            raise RuntimeError(
                f"no '{_CAL_KEY}' key in console JSON. Keys: "
                f"{sorted(config.json_data)}"
            )
        log.info(f"  dumped calibration block: {json.dumps(cal)[:200]}…")
        return cal
    finally:
        try:
            interface.stop()
        except Exception as e:
            log.warning(f"  interface.stop() raised: {e}")


# ─────────────────────────────────────────────
# Numerical assertions
# ─────────────────────────────────────────────
def _arrays_from_block(cal: dict) -> dict[str, np.ndarray]:
    """Convert the four calibration arrays from JSON lists to numpy.

    Raises ``AssertionError`` on missing keys or wrong shape.
    """
    out: dict[str, np.ndarray] = {}
    for key in _ALL_ARRAY_KEYS:
        assert key in cal, f"calibration block missing {key!r}: {sorted(cal)}"
        arr = np.asarray(cal[key], dtype=float)
        assert arr.shape == _EXPECTED_SHAPE, (
            f"{key} has shape {arr.shape}, expected {_EXPECTED_SHAPE}"
        )
        out[key] = arr
    return out


def _assert_calibration_valid(cal: dict, label: str) -> None:
    """Assert the four arrays are finite and ``C_max > C_min``,
    ``I_max > I_min`` everywhere — same property ``parse_calibration``
    enforces in the SDK."""
    arrs = _arrays_from_block(cal)
    for key, arr in arrs.items():
        assert np.all(np.isfinite(arr)), (
            f"[{label}] {key} contains NaN or inf:\n{arr}"
        )
    assert np.all(arrs[_C_MAX] > arrs[_C_MIN]), (
        f"[{label}] C_max not strictly > C_min element-wise:\n"
        f"C_min=\n{arrs[_C_MIN]}\nC_max=\n{arrs[_C_MAX]}"
    )
    assert np.all(arrs[_I_MAX] > arrs[_I_MIN]), (
        f"[{label}] I_max not strictly > I_min element-wise:\n"
        f"I_min=\n{arrs[_I_MIN]}\nI_max=\n{arrs[_I_MAX]}"
    )


def _assert_module_row_unchanged(
    label: str, before: dict, after: dict, module_idx: int,
) -> None:
    """Assert the ``module_idx`` row of every calibration array is
    byte-identical between ``before`` and ``after`` dumps."""
    before_arrs = _arrays_from_block(before)
    after_arrs = _arrays_from_block(after)
    for key in _ALL_ARRAY_KEYS:
        b = before_arrs[key][module_idx]
        a = after_arrs[key][module_idx]
        assert np.array_equal(b, a), (
            f"[{label}] {key}[{module_idx}] changed when target should "
            f"have left it untouched.\nbefore: {b}\nafter:  {a}"
        )


# ─────────────────────────────────────────────
# Test
# ─────────────────────────────────────────────
def test_calibration_target_isolation(app, tmp_path):
    """Calibrate Both → Left → Right; assert un-targeted module rows
    never change byte-for-byte, and the targeted module row stays
    numerically valid.

    The first iteration reuses the session-scoped ``app`` fixture (no
    extra launch); iterations 2 and 3 must relaunch because the
    previous iteration killed the process to release the console UART
    for the JSON dump.
    """

    iterations = (
        ("Both",  None),       # baseline
        ("Left",  1),          # right module (index 1) must be untouched
        ("Right", 0),          # left module (index 0) must be untouched
    )

    prev_dump: dict | None = None  # most recent dump, used by the next iteration

    for i, (label, untouched_idx) in enumerate(iterations):
        # ── 1. Launch and reach CONNECTED ───────────────────────────
        if i == 0:
            log.info(f"  [{label}] using already-launched app from session fixture")
            ensure_visible()
        else:
            _restart_app(label)

        # ── 2. Drive the calibration ───────────────────────────────
        _open_settings()
        try:
            _scroll_settings_to_bottom()
            _select_target(label)
            _click_run_calibration()
            final = _poll_for_terminal_state(_TERMINAL_WAIT_SEC)
            log.info(f"  [{label}] terminal status: {final!r}")
        finally:
            _close_settings()

        if final == "Calibration Aborted":
            pytest.skip(
                f"[{label}] calibration aborted — likely no phantom on "
                f"the bench. Skipping isolation check (no EEPROM write "
                f"happened)."
            )
        assert final in _TERMINAL_TEXTS, (
            f"[{label}] calibration did not reach a terminal state within "
            f"{_TERMINAL_WAIT_SEC} s. Last observed text: {final!r}."
        )

        # ── 3. Close the app so the SDK can grab the UART ──────────
        log.info(f"  [{label}] killing app to release console UART for dump")
        _kill_bloodflow_processes()
        time.sleep(2)

        # ── 4. Dump the console JSON ───────────────────────────────
        out = tmp_path / f"console_cal_{label.lower()}.json"
        cal = _dump_console_calibration(out)

        # ── 5. Numerical sanity always ─────────────────────────────
        _assert_calibration_valid(cal, label)

        # ── 6. Isolation property when applicable ──────────────────
        if untouched_idx is not None:
            assert prev_dump is not None, (
                "internal: prev_dump should be set after the baseline iteration"
            )
            _assert_module_row_unchanged(label, prev_dump, cal, untouched_idx)
            log.info(
                f"  [{label}] module row {untouched_idx} unchanged — "
                f"isolation holds"
            )

        prev_dump = cal
