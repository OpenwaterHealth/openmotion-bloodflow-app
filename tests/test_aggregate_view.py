"""
QA HIL verification of the research plot's Aggregate view (issue #621).

Aggregate folds each sensor module's 8 cameras into 4 plots by averaging
the mirrored camera pairs 1+8, 2+7, 3+6, 4+5 (NaN-aware: an unlit camera
drops out, both unlit is a gap, a pair with one enabled camera shows that
camera). Display only — the recorded scan is unchanged.

What this module can and cannot automate on the 1.5.x research build:

  * The plot page exposes NOTHING to UI Automation once a scan is in the
    viewer (verified 2026-09-29: the whole window is one empty Group).
    Cell labels ("LEFT 1+8") and value labels ("BFI 0.17") are plain QML
    Text and cannot be read. There is no OCR on the bench.
  * The ⋯ menu button and its View segments are located geometrically
    from the window's client rect and DPI (QML constants in
    PlotViewer.qml), and their state is read from screen pixels: the ⋯
    button and the selected segment fill with AppTheme.accentInteractive.
  * The numbers are checked two ways instead: test_aggregate_oracle.py
    proves the app's pair derivation (what value_at() reads for the
    labels) matches a spec oracle on every capture of the export; and
    this module saves a screenshot of each view plus the oracle's
    expected values at the replay edge, so the operator compares the
    on-screen labels against known numbers and attaches both to the
    report.

Flow:
  1. History → newest scan → Load (log-verified) replays it in the viewer.
  2. ⋯ → Individual; screenshot.
  3. ⋯ → Aggregate; screenshot.
  4. Expected pair values at the replay edge written beside the shots.
  5. ⋯ → Average; screenshot; ⋯ → Individual; screenshot.
  6. App still alive.

Run:
    pytest test_aggregate_view.py            # HIL, needs the app + a past research scan
    pytest test_aggregate_oracle.py          # the CSV oracle + derivation check, no app

Optional env: AGGREGATE_EXPORT_CSV=<path> pins the export used in step 4.
"""

from __future__ import annotations

import ctypes
import math
import re
import time
from pathlib import Path

import pyautogui
import pytest

from conftest import (
    SLEEP,
    get_app_window,
    log,
    require_focus,
    uia_window,
)
from hil_helpers import (
    click_element_center,
    click_panel,
    find_app_log,
    is_app_alive,
    log_size,
    wait_for_pattern,
)
from test_aggregate_oracle import (
    METRICS,
    PAIRS,
    TEST_LOGS,
    expected_pair_table,
    find_export_csv,
    finite,
    load_export,
)

pytestmark = pytest.mark.dev

# The pair view is research-only (#621) and the History/plot layout this
# module drives is the research one. Applied by conftest's
# pytest_collection_finish only when this module has selected tests;
# restored byte-exact at session end.
FORCE_APP_CONFIG = {"clinicalMode": False}

# HistoryModal's load button reads "Load in viewer  →" with no row focused
# and "Load “<label>”  →" once a row is; match either.
_RE_LOAD_BTN = re.compile(r"^Load\b.*→\s*$")
# HistoryModal logs this from QML the moment it opens.
_RE_HISTORY_OPENED = re.compile(r"\[History\] opened")
# motion_connector logs this when a past scan's buffers reach the viewer
# (the History modal closes itself on the same signal).
_RE_PLOT_LOADED = re.compile(r"\[Plot\] loaded past scan")
# Texts that only exist while the History modal is up (its Buttons are
# exposed; its Texts are not).
_HISTORY_MARKERS = ("Export CSV", "Load in viewer", "Load “")

# ── Plot overlay geometry (logical px, from PlotViewer.qml / BloodFlow.qml) ──
# ⋯ button centre from the window client's bottom-right:
#   right: page margin 8 + _overlayEdgeMarginPx (12+12) + half button 18 = 50
#   bottom: page margin 8 + _overlayBottomMarginPx (12 + 36 + 12) + 18 = 86
# (statsInset is 0 while the Statistics pane is hidden.)
MENU_BTN_FROM_BR = (50, 86)
# View segment centres relative to the ⋯ button centre, measured on the
# 1.5.4-dev.0 research build (popup anchored to the button's top-right).
SEGMENT_FROM_MENU_BTN = {
    "Individual": (-203, -231),
    "Aggregate": (-125, -231),
    "Average": (-53, -231),
}
SEGMENT_HALF_WIDTH = 30   # logical; pixel probe sits this far left of centre

STATE: dict = {}


