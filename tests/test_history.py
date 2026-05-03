"""
History — modal and visualization tests.

Autouse fixture _seed_with_short_scan runs a 30-second Middle/Middle
scan before the test class executes so the History modal has at
least one entry to show. The fresh self-hosted runner doesn't have
prior scan data; this seeds it.
"""

import re
import time

import pyautogui
import pytest

from conftest import (
    SLEEP,
    click_by_name,
    ensure_visible,
    log,
    read_combobox_values,
    require_focus,
    uia_window,
    wait_with_log,
)
from utils import (
    SENSOR_OPTIONS,
    click_element_center,
    click_panel,
    close_plot_window,
    force_app_config_value,
    selected_scan_text,
    write_app_config_value,
)

pytestmark = pytest.mark.dev

VIZ_WAIT = 60  # seconds to leave each plot open

# Same rationale as test_scan_settings: this module's seed scan opens
# Scan Settings, which is hidden in reduced mode. Snapshot at module
# import (before the session-scoped ``app`` fixture spins up the app)
# so any fresh launch in this session boots in non-reduced mode;
# restore on teardown via the autouse fixture below.
_INITIAL_REDUCED_MODE = force_app_config_value("reducedMode", False)


@pytest.fixture(scope="module", autouse=True)
def _restore_reduced_mode_on_module_teardown():
    yield
    write_app_config_value("reducedMode", _INITIAL_REDUCED_MODE)

# How long to seed the history with at the start. Short to keep
# the dev-tier suite snappy, but long enough that the SDK actually
# writes scan data to disk.
_SEED_SCAN_DURATION_SEC = 30
_SEED_SENSOR_OPTION = "Middle"

# Wall-clock budget for the seed phase: scan duration + camera
# config (~75s on first scan after launch) + Session-Notes-modal
# settle. 4 minutes is conservative.
_SEED_MAX_WAIT_SEC = 240


# ─────────────────────────────────────────────
# Seed-scan helpers (mirrors the abbreviated auto-stop-bug test)
# ─────────────────────────────────────────────
def _wait_for_combobox(idx: int, timeout: int = 15):
    """Poll UIA up to ``timeout`` seconds for at least ``idx + 1``
    ComboBoxes to appear in the window. Self-hosted runner sometimes
    needs several seconds before Qt exposes modal contents through
    the accessibility bridge, especially on the first modal of the
    session.

    Returns the ComboBox element on success, or None on timeout.
    """
    deadline = time.time() + timeout
    last_count = -1
    while time.time() < deadline:
        ensure_visible()
        try:
            cbs = uia_window().descendants(control_type="ComboBox")
        except Exception:
            cbs = []
        if len(cbs) > idx:
            return cbs[idx]
        if len(cbs) != last_count:
            log.info(
                f"  waiting for ComboBox[{idx}]... "
                f"currently {len(cbs)} visible"
            )
            last_count = len(cbs)
        time.sleep(0.5)
    return None


def _click_combobox_by_index(idx: int) -> None:
    ensure_visible()
    elem = _wait_for_combobox(idx, timeout=15)
    if elem is None:
        pytest.skip(
            f"Scan Settings modal didn't expose ComboBox[{idx}] within "
            f"15 s — Qt accessibility bridge may not be reflecting "
            f"modal contents on this runner. Skipping the seed scan; "
            f"test_history.test_02_latest_scan_listed will fall back "
            f"to its original 'no scans found' assertion."
        )
    click_element_center(elem, f"ComboBox[{idx}]")


def _select_sensor(idx: int, side: str, option: str) -> None:
    ensure_visible()
    require_focus()
    log.info(f"  seed scan: {side} sensor → '{option}'")
    _click_combobox_by_index(idx)
    time.sleep(0.2)
    target = SENSOR_OPTIONS.index(option)
    pyautogui.press("home")
    time.sleep(0.2)
    for _ in range(target):
        pyautogui.press("down")
        time.sleep(0.15)
    pyautogui.press("return")
    time.sleep(0.3)
    values = read_combobox_values()
    assert len(values) > idx and values[idx] == option, (
        f"{side} sensor: expected '{option}', got values={values}"
    )


def _set_scan_duration(hours: int, minutes: int, seconds: int) -> None:
    """Write H/M/S into the duration spinboxes (whichever UIA control
    type the build exposes them as)."""
    require_focus()
    win = uia_window()
    log.info(
        f"  seed scan: duration → {hours:02d}:{minutes:02d}:{seconds:02d}"
    )
    for control_type in ["Edit", "SpinBox", "Custom"]:
        try:
            fields = win.descendants(control_type=control_type)
        except Exception:
            continue
        small = []
        for f in fields:
            try:
                rect = f.rectangle()
                if rect.right - rect.left >= 150:
                    continue
                txt = (f.window_text() or "").strip()
                if txt.isdigit() or txt == "":
                    small.append(f)
            except Exception:
                continue
        if len(small) >= 3:
            for elem, value, label in (
                (small[0], hours,   "Hours"),
                (small[1], minutes, "Minutes"),
                (small[2], seconds, "Seconds"),
            ):
                click_element_center(elem, f"{label} field")
                time.sleep(0.1)
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.1)
                pyautogui.typewrite(str(value), interval=0.05)
                time.sleep(0.1)
            return
    pytest.fail(
        "Could not locate three duration spinboxes in the Scan Settings "
        "modal — UIA tree may have changed."
    )


