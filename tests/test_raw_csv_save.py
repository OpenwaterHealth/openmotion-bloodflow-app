"""
test_raw_csv_save.py — verify the developer-mode raw-CSV truncation.

Issue: OpenwaterHealth/openmotion-bloodflow-app#105

What this exercises
-------------------
When the user enables ``writeRawCsv`` in the Settings → Developer
section and sets ``Raw CSV duration`` to N seconds, the SDK's
ScanWorkflow writes per-side raw histogram CSVs (``*_left_mask*_raw.csv`` /
``*_right_mask*_raw.csv``) for the first N seconds of the scan, then
closes them — while the canonical scan CSV (``<ts>_<subject>.csv``)
keeps streaming for the full configured scan duration.

Lifecycle
---------
  1. Snapshot the on-disk values of every config key the test
     mutates.
  2. Kill the bloodflow app.
  3. Force on disk: ``developerMode=true`` (so the Save raw CSV
     toggle and duration field are visible), ``reducedMode=false``
     (so the Scan Settings panel button is visible).
  4. Launch the app, calibrate panel buttons, wait for CONNECTED.
  5. Open Settings, scroll to the Developer section, toggle
     ``Save raw CSV`` ON via the UI, type ``120`` into
     ``Raw CSV duration``, escape out.
  6. Open Scan Settings, set duration to 3 minutes, escape out.
  7. Click Start, wait for the auto-opened Session Notes modal
     (signals scan completion).
  8. Diff the data directory before/after the scan to find the
     three new CSVs (canonical + left/right raw). Assert:
       - canonical CSV's ``timestamp_s`` column spans ~180 s ± tol
       - both ``*_raw.csv`` files span ~120 s ± tol
  9. Always (in ``finally``): restore the original config values
     and relaunch so the bench is clean for whatever runs next.

Marked ``release``: ~7 min wall-clock total (3 min scan + camera
config + setup/teardown). Don't run in the dev-tier HIL set.
"""

from __future__ import annotations

import csv
import glob
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import psutil
import pytest

from conftest import (
    PROJECT_ROOT,
    SLEEP,
    ensure_visible,
    get_app_window,
    log,
    uia_window,
)
from hil_helpers import (
    RE_CONNECTED,
    click_element_center,
    click_panel,
    find_app_log,
    read_app_config_value,
    recalibrate_panel_buttons,
    wait_for_pattern,
    write_app_config_value,
)

import pyautogui

pytestmark = pytest.mark.release


# Wall-clock budgets
APP_CONNECT_TIMEOUT = 60
SESSION_NOTES_TIMEOUT = 360  # camera config (~75s) + 3-min scan + buffer

# Test parameters (also baked into the assertions below)
SCAN_DURATION_SEC      = 180   # 3 min, set in Scan Settings
RAW_CSV_DURATION_SEC   = 120   # 2 min, typed into Settings → Developer
DURATION_TOLERANCE_SEC = 12    # how far off the timestamps can drift

# Column name in both raw and canonical CSVs.
_TIMESTAMP_COL = "timestamp_s"

# Per-side raw CSV pattern. Subject can carry hyphens / digits;
# anything not matching the side+mask+_raw tail is rejected here.
_RAW_CSV_RE = re.compile(
    r"^(?P<ts>\d{8}_\d{6})_(?P<subject>.+?)_(?P<side>left|right)_mask[0-9A-Fa-f]+_raw\.csv$"
)
# Telemetry CSV pattern — distinguished only so we can exclude it
# from the canonical bucket below.
_TELEMETRY_CSV_RE = re.compile(
    r"^(?P<ts>\d{8}_\d{6})_(?P<subject>.+?)_telemetry\.csv$"
)
# Canonical scan CSV pattern: every other ``<ts>_*.csv`` in the
# data directory. We pick the non-raw / non-telemetry one rather
# than constraining the subject string, so user-labels containing
# digits, hyphens, or even (in principle) underscores still match.
_TS_CSV_RE = re.compile(r"^(?P<ts>\d{8}_\d{6})_.+\.csv$")