# ─────────────────────────────────────────────
# Window geometry
# ─────────────────────────────────────────────
class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _client_geometry() -> tuple[tuple[int, int, int, int], float]:
    """((left, top, right, bottom) of the app window's client area in
    screen px, DPI scale). Frame-free, unlike pygetwindow's rect."""
    hwnd = get_app_window()._hWnd
    u = ctypes.windll.user32
    rc = _RECT()
    u.GetClientRect(hwnd, ctypes.byref(rc))
    pt = _POINT(0, 0)
    u.ClientToScreen(hwnd, ctypes.byref(pt))
    scale = u.GetDpiForWindow(hwnd) / 96.0
    return (pt.x, pt.y, pt.x + rc.right, pt.y + rc.bottom), scale


def _menu_button_pos() -> tuple[int, int, float]:
    (_l, _t, r, b), s = _client_geometry()
    dx, dy = MENU_BTN_FROM_BR
    return int(r - dx * s), int(b - dy * s), s


def _segment_pos(label: str) -> tuple[int, int]:
    """Prefer the UIA Button (Accessible.role Button + name) when the
    popup is exposed; else the measured offset from the ⋯ button."""
    elem = _find_button(re.compile(rf"^{re.escape(label)}$"))
    if elem is not None:
        r = elem.rectangle()
        return (r.left + r.right) // 2, (r.top + r.bottom) // 2
    bx, by, s = _menu_button_pos()
    dx, dy = SEGMENT_FROM_MENU_BTN[label]
    return int(bx + dx * s), int(by + dy * s)


# ─────────────────────────────────────────────
# Pixels
# ─────────────────────────────────────────────
def _is_accent(px: tuple) -> bool:
    """AppTheme.accentInteractive (#4A90E2, dark theme) over the dark UI:
    clearly blue — blue channel well above red and bright."""
    r, g, b = px[:3]
    return b >= 150 and b - r >= 60


def _pixel(x: int, y: int) -> tuple:
    return tuple(pyautogui.pixel(x, y))


def _click_at(x: int, y: int, what: str) -> None:
    log.info(f"  {what}: click ({x}, {y})")
    pyautogui.moveTo(x, y, duration=0.3)
    pyautogui.click(x, y)
    time.sleep(SLEEP)


def screenshot(name: str) -> Path:
    """Save the app window to test_logs/aggregate_view_<name>.png."""
    TEST_LOGS.mkdir(exist_ok=True)
    (l, t, r, b), _ = _client_geometry()
    out = TEST_LOGS / f"aggregate_view_{name}.png"
    # Move the pointer off the plots so the hover tooltip isn't in the shot.
    pyautogui.moveTo(l + 5, t + 5, duration=0.2)
    time.sleep(0.5)
    pyautogui.screenshot(str(out), region=(l, t, r - l, b - t))
    log.info(f"  screenshot -> {out}")
    return out


# ─────────────────────────────────────────────
# UIA
# ─────────────────────────────────────────────
def _all_elements() -> list:
    try:
        return list(uia_window().descendants())
    except Exception as e:
        log.warning(f"  UIA scan failed: {e}")
        return []


def _texts() -> set[str]:
    out = set()
    for elem in _all_elements():
        try:
            t = (elem.window_text() or "").strip()
            if t:
                out.add(t)
        except Exception:
            continue
    return out


def _find_button(rx: re.Pattern):
    """First UIA Button (then any element) whose text matches ``rx``."""
    win = uia_window()
    for ctype in ("Button", None):
        try:
            elems = win.descendants(control_type=ctype) if ctype else win.descendants()
        except Exception:
            continue
        for elem in elems:
            try:
                if rx.match((elem.window_text() or "").strip()):
                    return elem
            except Exception:
                continue
    return None


def _dump_uia(tag: str, limit: int = 150) -> None:
    """Log what UIA exposes right now (control type, text, rect)."""
    rows = []
    for elem in _all_elements():
        try:
            ct = elem.element_info.control_type
            txt = (elem.window_text() or "").strip()
            r = elem.rectangle()
            rows.append(f"{ct:<10} {txt[:40]!r:<44} ({r.left},{r.top})-({r.right},{r.bottom})")
        except Exception:
            continue
    log.info(f"  UIA dump [{tag}]: {len(rows)} elements")
    for row in rows[:limit]:
        log.info(f"     {row}")


def _edit_rects() -> list:
    try:
        return [e.rectangle() for e in uia_window().descendants(control_type="Edit")]
    except Exception:
        return []


def _rect_key(r) -> tuple:
    return (r.left, r.top, r.right, r.bottom)


# ─────────────────────────────────────────────
# History → viewer
# ─────────────────────────────────────────────
def _history_open() -> bool:
    texts = _texts()
    return any(m in t for m in _HISTORY_MARKERS for t in texts)