def _wait_for_session_notes_modal(timeout: int) -> bool:
    """Poll UIA for the Session Notes modal (which opens automatically
    when a scan completes)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            win = uia_window()
            for elem in win.descendants():
                try:
                    text = (elem.window_text() or "").strip().lower()
                except Exception:
                    continue
                if "session notes" in text:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


@pytest.fixture(scope="class", autouse=True)
def _seed_with_short_scan(app):
    """Run a 30-second Middle/Middle scan once before TestHistory runs
    so the History combo box has something to show. Idempotent: if
    a scan already exists in History (e.g. from a prior session on
    the same runner), the seed is skipped.
    """
    # Quick check: if History already has data, skip the seed.
    try:
        click_panel("History")
        time.sleep(SLEEP)
        if selected_scan_text():
            log.info("Skipping seed scan — History already has entries")
            require_focus()
            pyautogui.press("escape")
            time.sleep(SLEEP)
            yield
            return
        # Close the empty History modal before configuring a scan.
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)
    except Exception as e:
        log.warning(f"  pre-seed History probe failed: {e}")

    log.info(
        f"Seeding History with a {_SEED_SCAN_DURATION_SEC}s "
        f"{_SEED_SENSOR_OPTION}/{_SEED_SENSOR_OPTION} scan..."
    )
    click_panel("Scan\nSettings")
    time.sleep(SLEEP)
    _select_sensor(0, "Left",  _SEED_SENSOR_OPTION)
    _select_sensor(1, "Right", _SEED_SENSOR_OPTION)
    _set_scan_duration(0, _SEED_SCAN_DURATION_SEC // 60, _SEED_SCAN_DURATION_SEC % 60)
    require_focus()
    pyautogui.press("escape")  # close Scan Settings
    time.sleep(SLEEP)

    click_panel("Start")
    log.info(
        f"  seed scan started — waiting up to {_SEED_MAX_WAIT_SEC}s "
        f"for completion"
    )
    if not _wait_for_session_notes_modal(_SEED_MAX_WAIT_SEC):
        pytest.fail(
            f"Seed scan did not complete within {_SEED_MAX_WAIT_SEC}s "
            f"(no Session Notes modal). Cannot run TestHistory without "
            f"history data."
        )
    log.info("  seed scan complete — dismissing Session Notes modal")
    require_focus()
    pyautogui.press("escape")
    time.sleep(SLEEP)
    yield


def _is_history_open() -> bool:
    """True iff the History modal appears to be on top — detected via
    the presence of any ComboBox in the UIA tree (the scan picker is
    the only ComboBox the bloodflow page exposes when no other modal
    is up)."""
    try:
        win = uia_window()
        return bool(win.descendants(control_type="ComboBox"))
    except Exception:
        return False


def _ensure_history_open() -> None:
    """Open the History modal if it isn't already. The History panel
    button is a TOGGLE (BloodFlow.qml's onHistoryClicked: 'closeAll +
    open if it was closed') — clicking when the modal is open closes
    it. So check first, click only if needed.

    Combined with HistoryModal having no Escape handler, this matters:
    the seed fixture's pyautogui.press('escape') silently fails to
    close the modal, so without this check, test_01's click ends up
    closing the modal that the fixture left open."""
    if _is_history_open():
        log.info("  History modal already open — no click needed")
        return
    log.info("  History modal not open — clicking the History panel")
    click_panel("History")
    time.sleep(SLEEP)


@pytest.mark.incremental
class TestHistory:
    """History modal — scan listing and visualization."""

    def test_01_open(self, app):
        _ensure_history_open()

    def test_02_latest_scan_listed(self, app):
        # Defensive: re-ensure the modal is up before reading. The
        # seed fixture's close-with-escape doesn't actually fire
        # (HistoryModal has no Keys handler), so we can't trust the
        # state coming into this test.
        _ensure_history_open()
        # Poll for up to 5 s — the ComboBox can take a moment to
        # populate on a slow runner.
        deadline = time.time() + 5.0
        scan_text = ""
        while time.time() < deadline:
            scan_text = selected_scan_text()
            if scan_text:
                break
            time.sleep(0.3)
        assert len(scan_text) > 0, (
            "ComboBox is empty after 5 s -- no scans found. Run a "
            "scan first, or the History modal failed to open."
        )
        log.info(f"  Scan ComboBox text: '{scan_text}'")

    def test_03_visualize_bfi_bvi(self, app):
        click_by_name("Visualize BFI/BVI")
        wait_with_log(VIZ_WAIT, "BFI/BVI plot open")

    def test_04_close_bfi_plot(self, app):
        close_plot_window()

    def test_05_visualize_contrast_mean(self, app):
        ensure_visible()
        click_by_name("Visualize Contrast/Mean")
        wait_with_log(VIZ_WAIT, "Contrast/Mean plot open")

    def test_06_close_contrast_plot(self, app):
        close_plot_window()

    def test_07_close_history(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)