# ─────────────────────────────────────────────
# App lifecycle helpers — same shape as test_force_laser_fail's,
# kept inline because conftest's session-scoped ``app`` fixture
# isn't designed to be re-run mid-session.
# ─────────────────────────────────────────────
def _from_source_mode() -> bool:
    return os.environ.get("OPENWATER_FROM_SOURCE", "").lower() in (
        "1", "true", "yes",
    )


def _find_exe() -> Optional[str]:
    env = os.environ.get("OPENWATER_EXE", "")
    if env and os.path.exists(env):
        return env
    patterns = [
        r"C:\Users\*\Documents\OpenMotion\**\OpenWaterApp.exe",
        r"C:\Users\*\Desktop\**\OpenWaterApp.exe",
        r"C:\Program Files\**\OpenWaterApp.exe",
        r"C:\Program Files (x86)\**\OpenWaterApp.exe",
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
            pytest.fail("OpenWaterApp.exe not found and OPENWATER_EXE unset")
        log.info(f"  launching: {exe}")
        subprocess.Popen([exe])


def _wait_for_app_window(timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ensure_visible():
            return True
        time.sleep(1)
    return False


def _wait_for_connect(timeout: int = APP_CONNECT_TIMEOUT) -> Optional[str]:
    log_path = find_app_log()
    if log_path is None:
        return None
    return wait_for_pattern(RE_CONNECTED, log_path, 0, timeout)


def _restart_app(label: str) -> None:
    log.info(f"  [{label}] (re)launching app")
    _kill_bloodflow_processes()
    time.sleep(2)
    _launch_app()
    assert _wait_for_app_window(timeout=30), (
        f"[{label}] app window did not appear within 30 s of launch"
    )
    recalibrate_panel_buttons()
    connect_line = _wait_for_connect(timeout=APP_CONNECT_TIMEOUT)
    assert connect_line, (
        f"[{label}] app did not reach CONNECTED within "
        f"{APP_CONNECT_TIMEOUT} s of relaunch"
    )
    log.info(f"  [{label}] connected: {connect_line.strip()}")
    time.sleep(SLEEP)


# ─────────────────────────────────────────────
# Settings modal manipulation
# ─────────────────────────────────────────────
def _scroll_modal_to_bottom() -> None:
    """Scroll the active modal's content all the way down. The
    Developer section sits at the bottom of Settings, so we need to
    bring its labels into view before walking UIA to find them."""
    ensure_visible()
    w = get_app_window()
    cx = w.left + w.width // 2
    cy = w.top + w.height // 2
    pyautogui.moveTo(cx, cy, duration=0.2)
    for _ in range(5):
        pyautogui.scroll(-50)
        time.sleep(0.3)
    time.sleep(0.5)
    log.info("  Settings modal scrolled to bottom")


def _find_label_rect(needle: str):
    """Return the (left, top, right, bottom) rect for the first UIA
    descendant whose text matches ``needle`` case-insensitively, or
    ``None`` if no match is found. Walks the unfiltered descendants
    list because Qt's accessibility bridge surfaces FieldRow Text
    labels through that path even when ``descendants(title=...)``
    returns empty."""
    needle_lc = needle.lower()
    try:
        win = uia_window()
        for elem in win.descendants():
            try:
                txt = (elem.window_text() or "").strip().lower()
            except Exception:
                continue
            if needle_lc in txt:
                rect = elem.rectangle()
                return (rect.left, rect.top, rect.right, rect.bottom), elem
    except Exception as e:
        log.warning(f"  _find_label_rect('{needle}') failed: {e}")
    return None


# Window-relative X for the Settings modal's first FieldRow control
# slot. Empirically derived in test_reducedmode._toggle_auto_scale_on
# and known to land on every FieldRow's PillSwitch / TextField. Used
# here for the same reason — the UIA rectangle of a label Text returns
# the painted-text bounds (not the layout slot), so right-edge + offset
# lands in the gap before the control rather than on it.
_SETTINGS_CONTROL_X_FRACTION = 0.43


def _click_field_control(label_needle: str, action_label: str) -> int:
    """Locate ``label_needle`` in the Settings modal's UIA tree, then
    click at ``_SETTINGS_CONTROL_X_FRACTION`` × window.width on the
    label's Y coordinate. Returns the click Y (so callers can do a
    follow-up Y-aligned action without re-querying).
    """
    found = _find_label_rect(label_needle)
    if found is None:
        pytest.fail(
            f"Could not locate '{label_needle}' label in Settings. "
            f"The Developer section may be hidden (developerMode flag "
            f"not applied?) or the label text was renamed."
        )
    (_, top, _, bottom), _ = found
    label_cy = (top + bottom) // 2
    w = get_app_window()
    click_x = int(w.left + _SETTINGS_CONTROL_X_FRACTION * w.width)
    log.info(f"  {action_label} at ({click_x}, {label_cy})")
    pyautogui.click(click_x, label_cy)
    time.sleep(SLEEP)
    return label_cy


def _toggle_raw_csv_save_on() -> None:
    """Find the 'Save raw CSV' label and click the PillSwitch beside it."""
    _click_field_control("Save raw CSV", "clicking Save-raw-CSV PillSwitch")


def _set_raw_csv_duration_sec(seconds: int) -> None:
    """Click into the 'Raw CSV duration' TextField and type ``seconds``.
    Tab afterwards so ``onEditingFinished`` fires and the value is
    committed through to ``setRawCsvDurationSec`` (which writes to
    disk)."""
    _click_field_control(
        "Raw CSV duration",
        f"clicking Raw-CSV-duration TextField and typing '{seconds}'",
    )
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.typewrite(str(seconds), interval=0.05)
    time.sleep(0.2)
    pyautogui.press("tab")  # commit via focus-loss → onEditingFinished
    time.sleep(SLEEP)


# ─────────────────────────────────────────────
# Scan Settings modal manipulation
# ─────────────────────────────────────────────
def _set_scan_duration(hours: int, minutes: int, seconds: int) -> None:
    """Walk UIA descendants for small numeric Edit fields and write
    H/M/S into the first three. Mirrors test_history's
    ``_set_scan_duration``."""
    require_focus_via_ensure_visible()
    win = uia_window()
    log.info(
        f"  scan duration → {hours:02d}:{minutes:02d}:{seconds:02d}"
    )
    for control_type in ("Edit", "SpinBox", "Custom"):
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
            for elem, value, lbl in (
                (small[0], hours, "Hours"),
                (small[1], minutes, "Minutes"),
                (small[2], seconds, "Seconds"),
            ):
                click_element_center(elem, f"{lbl} field")
                time.sleep(0.1)
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.1)
                pyautogui.typewrite(str(value), interval=0.05)
                time.sleep(0.1)
            return
    pytest.fail(
        "Could not locate three duration spinboxes in Scan Settings — "
        "UIA tree may have changed."
    )


def require_focus_via_ensure_visible() -> None:
    """Local wrapper — we don't import conftest.require_focus to avoid
    a hard fail on transient activation issues mid-test; ensure_visible
    is enough to drive subsequent pyautogui calls."""
    if not ensure_visible():
        pytest.fail("App window not visible — cannot type into duration fields")


def _wait_for_session_notes_modal(timeout: int) -> bool:
    """Poll UIA for a 'Session Notes' string. The modal auto-opens
    when the SDK fires the scan-complete callback."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            win = uia_window()
            for elem in win.descendants():
                try:
                    txt = (elem.window_text() or "").strip().lower()
                except Exception:
                    continue
                if "session notes" in txt:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


# ─────────────────────────────────────────────
# CSV inspection
# ─────────────────────────────────────────────
def _csv_timestamp_span_seconds(path: Path) -> float:
    """Return ``max(timestamp_s) - min(timestamp_s)`` over every row
    in ``path``. Both raw and canonical scan CSVs carry a
    ``timestamp_s`` column; raw CSVs have one row per (cam, frame),
    canonical CSVs have one row per frame."""
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        try:
            ts_idx = header.index(_TIMESTAMP_COL)
        except ValueError:
            pytest.fail(
                f"{path.name}: no '{_TIMESTAMP_COL}' column in header "
                f"({header[:6]}…)"
            )
        first_ts: Optional[float] = None
        last_ts: Optional[float] = None
        for row in reader:
            if ts_idx >= len(row):
                continue
            try:
                ts = float(row[ts_idx])
            except (TypeError, ValueError):
                continue
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
    if first_ts is None or last_ts is None:
        pytest.fail(f"{path.name}: no usable timestamp_s rows")
    return float(last_ts - first_ts)


def _new_csvs_in(dir_path: Path, before: set[str]) -> dict[str, Path]:
    """Diff the directory listing and bucket new files by role.

    Returns ``{'canonical': Path, 'left_raw': Path, 'right_raw': Path}``.
    Raises pytest.fail if any of the three roles is missing."""
    after = {p.name for p in dir_path.iterdir() if p.is_file()}
    new = sorted(after - before)
    log.info(f"  new files in {dir_path}: {new}")

    canonical: Optional[Path] = None
    left_raw: Optional[Path] = None
    right_raw: Optional[Path] = None
    for name in new:
        m_raw = _RAW_CSV_RE.match(name)
        if m_raw:
            if m_raw.group("side") == "left":
                left_raw = dir_path / name
            else:
                right_raw = dir_path / name
            continue
        if _TELEMETRY_CSV_RE.match(name):
            continue  # telemetry not under test
        if _TS_CSV_RE.match(name):
            # Anything else matching <ts>_*.csv that's not raw or
            # telemetry is the canonical scan output.
            canonical = dir_path / name

    missing = []
    if canonical is None:
        missing.append("canonical <ts>_<subject>.csv")
    if left_raw is None:
        missing.append("left _raw.csv")
    if right_raw is None:
        missing.append("right _raw.csv")
    if missing:
        pytest.fail(
            f"Expected three new scan CSVs in {dir_path} but missing "
            f"{missing}. Saw: {new}"
        )
    return {
        "canonical": canonical,
        "left_raw":  left_raw,
        "right_raw": right_raw,
    }


# ─────────────────────────────────────────────
# Test
# ─────────────────────────────────────────────
class TestRawCsvSave:
    """End-to-end verification of the developer-mode raw-CSV truncation."""

    def test_raw_csv_truncates_at_configured_duration(self, app):
        """Walk through the lifecycle in the module docstring and
        assert the canonical scan CSV runs the full configured scan
        duration while the per-side raw CSVs stop at the
        ``rawCsvDurationSec`` mark.
        """
        # ─── Step 0: snapshot config we will mutate ──────────────
        original = {
            "developerMode":     bool(read_app_config_value("developerMode", False)),
            "reducedMode":       bool(read_app_config_value("reducedMode", False)),
            "writeRawCsv":       bool(read_app_config_value("writeRawCsv", False)),
            "rawCsvDurationSec": read_app_config_value("rawCsvDurationSec", None),
            "dataDirectory":     read_app_config_value("dataDirectory", None),
        }
        log.info(f"original config snapshot: {original}")

        # Pin a known data directory so the diff-after-scan step
        # doesn't have to chase a default that depends on cwd.
        test_data_dir = PROJECT_ROOT / "scan_data"

        try:
            # ─── Step 1+2+3: kill app, write pre-launch flags ───
            log.info("=" * 60)
            log.info(
                "Step 1+2+3: killing app, writing developerMode=true, "
                "reducedMode=false, writeRawCsv=false (will toggle ON "
                "via UI), pinning dataDirectory"
            )
            log.info("=" * 60)
            _kill_bloodflow_processes()
            time.sleep(2)
            test_data_dir.mkdir(parents=True, exist_ok=True)
            write_app_config_value("developerMode",     True)
            write_app_config_value("reducedMode",       False)
            # Start with writeRawCsv off so we can prove the UI toggle
            # actually changed it (rather than walking past a no-op).
            write_app_config_value("writeRawCsv",       False)
            write_app_config_value("rawCsvDurationSec", None)
            write_app_config_value("dataDirectory",     str(test_data_dir))

            # ─── Step 4: launch + calibrate ─────────────────────────
            log.info("=" * 60)
            log.info("Step 4: launching app, calibrating panel buttons")
            log.info("=" * 60)
            _restart_app("step 4")

            # Snapshot the data dir BEFORE the scan so we can diff
            # afterwards. Files written before this set don't count.
            files_before = {p.name for p in test_data_dir.iterdir() if p.is_file()}

            # ─── Step 5: open Settings, toggle raw CSV, set duration ─
            log.info("=" * 60)
            log.info(
                "Step 5: opening Settings, toggling Save raw CSV ON, "
                f"setting Raw CSV duration to {RAW_CSV_DURATION_SEC} s"
            )
            log.info("=" * 60)
            click_panel("Settings")
            time.sleep(SLEEP)
            _scroll_modal_to_bottom()  # Developer section is at the bottom
            _toggle_raw_csv_save_on()
            _set_raw_csv_duration_sec(RAW_CSV_DURATION_SEC)
            require_focus_via_ensure_visible()
            pyautogui.press("escape")  # close Settings → commits writes
            time.sleep(SLEEP)

            # Sanity-check what the connector actually persisted. The
            # PillSwitch and TextField call setWriteRawCsv /
            # setRawCsvDurationSec which write through to disk.
            wrote = read_app_config_value("writeRawCsv", False)
            dur   = read_app_config_value("rawCsvDurationSec", None)
            assert wrote is True, (
                f"Settings UI didn't flip writeRawCsv to True "
                f"(got {wrote!r}). Toggle click likely missed the "
                f"PillSwitch — check coordinate offsets in "
                f"_toggle_raw_csv_save_on."
            )
            assert dur is not None and abs(float(dur) - RAW_CSV_DURATION_SEC) < 1, (
                f"Settings UI didn't write rawCsvDurationSec="
                f"{RAW_CSV_DURATION_SEC} (got {dur!r}). The TextField "
                f"likely didn't accept the typed input — check "
                f"_set_raw_csv_duration_sec."
            )
            log.info(
                f"  config sanity: writeRawCsv={wrote}, "
                f"rawCsvDurationSec={dur}"
            )

            # ─── Step 6: open Scan Settings, set duration ────────────
            log.info("=" * 60)
            log.info(
                f"Step 6: opening Scan Settings, setting duration to "
                f"{SCAN_DURATION_SEC} s"
            )
            log.info("=" * 60)
            click_panel("Scan\nSettings")
            time.sleep(SLEEP)
            _set_scan_duration(0, SCAN_DURATION_SEC // 60, SCAN_DURATION_SEC % 60)
            require_focus_via_ensure_visible()
            pyautogui.press("escape")  # close Scan Settings → commits
            time.sleep(SLEEP)

            # ─── Step 7: click Start, wait for scan completion ──────
            log.info("=" * 60)
            log.info(
                f"Step 7: clicking Start, waiting up to "
                f"{SESSION_NOTES_TIMEOUT} s for the Session Notes modal"
            )
            log.info("=" * 60)
            click_panel("Start")
            assert _wait_for_session_notes_modal(SESSION_NOTES_TIMEOUT), (
                f"Session Notes modal did not appear within "
                f"{SESSION_NOTES_TIMEOUT} s — the scan never completed. "
                f"Possible camera-config stall or the trigger never "
                f"fired."
            )
            require_focus_via_ensure_visible()
            pyautogui.press("escape")  # dismiss Session Notes
            time.sleep(SLEEP)

            # ─── Step 8: diff data dir, verify durations ─────────────
            log.info("=" * 60)
            log.info("Step 8: verifying CSV durations against assertions")
            log.info("=" * 60)
            scan_files = _new_csvs_in(test_data_dir, files_before)
            log.info(
                f"  canonical={scan_files['canonical'].name}\n"
                f"  left_raw ={scan_files['left_raw'].name}\n"
                f"  right_raw={scan_files['right_raw'].name}"
            )

            canonical_span = _csv_timestamp_span_seconds(scan_files["canonical"])
            left_span      = _csv_timestamp_span_seconds(scan_files["left_raw"])
            right_span     = _csv_timestamp_span_seconds(scan_files["right_raw"])
            log.info(
                f"  canonical span: {canonical_span:.2f} s "
                f"(expected ~{SCAN_DURATION_SEC})"
            )
            log.info(
                f"  left raw span : {left_span:.2f} s "
                f"(expected ~{RAW_CSV_DURATION_SEC})"
            )
            log.info(
                f"  right raw span: {right_span:.2f} s "
                f"(expected ~{RAW_CSV_DURATION_SEC})"
            )

            assert abs(canonical_span - SCAN_DURATION_SEC) <= DURATION_TOLERANCE_SEC, (
                f"Canonical scan CSV span {canonical_span:.2f} s differs "
                f"from configured {SCAN_DURATION_SEC} s by more than "
                f"{DURATION_TOLERANCE_SEC} s tolerance. The full scan "
                f"should run for the duration set in Scan Settings, "
                f"independent of writeRawCsv truncation."
            )
            assert abs(left_span - RAW_CSV_DURATION_SEC) <= DURATION_TOLERANCE_SEC, (
                f"Left raw CSV span {left_span:.2f} s differs from "
                f"configured raw-CSV duration {RAW_CSV_DURATION_SEC} s "
                f"by more than {DURATION_TOLERANCE_SEC} s tolerance. The "
                f"raw histogram saver should halt at this mark; the "
                f"main scan should keep going."
            )
            assert abs(right_span - RAW_CSV_DURATION_SEC) <= DURATION_TOLERANCE_SEC, (
                f"Right raw CSV span {right_span:.2f} s differs from "
                f"configured raw-CSV duration {RAW_CSV_DURATION_SEC} s "
                f"by more than {DURATION_TOLERANCE_SEC} s tolerance."
            )
            # Also assert the raw CSVs are SHORTER than the canonical —
            # protects against a build where the truncation didn't fire
            # at all and all three CSVs ran to full scan duration but
            # within the absolute tolerance happened to look OK.
            assert left_span  < canonical_span - 30, (
                f"Left raw CSV span ({left_span:.1f} s) should be at "
                f"least 30 s shorter than canonical ({canonical_span:.1f} s); "
                f"raw truncation likely didn't fire."
            )
            assert right_span < canonical_span - 30, (
                f"Right raw CSV span ({right_span:.1f} s) should be at "
                f"least 30 s shorter than canonical ({canonical_span:.1f} s); "
                f"raw truncation likely didn't fire."
            )
            log.info("  PASS: all three CSVs span their expected durations")

        finally:
            # ─── Step 9 (cleanup): restore original config ───────────
            log.info("=" * 60)
            log.info(
                f"Step 9 (cleanup): restoring original config: {original}"
            )
            log.info("=" * 60)
            _kill_bloodflow_processes()
            time.sleep(2)
            for key, value in original.items():
                write_app_config_value(key, value)
            _launch_app()
            if not _wait_for_app_window(timeout=30):
                log.error(
                    "Cleanup failure: app window did not reappear after "
                    "config restore. Subsequent tests will likely fail "
                    "until the app is relaunched manually."
                )
                return
            recalibrate_panel_buttons()
            log.info("  cleanup complete; original config restored")