def _focus_first_history_row(before_edits: list) -> None:
    """Click the newest row's body so it becomes the active (Load) row.

    The rows are plain Text and never reach UIA, but the modal's search
    TextField is an Edit and does. Its logical height is 34 px, which
    gives the DPI scale; from its bottom edge the modal's ColumnLayout
    (spacing 12) stacks the 30 px column-header bar, the table (ListView
    margin 2), then the 34 px first row → first-row centre is 73 logical
    px below the search field. Any x on the row body outside the ☐
    column selects the row (the label Texts don't intercept clicks)."""
    known = {_rect_key(r) for r in before_edits}
    search = None
    deadline = time.time() + 8.0
    while time.time() < deadline and search is None:
        new = [r for r in _edit_rects() if _rect_key(r) not in known]
        if new:
            search = min(new, key=lambda r: r.top)
        else:
            time.sleep(0.5)
    if search is None:
        _dump_uia("History open, no search Edit")
        raise AssertionError("History modal's search field not exposed via UIA; cannot locate rows")
    scale = (search.bottom - search.top) / 34.0
    x = int(search.left + 10 * scale)
    y = int(search.bottom + 73 * scale)
    log.info(f"  search field {_rect_key(search)} → DPI scale {scale:.2f}; first row at ({x}, {y})")
    _click_at(x, y, "focus first History row")


def load_latest_scan_in_viewer() -> str | None:
    """Open History, focus the newest row, click its Load button, wait
    for the viewer to report the scan loaded. Returns the session label
    from the '[Plot] loaded past scan' log line."""
    app_log = find_app_log()
    assert app_log is not None, "app log not found — is the app running?"
    log_start = log_size(app_log)
    before_edits = _edit_rects()
    if not _history_open():
        click_panel("History")
        line = wait_for_pattern(_RE_HISTORY_OPENED, app_log, log_start, timeout=8.0)
        assert line is not None or _history_open(), (
            "History modal did not open (no '[History] opened' log line, no UIA marker)"
        )
        log.info(f"  History opened: {line or 'UIA marker'}")
        time.sleep(SLEEP)
    _focus_first_history_row(before_edits)
    focused = None
    deadline = time.time() + 6.0
    while time.time() < deadline and focused is None:
        elem = _find_button(_RE_LOAD_BTN)
        if elem is not None and "“" in (elem.window_text() or ""):
            focused = elem
        else:
            time.sleep(0.5)
    if focused is None:
        _dump_uia("row click did not focus a row")
        raise AssertionError("Load button never relabelled to the scan name — row click missed")
    log.info(f"  row focused: Load button reads {focused.window_text()!r}")
    # Pixel click, never InvokePattern: QML buttons accept the invoke and
    # do nothing.
    click_element_center(focused, "Load in viewer")
    line = wait_for_pattern(_RE_PLOT_LOADED, app_log, log_start, timeout=45.0)
    assert line is not None, "no '[Plot] loaded past scan' log line within 45s after Load"
    log.info(f"  {line[:160]}")
    m = re.search(r"loaded past scan '([^']*)'", line)
    time.sleep(SLEEP * 2)
    return m.group(1) if m else None


# ─────────────────────────────────────────────
# ⋯ menu
# ─────────────────────────────────────────────
def _menu_open() -> bool:
    """Popup up iff its View segments are exposed (Accessible Buttons) or
    the ⋯ button has taken the accent fill."""
    texts = _texts()
    if "Aggregate" in texts and "Individual" in texts:
        return True
    x, y, _ = _menu_button_pos()
    # Probe just off-centre so the "⋯" glyph itself isn't sampled.
    return _is_accent(_pixel(x - 8, y - 8))


def open_menu() -> None:
    if _menu_open():
        return
    if _history_open():
        log.info("  History modal still up — toggling it closed first")
        click_panel("History")
        time.sleep(SLEEP)
    x, y, s = _menu_button_pos()
    before = _pixel(x - 8, y - 8)
    tried = []
    # Exact estimate first, then a small ring in case the frame/DPI maths
    # is off by a few px (the button is a 36 px logical circle).
    for dx, dy in ((0, 0), (-12, 0), (12, 0), (0, -12), (0, 12), (-12, -12), (12, 12)):
        px, py = int(x + dx * s), int(y + dy * s)
        tried.append((px, py))
        _click_at(px, py, "open ⋯ menu")
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if _menu_open():
                STATE["menu_button"] = (x, y)
                log.info(f"  ⋯ menu open (button pixel {before} → {_pixel(x - 8, y - 8)})")
                return
            time.sleep(0.3)
    shot = screenshot("menu_failed")
    _dump_uia("⋯ menu did not open")
    raise AssertionError(f"plot ⋯ menu did not open; clicked {tried}; see {shot}")


