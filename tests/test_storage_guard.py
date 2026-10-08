"""
test_storage_guard.py — low-storage guard on the data drive
(openmotion-bloodflow-app#506; the E-306 critical modal from #710).

The thresholds are compiled constants (``minFreeDiskMb`` = 1024,
``scanStopFreeDiskMb`` = 100), so the only way to trip them is real free
space. The test reserves space on the data drive with ``fsutil file
createnew`` (allocated, not sparse; instant; works unelevated) and
deletes the files again — the same recipe vpenn posted on the ticket.

Retest steps (boringethan, #506 after #710):
  1. Under 1 GB mid-scan: one "Storage is running low" warning toast and
     the scan keeps running.
  2. Under 100 MB mid-scan: the scan stops gracefully (data kept) and the
     E-306 critical modal shows; the legacy "Scan stopped" toast is gone.
  3. Dismiss the modal: the Session Notes modal opens.
  4. Press Start again while still under 1 GB: E-305.

Every step is asserted on the app log (the connector logs the toast, the
CRITICAL lines and "Scan start refused"); the modals are confirmed by
their Buttons, which Qt exposes over UIA. Not log-verifiable: the E-306
path withdraws the low-storage toast through a direct dismiss-by-tag
signal that bypasses the logging slot, so that one is operator-visual.

Safety: the drive sits at ~80 MB free only until the app's 5 s storage
poll stops the scan; the second filler is deleted the moment E-306 is
logged and the first at class teardown, failure or not.

Preconditions
- Console + at least one sensor connected; app running (any variant —
  on a Clinical build Start runs the contact-quality gate first and the
  test clicks "Start Scan" in that modal).
- The data drive is the drive the running app logs in
  "Startup storage check: N MB free on <path>"; this test fills that
  drive's root.

Marked ``release``: runs a real (short) scan.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

import pyautogui
import pytest

from conftest import SLEEP, click_by_name, log, require_focus, uia_window
from hil_helpers import (
    click_element_center,
    click_panel,
    find_app_log,
    is_app_alive,
    log_size,
    wait_for_pattern,
)

pytestmark = pytest.mark.release

MB = 1024 * 1024
WARN_TARGET_MB = 900      # < minFreeDiskMb (1024)
STOP_TARGET_MB = 80       # < scanStopFreeDiskMb (100)
SETTLE_AFTER_START_S = 45
RUNNING_HOLD_S = 30

RE_SCAN_STARTED = re.compile(r"Full scan started")
RE_SCAN_ENDED = re.compile(r"Full scan ended")
RE_SCAN_STOPPED = re.compile(r"Full scan ended: stopped")
RE_LOW_STORAGE_TOAST = re.compile(r"Toast #\d+ \[warning\] tag='low_storage'")
RE_ALMOST_FULL = re.compile(r"Data drive almost full")
RE_E306 = re.compile(r"CRITICAL E-306")
RE_E305 = re.compile(r"CRITICAL E-305")
RE_START_REFUSED = re.compile(r"Scan start refused")
RE_LEGACY_STOP_TOAST = re.compile(r"Toast #\d+ .*Scan stopped: the data drive")
RE_NOTE_SAVED = re.compile(r"Toast #\d+ \[success\].*Note saved\.")
RE_STARTUP_STORAGE = re.compile(r"Startup storage check: (\d+) MB free on (.+)$")

MODAL_BUTTONS = ("Dismiss", "Contact Support", "Copy details")

STATE: dict = {"fillers": []}


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def _data_root() -> Path:
    """Drive root the app's storage guard checks (from the startup line)."""
    lp = find_app_log()
    if lp:
        for line in Path(lp).read_text(encoding="utf-8", errors="replace").splitlines():
            m = RE_STARTUP_STORAGE.search(line)
            if m:
                return Path(Path(m.group(2).strip()).anchor)
    return Path("C:\\")


def _free_mb() -> int:
    return shutil.disk_usage(str(_data_root())).free // MB


def _log_since(offset: int) -> str:
    """App-log text from a byte ``offset`` (as ``log_size`` reports) to EOF.

    Read as bytes and decode after seeking: the log holds multibyte
    characters (em dashes), so slicing a decoded string by a byte offset
    lands past the lines written at that point."""
    with Path(STATE["log"]).open("rb") as f:
        f.seek(offset)
        return f.read().decode("utf-8", errors="replace")


