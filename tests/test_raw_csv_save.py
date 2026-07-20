"""
test_raw_csv_save.py — verify the research-mode raw-CSV truncation.

Issues: OpenwaterHealth/openmotion-bloodflow-app#105 (truncation),
#234 (raw CSVs are a research-mode feature, no engineering unlock
needed).

What this exercises
-------------------
When the user enables ``writeRawCsv`` in the Settings → Data Output
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
  3. Force on disk: ``engineeringMode=false`` + ``clinicalMode=false``
     — research mode, where the Save raw CSV toggle and duration
     field must be visible without the engineering unlock (#234),
     and the Scan Settings panel button is visible.
  4. Launch the app, calibrate panel buttons, wait for CONNECTED.
  5. Open Settings, scroll to the Data Output section, toggle
     ``Save raw CSV`` ON via the UI, type ``30`` into
     ``Raw CSV duration``, escape out.
  6. Open Scan Settings, set duration to 1 minute, escape out.
  7. Click Start, wait for the auto-opened Session Notes modal
     (signals scan completion).
  8. Diff the data directory before/after the scan to find the
     three new CSVs (canonical + left/right raw). Assert:
       - canonical CSV's ``timestamp_s`` column spans ~60 s ± tol
       - both ``*_raw.csv`` files span ~30 s ± tol
       - both ``*_raw.csv`` files pass the SDK
         ``check_csv_integrity`` payload check (per-row sum,
         frame_id sequencing, equal cam count per frame)
  9. Always (in ``finally``): restore the original config values
     and relaunch so the bench is clean for whatever runs next.

Marked ``release``: ~3-4 min wall-clock total (1 min scan + camera
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
    _resolve_app_config_path,
    click_element_center,
    click_panel,
    find_app_log,
    read_app_config_value,
    read_local_config_value,
    recalibrate_panel_buttons,
    resolve_local_config_path,
    validate_raw_csv_payload,
    wait_for_pattern,
    write_app_config_value,
)

import pyautogui

pytestmark = pytest.mark.release


# Wall-clock budgets
APP_CONNECT_TIMEOUT = 60
SESSION_NOTES_TIMEOUT = 200  # camera config (~75s) + 1-min scan + buffer

# Test parameters (also baked into the assertions below)
SCAN_DURATION_SEC      = 60    # 1 min, set in Scan Settings
RAW_CSV_DURATION_SEC   = 30    # 30 s, typed into Settings → Data Output
DURATION_TOLERANCE_SEC = 8     # how far off the timestamps can drift

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
def _dump_uia_texts(max_items: int = 80) -> list[str]:
    """Return up to ``max_items`` non-empty window_text() strings from
    every descendant of the app window. Used by failure paths to make
    it obvious whether a label is missing entirely vs. surfacing under
    an unexpected form (whitespace, accidental empty SectionCard, etc).
    """
    seen: list[str] = []
    try:
        win = uia_window()
        for elem in win.descendants():
            try:
                t = (elem.window_text() or "").strip()
            except Exception:
                continue
            if t and len(t) <= 80:
                seen.append(t)
                if len(seen) >= max_items:
                    break
    except Exception as e:
        log.warning(f"  _dump_uia_texts failed: {e}")
    return seen


def _scroll_until_label_visible(
    needle: str,
    max_passes: int = 12,
    scroll_per_pass: int = 200,
) -> bool:
    """Scroll the modal under the cursor down until ``needle`` (case-
    insensitive) shows up in the UIA descendants list, OR we've made
    ``max_passes`` × ``scroll_per_pass`` worth of wheel ticks without
    finding it.

    On failure, dumps the UIA texts that ARE visible so the operator
    can see whether the label is genuinely missing (Qt bridge dropped
    it) vs. surfacing under a slightly different form.
    """
    ensure_visible()
    w = get_app_window()
    cx = w.left + w.width // 2
    cy = w.top + w.height // 2
    pyautogui.moveTo(cx, cy, duration=0.2)
    for i in range(max_passes):
        if _find_label_rect(needle) is not None:
            log.info(f"  '{needle}' visible after {i} scroll pass(es)")
            return True
        pyautogui.scroll(-scroll_per_pass)
        time.sleep(0.4)
    # One last check after the final scroll
    if _find_label_rect(needle) is not None:
        log.info(f"  '{needle}' visible after {max_passes} scroll pass(es)")
        return True

    visible = _dump_uia_texts()
    log.warning(
        f"  '{needle}' still not visible after {max_passes} × "
        f"scroll({scroll_per_pass}) ticks. UIA texts seen: {visible}"
    )
    return False


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


# Vertical tolerance (pixels) when matching a control's center Y to
# its label's center Y. FieldRow children are vertically centered, so
# the label and its control row should agree to within ~12 px even
# at small label widths.
_ROW_Y_TOLERANCE_PX = 18


def _find_control_aligned_with_label(
    label_needle: str,
    control_types: tuple[str, ...],
) -> Optional[object]:
    """Locate ``label_needle`` in the UIA tree, then return the
    nearest UIA element whose ``control_type`` is in ``control_types``
    AND whose center Y is within ``_ROW_Y_TOLERANCE_PX`` of the
    label's center Y AND whose center X is to the RIGHT of the label
    (FieldRow lays out left-to-right). Among candidates the leftmost
    wins — that's the first non-label element on the row, which is
    what FieldRow puts immediately after the label slot.

    Returns the element itself or ``None`` if no aligned candidate.
    """
    label = _find_label_rect(label_needle)
    if label is None:
        return None
    (label_left, label_top, label_right, label_bottom), _ = label
    label_cx = (label_left + label_right) // 2
    label_cy = (label_top + label_bottom) // 2

    best = None
    best_x = None
    try:
        win = uia_window()
        for ct in control_types:
            try:
                candidates = win.descendants(control_type=ct)
            except Exception:
                continue
            for elem in candidates:
                try:
                    rect = elem.rectangle()
                except Exception:
                    continue
                cy = (rect.top + rect.bottom) // 2
                cx = (rect.left + rect.right) // 2
                # Same row as the label?
                if abs(cy - label_cy) > _ROW_Y_TOLERANCE_PX:
                    continue
                # Right of the label, with a small buffer past the
                # label's right edge so we don't grab the label
                # itself if UIA happens to expose it as the requested
                # control_type.
                if cx <= label_right + 4:
                    continue
                if best is None or cx < best_x:
                    best = elem
                    best_x = cx
    except Exception as e:
        log.warning(
            f"  _find_control_aligned_with_label('{label_needle}', "
            f"{control_types}) UIA walk failed: {e}"
        )
    return best


def _find_named_control(name: str, control_types: tuple[str, ...]):
    """Return the first UIA descendant whose title matches ``name``.

    The Save raw CSV PillSwitch and Raw CSV duration TextField carry
    explicit ``Accessible.name`` values (issue #234 moved them into the
    Data Output SectionCard), so — unlike plain FieldRow Text labels,
    which Qt's a11y bridge drops — they surface in the UIA tree and can
    be targeted directly. Tries a title+control_type query per candidate
    type first, then falls back to a plain title walk in case the Qt
    bridge maps the control to an unexpected UIA control type.
    """
    win = uia_window()
    for ct in control_types:
        try:
            matches = win.descendants(title=name, control_type=ct)
        except Exception:
            continue
        if matches:
            return matches[0]
    for elem in win.descendants():
        try:
            if (elem.window_text() or "").strip() == name:
                return elem
        except Exception:
            continue
    return None


def _toggle_raw_csv_save_on() -> None:
    """Flip the Save raw CSV PillSwitch by clicking it directly.

    The switch is found by its ``Accessible.name`` ("Save raw CSV") —
    QML Switch surfaces as a CheckBox through Qt's a11y bridge, with
    Button/Custom fallbacks for bridge quirks.
    """
    elem = _find_named_control(
        "Save raw CSV", ("CheckBox", "Button", "Custom"))
    if elem is None:
        pytest.fail(
            "Could not locate the 'Save raw CSV' switch in the UIA "
            "tree. The Data Output section may be hidden (is the app "
            "in clinical mode? it should be research/engineering here) "
            "or the Accessible.name was removed. UIA texts seen: "
            f"{_dump_uia_texts()}"
        )
    click_element_center(elem, "Save raw CSV switch")
    log.info("  clicked Save raw CSV switch")
    time.sleep(SLEEP)


def _set_raw_csv_duration_sec(seconds: int) -> None:
    """Type ``seconds`` into the Raw CSV duration TextField.

    The field is found by its ``Accessible.name`` ("Raw CSV duration")
    and clicked to focus; typing replaces any existing content
    (Ctrl+A first) and a final Tab commits via focus-loss →
    ``onEditingFinished``.
    """
    elem = _find_named_control("Raw CSV duration", ("Edit",))
    if elem is None:
        pytest.fail(
            "Could not locate the 'Raw CSV duration' TextField in the "
            "UIA tree. The Data Output section may be hidden or the "
            "Accessible.name was removed. UIA texts seen: "
            f"{_dump_uia_texts()}"
        )
    click_element_center(elem, "Raw CSV duration field")
    time.sleep(0.3)
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.typewrite(str(seconds), interval=0.05)
    time.sleep(0.2)
    pyautogui.press("tab")  # commit via focus-loss → onEditingFinished
    log.info(f"  typed '{seconds}' into Raw CSV duration TextField")
    time.sleep(SLEEP)


# ─────────────────────────────────────────────
# Scan Settings modal manipulation
# ─────────────────────────────────────────────
def _scan_settings_modal_open() -> bool:
    """True when the Scan Settings modal is open, detected via its
    exposed control signature.

    Plain QML ``Text`` items never surface through Qt's a11y bridge, so
    text anchors can't work here — the main page exposes NO elements at
    all, and controls only appear in the UIA tree while a modal hosting
    real QC2 controls is open. Scan Settings exposes >=3 Edit fields
    (user label + H/M/S duration) with at most one CheckBox-role
    control; the main Settings modal exposes 4+ CheckBoxes, so the
    CheckBox bound keeps a stuck-open Settings modal (whose numeric
    plot-bound fields look just like duration fields to the
    small-numeric walk in ``_set_scan_duration``) from false-positiving.
    """
    try:
        win = uia_window()
        edits = win.descendants(control_type="Edit")
        checks = win.descendants(control_type="CheckBox")
    except Exception:
        return False
    return len(edits) >= 3 and len(checks) <= 2


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
        f"UIA tree may have changed. UIA texts seen: {_dump_uia_texts()}"
    )


def require_focus_via_ensure_visible() -> None:
    """Local wrapper — we don't import conftest.require_focus to avoid
    a hard fail on transient activation issues mid-test; ensure_visible
    is enough to drive subsequent pyautogui calls."""
    if not ensure_visible():
        pytest.fail("App window not visible — cannot type into duration fields")


# When the scan workflow completes, motion_connector logs:
#   "Saved scan notes to <path>"
# at the same moment the Session Notes modal auto-opens. The modal is
# the visible signal to the operator; the log line is the structured
# signal we trust here, since UIA window walks fail intermittently
# under the bench's display configuration ("UIA window not found"
# warnings during multi-minute waits) and would let scan completion
# slip past detection. Belt-and-suspenders: also poll UIA so a
# non-default app build that doesn't emit that exact log line still
# gets caught.
_SCAN_COMPLETE_RE = re.compile(r"Saved scan notes to ", re.IGNORECASE)


def _wait_for_session_notes_modal(timeout: int) -> bool:
    """Return True once the scan workflow signals completion.

    Two parallel signals; whichever fires first wins:
      - app log carries "Saved scan notes to <path>" — emitted by
        motion_connector at scan completion, simultaneous with the
        Session Notes modal opening.
      - UIA descendants surface a "Session Notes" text — works when
        Qt's a11y bridge is cooperating.

    The log path is captured up-front (before Start was clicked) so we
    only consider lines written during this scan, not a stale entry
    from a prior session.
    """
    log_path = find_app_log()
    log_offset = log_path.stat().st_size if log_path else 0
    if log_path is not None:
        log.info(
            f"  scan-complete watch: tailing {log_path.name} "
            f"from byte {log_offset}"
        )

    deadline = time.time() + timeout
    while time.time() < deadline:
        # Log signal — cheap, deterministic, no UIA round-trips.
        if log_path is not None and log_path.exists():
            try:
                with log_path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(log_offset)
                    chunk = fh.read()
                    log_offset += len(chunk.encode("utf-8", errors="replace"))
                    if _SCAN_COMPLETE_RE.search(chunk):
                        log.info("  scan complete via app-log 'Saved scan notes to'")
                        return True
            except Exception as e:
                log.warning(f"  log tail failed: {e}")
        # UIA signal — secondary, in case the log line ever changes.
        try:
            win = uia_window()
            for elem in win.descendants():
                try:
                    txt = (elem.window_text() or "").strip().lower()
                except Exception:
                    continue
                if "session notes" in txt:
                    log.info("  scan complete via UIA 'Session Notes' text")
                    return True
        except Exception:
            pass
        time.sleep(1)
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
    """End-to-end verification of the research-mode raw-CSV truncation."""

    def test_raw_csv_truncates_at_configured_duration(self, app):
        """Walk through the lifecycle in the module docstring and
        assert the canonical scan CSV runs the full configured scan
        duration while the per-side raw CSVs stop at the
        ``rawCsvDurationSec`` mark.
        """
        # ─── Step 0: snapshot config we will mutate ──────────────
        # Byte-snapshots, not per-key: write_app_config_value re-dumps
        # the shipped config with different formatting, so a value-level
        # restore leaves the tracked file dirty in git. The local
        # overrides file is where the Settings UI persists (#233); a
        # stale one from a previous run or hand-test would override the
        # dataDirectory seeding below AND make the toggle assert pass
        # before the click, so it is parked for the duration of the run.
        cfg_path    = _resolve_app_config_path()
        cfg_bytes   = cfg_path.read_bytes()
        local_path  = resolve_local_config_path()
        local_bytes = local_path.read_bytes() if local_path.exists() else None
        original = {
            "engineeringMode":     bool(read_app_config_value("engineeringMode", False)),
            "clinicalMode":       bool(read_app_config_value("clinicalMode", False)),
            "writeRawCsv":       bool(read_app_config_value("writeRawCsv", False)),
            "rawCsvDurationSec": read_app_config_value("rawCsvDurationSec", None),
            "dataDirectory":     read_app_config_value("dataDirectory", None),
        }
        log.info(f"original config snapshot: {original}")
        log.info(
            f"local overrides at {local_path}: "
            f"{'present (parked for the run)' if local_bytes is not None else 'absent'}"
        )

        # Pin a known data directory so the diff-after-scan step
        # doesn't have to chase a default that depends on cwd. The app
        # writes scan CSVs under <dataDirectory>/data/, not the root itself.
        test_data_dir = PROJECT_ROOT / "scan_data"
        scan_output_dir = test_data_dir / "data"

        try:
            # ─── Step 1+2+3: kill app, write pre-launch flags ───
            log.info("=" * 60)
            log.info(
                "Step 1+2+3: killing app, writing engineeringMode=false "
                "+ clinicalMode=false (research mode — #234 makes raw "
                "CSVs a research feature), writeRawCsv=false (will "
                "toggle ON via UI), pinning dataDirectory"
            )
            log.info("=" * 60)
            _kill_bloodflow_processes()
            time.sleep(2)
            # Park any pre-existing local overrides (restored in step 9)
            # so the shipped-config seeding below is what the app boots
            # with — see the step-0 comment.
            local_path.unlink(missing_ok=True)
            test_data_dir.mkdir(parents=True, exist_ok=True)
            # Research mode, NOT engineering: since #234 the raw-CSV
            # toggle lives in the research-visible Data Output card, so
            # this doubles as the hand-off check that research users
            # really can enable raw CSVs without the engineering unlock.
            write_app_config_value("engineeringMode",     False)
            write_app_config_value("clinicalMode",       False)
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
            scan_output_dir.mkdir(parents=True, exist_ok=True)
            files_before = {p.name for p in scan_output_dir.iterdir() if p.is_file()}

            # ─── Step 5: open Settings, toggle raw CSV, set duration ─
            log.info("=" * 60)
            log.info(
                "Step 5: opening Settings, toggling Save raw CSV ON, "
                f"setting Raw CSV duration to {RAW_CSV_DURATION_SEC} s"
            )
            log.info("=" * 60)
            click_panel("Settings")
            time.sleep(SLEEP)
            # The Data Output section is far down a long modal. The
            # Save raw CSV switch carries an Accessible.name (#234),
            # so it surfaces in the UIA tree directly and doubles as
            # the scroll anchor.
            assert _scroll_until_label_visible("Save raw CSV"), (
                "Could not bring the Data Output section into view "
                "after scrolling the Settings modal. Either the modal "
                "didn't open or the section is hidden (clinicalMode "
                "flag not applied? the card shows for research and "
                "engineering modes)."
            )
            _toggle_raw_csv_save_on()
            _set_raw_csv_duration_sec(RAW_CSV_DURATION_SEC)
            require_focus_via_ensure_visible()
            pyautogui.press("escape")  # close Settings → commits writes
            time.sleep(SLEEP)

            # Sanity-check what the connector actually persisted. The
            # PillSwitch and TextField call setWriteRawCsv /
            # setRawCsvDurationSec, and since #233 config_store writes
            # changed keys ONLY to the local overrides file — the
            # shipped app_config.json is never touched at runtime, so
            # these reads MUST hit app_config.local.json.
            wrote = read_local_config_value("writeRawCsv", False)
            dur   = read_local_config_value("rawCsvDurationSec", None)
            assert wrote is True, (
                f"Settings UI didn't persist writeRawCsv=True to "
                f"{resolve_local_config_path()} (got {wrote!r}). Either "
                f"the toggle click missed the PillSwitch (check "
                f"_toggle_raw_csv_save_on) or config_store's write "
                f"path changed."
            )
            assert dur is not None and abs(float(dur) - RAW_CSV_DURATION_SEC) < 1, (
                f"Settings UI didn't persist rawCsvDurationSec="
                f"{RAW_CSV_DURATION_SEC} to the local overrides file "
                f"(got {dur!r}). The TextField likely didn't accept the "
                f"typed input — check _set_raw_csv_duration_sec."
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
            if not _scan_settings_modal_open():
                log.warning(
                    "  Scan Settings modal not detected after click — "
                    "recalibrating panel buttons and retrying"
                )
                recalibrate_panel_buttons()
                click_panel("Scan\nSettings")
                time.sleep(SLEEP)
            assert _scan_settings_modal_open(), (
                "Scan Settings modal did not open even after a "
                "recalibrated retry — the panel click is landing "
                f"somewhere else. UIA texts seen: {_dump_uia_texts()}"
            )
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
            scan_files = _new_csvs_in(scan_output_dir, files_before)
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
            assert left_span  < canonical_span - 15, (
                f"Left raw CSV span ({left_span:.1f} s) should be at "
                f"least 15 s shorter than canonical ({canonical_span:.1f} s); "
                f"raw truncation likely didn't fire."
            )
            assert right_span < canonical_span - 15, (
                f"Right raw CSV span ({right_span:.1f} s) should be at "
                f"least 15 s shorter than canonical ({canonical_span:.1f} s); "
                f"raw truncation likely didn't fire."
            )
            log.info("  PASS: all three CSVs span their expected durations")

            # ─── Payload integrity (per-row sum + frame sequencing + cam count)
            #
            # We assert tightly on the two checks that SHOULD always be
            # zero on a healthy scan:
            #   * bad_sum:           every row's histogram sums to the
            #                        configured photon-count constant.
            #   * frame_id_skipped:  no dropped frames (mod-256 rollover
            #                        is normal and not flagged).
            #
            # We allow ``bad_frame_cam_count <= 1`` because the SDK
            # closes raw CSVs mid-frame at the truncation boundary,
            # leaving exactly one partial frame with fewer cam rows.
            # Anything beyond that points at real frame loss during
            # the scan.
            for label in ("left_raw", "right_raw"):
                path = scan_files[label]
                log.info(f"  validating payload of {path.name}")
                counts, output = validate_raw_csv_payload(path)
                assert counts, (
                    f"{path.name}: SDK check_csv setup failed.\n"
                    f"{output.strip()}"
                )
                bad_sum     = counts.get("bad_sum", -1)
                fid_skipped = counts.get("frame_id_skipped", -1)
                bad_cams    = counts.get("bad_frame_cam_count", -1)
                assert bad_sum == 0, (
                    f"{path.name}: {bad_sum} row(s) failed the "
                    f"per-row histogram sum check.\n"
                    f"--- check_csv output ---\n{output.strip()}"
                )
                assert fid_skipped == 0, (
                    f"{path.name}: {fid_skipped} frame_id(s) dropped "
                    f"during the scan.\n"
                    f"--- check_csv output ---\n{output.strip()}"
                )
                assert bad_cams <= 1, (
                    f"{path.name}: {bad_cams} frame(s) had a wrong "
                    f"cam_id count. Up to 1 partial frame is expected "
                    f"at the truncation boundary; this run shows more, "
                    f"which suggests real frame loss.\n"
                    f"--- check_csv output ---\n{output.strip()}"
                )
                log.info(
                    f"    {path.name}: PASS "
                    f"(bad_sum={bad_sum}, frame_id_skipped={fid_skipped}, "
                    f"bad_frame_cam_count={bad_cams})"
                )

        finally:
            # ─── Step 9 (cleanup): restore original config on disk ───
            # No kill / relaunch needed: the running app already
            # consumed our config (writeRawCsv / rawCsvDurationSec /
            # dataDirectory take effect immediately via the connector
            # slots; engineeringMode / clinicalMode were captured at
            # launch and won't change for THIS app instance regardless
            # of what we write back). Whatever runs next is responsible
            # for its own kill+relaunch if it needs the disk values
            # honoured at startup.
            log.info("=" * 60)
            log.info(
                "Step 9 (cleanup): byte-restoring shipped config and "
                f"local overrides (snapshotted values were: {original})"
            )
            log.info("=" * 60)
            cfg_path.write_bytes(cfg_bytes)
            if local_bytes is None:
                local_path.unlink(missing_ok=True)
            else:
                local_path.write_bytes(local_bytes)
            log.info("  cleanup complete; original config restored byte-exact")