def close_menu() -> None:
    if not _menu_open():
        return
    require_focus()
    pyautogui.press("escape")
    time.sleep(SLEEP)
    if _menu_open():
        x, y, _ = _menu_button_pos()
        _click_at(x, y, "close ⋯ menu (toggle)")
    assert not _menu_open(), "plot ⋯ menu would not close"


def select_view(label: str) -> None:
    """Pick Individual / Aggregate / Average in the ⋯ menu's View row,
    confirm the segment took the accent (selected) fill, close the menu."""
    open_menu()
    x, y = _segment_pos(label)
    _click_at(x, y, f"View → {label}")
    _, _, s = _menu_button_pos()
    probe = (int(x - SEGMENT_HALF_WIDTH * s), y)
    px = _pixel(*probe)
    selected = _is_accent(px)
    log.info(f"  {label} segment probe {probe} = {px} → {'selected' if selected else 'NOT selected'}")
    STATE.setdefault("segment_probe", {})[label] = (probe, px, selected)
    close_menu()
    assert selected, (
        f"View → {label}: segment at ({x}, {y}) did not take the selected fill "
        f"(probe pixel {px}); the click missed or the popup geometry changed"
    )


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────
@pytest.mark.incremental
class TestAggregateView:
    """Aggregate view on a replayed research scan (issue #621)."""

    def test_01_load_latest_scan_in_viewer(self, app):
        """History → newest scan → Load replays it in the embedded
        PlotViewer (confirmed by the '[Plot] loaded past scan' log line)."""
        label = load_latest_scan_in_viewer()
        STATE["session"] = label
        log.info(f"  loaded session: {label}")
        assert label, "could not read the loaded session label from the app log"
        assert is_app_alive()

    def test_02_individual_view(self, app):
        """⋯ → View → Individual selects (accent fill) and the 8-per-module
        camera grid is captured for the report."""
        select_view("Individual")
        STATE["shot_individual"] = screenshot("01_individual")

    def test_03_aggregate_view(self, app):
        """⋯ → View → Aggregate selects (accent fill) and the 4-per-module
        pair grid (LEFT 4+5 top … LEFT 1+8 bottom) is captured."""
        select_view("Aggregate")
        STATE["shot_aggregate"] = screenshot("02_aggregate")

    def test_04_expected_values_for_operator_readout(self, app):
        """Write the oracle's expected pair values at the replay edge next
        to the screenshots. The value labels are unreadable over UIA, so
        the operator compares them with these numbers (two decimals) and
        attaches both to the HIL report."""
        path = find_export_csv(STATE.get("session"))
        if path is None:
            pytest.skip(f"no CSV export for session {STATE.get('session')}; export it "
                        "from History → Export CSV and rerun for the value readout")
        last = expected_pair_table(load_export(path))[-1]
        lines = [
            f"session {STATE['session']}  export {path.name}",
            f"replay edge = last capture: frame {last['frame_id']} @ {last['timestamp_s']} s",
            "Aggregate value labels should read (BFI / BVI, two decimals):",
        ]
        for side in ("LEFT", "RIGHT"):
            s = side[0].lower()
            for a, b in PAIRS:
                vals = []
                for m in METRICS:
                    ev = last[f"exp_{m}_{s}{a}+{b}"]
                    vals.append(f"{m.upper()} {ev:.2f}" if finite(ev) else f"{m.upper()} --")
                cams = [f"cam{c}={last[f'bfi_{s}{c}']:.3f}" if finite(last[f"bfi_{s}{c}"]) else f"cam{c}=NaN"
                        for c in (a, b)]
                lines.append(f"  {side} {a}+{b}: {' / '.join(vals)}    (BFI inputs {', '.join(cams)})")
        lines.append("Individual view: each pair value must equal the NaN-aware mean of its two camera cells.")
        out = TEST_LOGS / f"aggregate_view_expected_{STATE['session']}.txt"
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        for ln in lines:
            log.info(f"  {ln}")
        log.warning(f"  OPERATOR STEP: compare {STATE.get('shot_aggregate')} against {out}")

    def test_05_average_then_back_to_individual(self, app):
        """Average selects and shows the AVG cells; Individual restores
        the camera grid. Both captured."""
        select_view("Average")
        screenshot("03_average")
        select_view("Individual")
        screenshot("04_individual_again")

    def test_06_app_still_alive(self, app):
        assert is_app_alive(), "app closed during Aggregate view checks"
        probes = STATE.get("segment_probe", {})
        assert all(v[2] for v in probes.values()), f"segment selection probes: {probes}"