def _scan_in_progress() -> bool:
    """True when the newest 'Full scan started' has no matching 'ended'."""
    text = Path(STATE["log"]).read_bytes().decode("utf-8", errors="replace")
    last_start = text.rfind("Full scan started")
    return last_start >= 0 and "Full scan ended" not in text[last_start:]


def _filler_path(n: int) -> Path:
    return Path.home() / f"filler{n}.bin"


def _make_filler(n: int, target_mb: int) -> None:
    size = (_free_mb() - target_mb) * MB
    assert size > 0, f"only {_free_mb()} MB free; already below target {target_mb} MB"
    path = _filler_path(n)
    r = subprocess.run(["fsutil", "file", "createnew", str(path), str(size)],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"fsutil createnew failed: {(r.stdout or r.stderr).strip()}"
    STATE["fillers"].append(path)
    log.info(f"  {path.name}: {size // MB} MB reserved; free now {_free_mb()} MB")


def _remove_filler(n: int) -> None:
    path = _filler_path(n)
    if path.exists():
        path.unlink()
        log.info(f"  {path.name} removed; free now {_free_mb()} MB")
    if path in STATE["fillers"]:
        STATE["fillers"].remove(path)


def _remove_all_fillers() -> None:
    for n in (1, 2):
        try:
            _remove_filler(n)
        except OSError as e:
            log.warning(f"  could not remove filler{n}.bin: {e}")


def _modal_buttons() -> set[str]:
    """Critical-error modal buttons visible over UIA (Qt exposes Buttons)."""
    found = set()
    try:
        for b in uia_window().descendants(control_type="Button"):
            n = (b.window_text() or "").strip()
            if n in MODAL_BUTTONS:
                found.add(n)
    except Exception as e:
        log.warning(f"  UIA button scan failed: {e}")
    return found


def _click_button(name: str, timeout: float = 10.0) -> bool:
    """Pixel-click a UIA Button by exact name (QML Buttons ignore Invoke)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for b in uia_window().descendants(control_type="Button"):
                if (b.window_text() or "").strip() == name:
                    click_element_center(b, name)
                    time.sleep(SLEEP)
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _press_start_and_wait_for_scan(log_path: Path, start_off: int, timeout: float = 240.0) -> str:
    """Click Start; on a Clinical build click 'Start Scan' in the contact-
    quality gate when it appears; return the 'Full scan started' line."""
    click_panel("Start")
    deadline = time.time() + timeout
    clicked_gate = False
    while time.time() < deadline:
        line = wait_for_pattern(RE_SCAN_STARTED, log_path, start_off, 2)
        if line:
            return line
        if not clicked_gate and _click_button("Start Scan", timeout=0.5):
            log.info("  contact-quality gate: clicked 'Start Scan'")
            clicked_gate = True
    pytest.fail(f"no 'Full scan started' line within {timeout:.0f} s of clicking Start")


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────
@pytest.fixture(scope="class", autouse=True)
def _fillers_always_removed(app):
    """Whatever happens, leave the data drive the way we found it."""
    _remove_all_fillers()
    free_before = _free_mb()
    log.info(f"  data drive {_data_root()}: {free_before} MB free before the test")
    try:
        yield
    finally:
        _remove_all_fillers()
        log.info(f"  data drive {_data_root()}: {_free_mb()} MB free after cleanup")


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────
@pytest.mark.incremental
class TestStorageGuard:
    """Low-storage warning, graceful stop with E-306, and Start refusal with E-305."""

    def test_01_scan_starts(self, app):
        """A scan starts (Start, plus 'Start Scan' through the clinical contact-quality gate)."""
        log_path = find_app_log()
        assert log_path, "app log not found"
        STATE["log"] = log_path
        if _scan_in_progress():
            # Start doubles as Stop while a scan runs — never press it blind.
            log.info("  a scan is already running — waiting for it to end before starting ours")
            deadline = time.time() + 660
            while time.time() < deadline and _scan_in_progress():
                time.sleep(5)
            assert not _scan_in_progress(), "a scan was still running after 11 min; stop it and rerun"
            require_focus()
            pyautogui.press("escape")   # the post-scan Session Notes modal
            time.sleep(SLEEP)
        off = log_size(log_path)
        line = _press_start_and_wait_for_scan(log_path, off)
        log.info(f"  {line.strip()[:120]}")
        STATE["scan_off"] = log_size(log_path)
        log.info(f"  settling {SETTLE_AFTER_START_S} s into the scan")
        time.sleep(SETTLE_AFTER_START_S)
        assert wait_for_pattern(RE_SCAN_ENDED, log_path, STATE["scan_off"], 1) is None, \
            "the scan ended before the storage checks could run"

    def test_02_warning_toast_under_1gb(self, app):
        """Free space taken to ~900 MB mid-scan: the 'Storage is running low' toast is raised once."""
        off = log_size(STATE["log"])
        STATE["warn_off"] = off
        _make_filler(1, WARN_TARGET_MB)
        toast = wait_for_pattern(RE_LOW_STORAGE_TOAST, STATE["log"], off, 40)
        assert toast, "no low_storage warning toast logged within 40 s of dropping under 1 GB"
        log.info(f"  {toast.strip()[11:150]}")

    def test_03_scan_keeps_running(self, app):
        """The scan is still running 30 s after the warning, and the warning fired exactly once."""
        time.sleep(RUNNING_HOLD_S)
        text = _log_since(STATE["warn_off"])
        assert not RE_SCAN_ENDED.search(text), "the scan ended after the 1 GB warning — it must keep running"
        n = len(RE_LOW_STORAGE_TOAST.findall(text))
        assert n == 1, f"expected exactly one low-storage warning, saw {n}"

    def test_04_graceful_stop_with_e306_under_100mb(self, app):
        """Free space taken to ~80 MB: E-306 is raised, the scan stops gracefully, the modal is up, no legacy toast."""
        off = log_size(STATE["log"])
        _make_filler(2, STOP_TARGET_MB)
        try:
            e306 = wait_for_pattern(RE_E306, STATE["log"], off, 40)
        finally:
            _remove_filler(2)      # protect the system drive immediately
        assert e306, "no 'CRITICAL E-306' within 40 s of dropping under 100 MB"
        log.info(f"  {e306.strip()[11:170]}")
        stop = wait_for_pattern(RE_SCAN_STOPPED, STATE["log"], off, 30)
        assert stop, "scan did not log 'Full scan ended: stopped' after E-306"
        text = _log_since(off)
        assert RE_ALMOST_FULL.search(text), "'Data drive almost full' line missing"
        assert not RE_LEGACY_STOP_TOAST.search(text), "legacy 'Scan stopped' toast raised — the modal should replace it"
        time.sleep(2)
        btns = _modal_buttons()
        log.info(f"  modal buttons over UIA: {sorted(btns)}")
        assert "Dismiss" in btns, f"E-306 modal not visible (buttons seen: {sorted(btns)})"
        log.info("  NOTE: the low-storage toast withdrawal is signal-driven and not logged — confirm visually")

    def test_05_dismiss_opens_session_notes(self, app):
        """Dismiss closes the modal and the Session Notes modal opens (closed here with Escape -> 'Note saved.')."""
        assert _click_button("Dismiss"), "could not click the E-306 modal's Dismiss button"
        time.sleep(SLEEP)
        off = log_size(STATE["log"])
        require_focus()
        pyautogui.press("escape")
        saved = wait_for_pattern(RE_NOTE_SAVED, STATE["log"], off, 10)
        assert saved, "no 'Note saved.' after Escape — the Session Notes modal did not open after Dismiss"
        log.info("  Session Notes opened after Dismiss and closed with Escape")

    def test_06_start_refused_with_e305(self, app):
        """With the drive still under 1 GB, Start is refused with E-305."""
        assert _free_mb() < 1024, f"drive is at {_free_mb()} MB — not under 1 GB any more"
        off = log_size(STATE["log"])
        click_panel("Start")
        refused = wait_for_pattern(RE_START_REFUSED, STATE["log"], off, 20)
        e305 = wait_for_pattern(RE_E305, STATE["log"], off, 5)
        assert refused and e305, "Start under 1 GB was not refused with E-305"
        log.info(f"  {e305.strip()[11:150]}")
        _click_button("Dismiss", timeout=5)

    def test_07_free_space_restored(self, app):
        """Fillers removed; free space back above 1 GB; app still alive."""
        _remove_all_fillers()
        assert _free_mb() > 1024, f"drive still at {_free_mb()} MB after removing fillers"
        assert not list(Path.home().glob("filler*.bin")), "filler files left behind"
        assert is_app_alive(), "app died during the storage checks"
