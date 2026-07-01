"""
Shared test utilities for the Open-Motion UI test suite.

This module is for plain helper functions — not pytest fixtures or hooks.
Fixtures live in ``conftest.py``; helpers that previously got copy-pasted
between test files live here.

Three categories so far:

  - **App-log tailing**: ``find_app_log``, ``log_size``, ``wait_for_pattern``
    plus the ``RE_CONNECTED`` / ``RE_DISCONNECTED`` regexes. Pair every
    power/connection event with a log-tail wait — see ``shelly.py`` docs.
  - **Window / UIA**: ``move_window_on_screen``, ``is_app_alive``,
    ``click_element_center``, ``focus_combobox_by_label``,
    ``selected_scan_text``.
  - **Modal handling**: ``dismiss_signal_quality_modal``, ``visible_modals``.

``SENSOR_OPTIONS`` is the canonical sensor-dropdown ordering used by both
the scan-settings and scan-auto-stop tests; keep it in sync with the QML
``SensorComboBox`` model if that list ever changes.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import pyautogui
import pygetwindow as gw

from conftest import (
    APP_KEYWORDS,
    SLEEP,
    ensure_visible,
    get_app_window,
    log,
    uia_window,
)

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

# SDK MotionConsole/MotionSensor _set_state lines look like
# "<name> state <OLD> -> <NEW> (<reason>)". Reaching the terminal CONNECTED
# transition implies USB enumeration + handshake completed.
RE_CONNECTED    = re.compile(r"state \S+ -> CONNECTED")
RE_DISCONNECTED = re.compile(r"state \S+ -> DISCONNECTED")

# Sensor dropdown options in the order they appear in the QML model. Keep
# this in sync with ``components/SensorComboBox.qml`` if the list changes.
SENSOR_OPTIONS = [
    "None", "Near", "Middle", "Far", "Outer",
    "Left", "Right", "Third Row", "All",
]

# Height of the auto-update banner (components/UpdateBanner.qml line 13:
# ``height: visible ? 36 : 0``). When the banner is shown, every
# coordinate below it shifts down by this amount, so coord-based clicks
# need to add it. UIA-discovered rectangles are absolute screen coords
# and already include the offset.
BANNER_OFFSET_PX = 36

# ButtonPanel pixel layout, derived from QML so it's window-size
# independent. Source files:
#   main.qml line 60       — outer Item topMargin: 65 + banner height
#   main.qml line 64       — outer Item leftMargin: 8
#   pages/BloodFlow.qml    — ButtonPanel anchors.margins: 8, width: 80
#   components/ButtonPanel.qml — ColumnLayout anchors.margins: 6, spacing: 4
#   components/ButtonPanel.qml — buttons 68×68, dividers contribute 9 px
# Distance between button centers = 68 + 4 + 9 + 4 = 85 px (button + gap +
# divider with its 4+1+4 margins + gap).
_PANEL_HEADER_PX     = 65   # header bar above the BloodFlow page
_PANEL_OUTER_LEFT    = 8    # main.qml outer Item leftMargin
_PANEL_INNER_TOP     = 8    # ButtonPanel anchors.margins: 8 inside BloodFlow
_PANEL_INNER_LEFT    = 8    # ditto, applied to left
_PANEL_CONTENT_MARG  = 6    # ColumnLayout anchors.margins: 6 inside ButtonPanel
_PANEL_BUTTON_HALF   = 34   # half of 68 px button
_PANEL_SLOT_PX       = 85   # vertical spacing between button centers

# Slot index for each top-of-panel button (counted from Start = 0)
# in normal (non-clinical) mode.
PANEL_BUTTON_SLOTS = {
    "Start":         0,
    "Scan\nSettings": 1,
    "Notes":          2,
    "Check":          3,
}

# In clinical mode, ``Scan\nSettings`` and ``Check`` are hidden
# (``visible: !panel.clinicalMode`` in components/ButtonPanel.qml),
# so the QML ColumnLayout collapses and Notes slides up into the
# former Scan Settings slot. The fallback screen-pos math needs to
# know this or it computes Notes at its non-clinical y coordinate
# and every click on Notes in clinical mode misses by one slot.
PANEL_BUTTON_SLOTS_CLINICAL = {
    "Start": 0,
    "Notes": 1,
}

# History and Settings sit at the bottom of the panel below a
# Layout.fillHeight spacer, so they're anchored from the bottom rather
# than slotted from the top. Slot index 0 = bottom-most (Settings).
PANEL_BUTTON_BOTTOM_SLOTS = {
    "Settings": 0,
    "History":  1,
}
_PANEL_INNER_BOTTOM = 8     # ButtonPanel anchors.margins: 8 inside BloodFlow (bottom)
_PANEL_OUTER_BOTTOM = 8     # main.qml outer Item bottomMargin: 8

# Module-level cache of calibrated button centers. Populated lazily on
# first click_panel() / get_panel_button_pos() call. Reset via
# recalibrate_panel_buttons() if the window is resized or moved.
_panel_button_cache: dict[str, tuple[int, int]] | None = None
_PANEL_BUTTON_LABELS = (
    "Start", "Scan\nSettings", "Notes", "Check", "History", "Settings",
)

_REPO_APP_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "app_config.json"
)


def _from_source_mode() -> bool:
    return os.environ.get("OPENWATER_FROM_SOURCE", "").lower() in (
        "1", "true", "yes",
    )


def _resolve_app_config_path() -> Path:
    """Return the app_config.json the running/about-to-run bloodflow app
    actually reads.

    From-source mode reads ``<repo>/config/app_config.json`` (cwd is
    PROJECT_ROOT when ``python main.py`` is launched). The packaged
    PyInstaller exe reads ``<exe-dir>/_internal/config/app_config.json``
    — its own bundled copy, NOT the repo's. Tests that write to the
    wrong file silently no-op against the running exe and the
    snapshot-and-restore dance leaves an alien value on disk afterward.

    Resolution order:
      1. ``OPENWATER_APP_CONFIG`` env var (explicit override).
      2. From-source mode  → repo config.
      3. ``OPENWATER_EXE`` env var → its sibling ``_internal/config``.
      4. Latest installed exe via the same glob set as conftest's
         ``_find_exe``  → its sibling ``_internal/config``.
      5. Fall back to the repo config (tests that don't run an exe
         still need somewhere to read/write).
    """
    explicit = os.environ.get("OPENWATER_APP_CONFIG", "")
    if explicit and Path(explicit).exists():
        return Path(explicit)
    if _from_source_mode():
        return _REPO_APP_CONFIG_PATH
    candidates: list[str] = []
    env_exe = os.environ.get("OPENWATER_EXE", "")
    if env_exe and os.path.exists(env_exe):
        candidates.append(env_exe)
    else:
        import glob as _glob
        for pattern in (
            r"C:\Users\*\Documents\OpenMotion\**\Open-Motion.exe",
            r"C:\Users\*\Desktop\**\Open-Motion.exe",
            r"C:\Program Files\**\Open-Motion.exe",
            r"C:\Program Files (x86)\**\Open-Motion.exe",
        ):
            candidates.extend(_glob.glob(pattern, recursive=True))
    if candidates:
        latest = max(candidates, key=os.path.getmtime)
        bundled = Path(latest).parent / "_internal" / "config" / "app_config.json"
        if bundled.exists():
            return bundled
    return _REPO_APP_CONFIG_PATH


# ─────────────────────────────────────────────
# App config (read / write / snapshot-and-force)
# ─────────────────────────────────────────────
#
# Test modules that need to pin a specific app_config.json value before
# the bloodflow app launches (e.g. clinicalMode=false, so the modal
# layout matches what tab walks expect) can:
#
#     from hil_helpers import force_app_config_value, write_app_config_value
#
#     _INITIAL_CLINICAL_MODE = force_app_config_value("clinicalMode", False)
#
#     @pytest.fixture(scope="module", autouse=True)
#     def _restore_clinical_mode():
#         yield
#         write_app_config_value("clinicalMode", _INITIAL_CLINICAL_MODE)
#
# Caveat: the connector caches its config at startup, so writes only
# affect the *next* app launch. Force calls happen at module-import
# time (before the session-scoped ``app`` fixture runs) precisely so
# any fresh launch in this session boots with the desired value. If
# the app was already running before pytest started, see
# ``force_app_config_value``'s warning log.

def read_app_config_value(key: str, default: Any = None) -> Any:
    """Return the value of ``key`` in app_config.json, or ``default``
    if the key is absent or the file is missing/unreadable."""
    try:
        with _resolve_app_config_path().open(encoding="utf-8") as fh:
            return json.load(fh).get(key, default)
    except Exception:
        return default


def write_app_config_value(key: str, value: Any) -> None:
    """Persist ``key=value`` to app_config.json. Logs a warning on
    failure rather than raising — config IO shouldn't take a test
    down."""
    path = _resolve_app_config_path()
    try:
        with path.open(encoding="utf-8") as fh:
            cfg = json.load(fh)
        cfg[key] = value
        with path.open("w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except Exception as e:
        log.warning(f"  Failed to persist {key}={value} at {path}: {e}")


def force_app_config_value(key: str, value: Any) -> Any:
    """Snapshot the current value of ``key``, write ``value`` if it
    differs, return the original. Pair with a teardown fixture that
    calls ``write_app_config_value(key, original)`` so the file ends
    in the same state we found it.

    The running app caches its config at startup and won't reload, so
    if a write happened the warning log tells the operator to relaunch
    if they want this session's tests to actually see the new value.
    """
    initial = read_app_config_value(key)
    if initial != value:
        write_app_config_value(key, value)
        log.warning(
            f"  app_config.json {key} was {initial!r}; forced it to "
            f"{value!r} for this test module — relaunch the app if it "
            f"was already running, otherwise the running instance is "
            f"still on the old value and tests may fail."
        )
    return initial


# ─────────────────────────────────────────────
# App-log tailing
# ─────────────────────────────────────────────
def find_app_log() -> Path | None:
    """Locate the most recently modified bloodflow app log."""
    home = Path.home()
    project_root = Path(__file__).resolve().parent.parent
    roots = [
        Path.cwd(),
        project_root,  # when launched via OPENWATER_FROM_SOURCE
        home / "Documents" / "Open-Motion",
        home / "Documents" / "OpenMotion",
    ]
    candidates: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        candidates.extend(root.glob("logs/ow-bloodflowapp-*.log"))
        candidates.extend(root.glob("**/logs/ow-bloodflowapp-*.log"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def log_size(path: Path) -> int:
    """Return ``path`` size in bytes, or 0 if the file is unreadable."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def wait_for_pattern(
    pattern: re.Pattern,
    log_path: Path,
    start_offset: int,
    timeout: float,
    poll: float = 0.5,
) -> str | None:
    """Tail ``log_path`` from ``start_offset``; return the first matching line within ``timeout``."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(start_offset)
                for line in f:
                    if pattern.search(line):
                        return line.strip()
        except OSError:
            pass
        time.sleep(poll)
    return None


# ─────────────────────────────────────────────
# Window / UIA
# ─────────────────────────────────────────────
def is_app_alive() -> bool:
    """Return True if the app window still exists (matched by APP_KEYWORDS)."""
    for w in gw.getAllWindows():
        if any(k in w.title.lower() for k in APP_KEYWORDS):
            return True
    return False


def move_window_on_screen():
    """Move the app window onto the primary screen if it is off-screen."""
    try:
        w = get_app_window()
        screen_w, screen_h = pyautogui.size()
        if w.left < 0 or w.top < 0 or w.left > screen_w or w.top > screen_h:
            log.warning(
                f"  Window is off-screen at ({w.left}, {w.top}) — "
                f"moving to primary display"
            )
            w.moveTo(50, 50)
            time.sleep(1)
            log.info(f"  Window moved to ({w.left}, {w.top})")
    except Exception as e:
        log.warning(f"  move_window_on_screen failed: {e}")


def click_element_center(elem, label: str = ""):
    """Click the center of a UIA element. Repositions the app window if the element is off-screen."""
    rect = elem.rectangle()
    cx = (rect.left + rect.right) // 2
    cy = (rect.top + rect.bottom) // 2
    screen_w, screen_h = pyautogui.size()
    if cx < 0 or cy < 0 or cx > screen_w or cy > screen_h:
        log.warning(
            f"     '{label}' is off-screen at ({cx}, {cy}), "
            f"screen={screen_w}x{screen_h} — moving window on-screen"
        )
        move_window_on_screen()
        rect = elem.rectangle()
        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        log.info(f"     after reposition: '{label}' at ({cx}, {cy})")
    log.info(f"     click '{label}' at ({cx}, {cy})")
    pyautogui.click(cx, cy)


def focus_combobox_by_label(label: str) -> bool:
    """Find and click a ComboBox by its associated label name (or by index).

    Method 1 — UIA name match: ComboBox whose UIA title equals the label.
    Method 2 — Label proximity: find a Text element with that label then click
               the nearest ComboBox by vertical row.
    Method 3 — Index fallback: this QML app does not expose accessible names
               for its ComboBoxes, so fall back to clicking the Nth ComboBox
               ('Left Sensor' → index 0, 'Right Sensor' → index 1).
    """
    try:
        win = uia_window()

        # Method 1: ComboBox has the label as its accessible name
        for ct in ["ComboBox", "Custom"]:
            try:
                elem = win.child_window(title=label, control_type=ct)
                if elem.exists(timeout=2):
                    rect = elem.rectangle()
                    cx = (rect.left + rect.right) // 2
                    cy = (rect.top + rect.bottom) // 2
                    pyautogui.moveTo(cx, cy, duration=0.3)
                    pyautogui.click(cx, cy)
                    time.sleep(0.3)
                    log.info(f"     UIA name lookup '{label}': PASSED")
                    return True
            except Exception:
                continue

        # Method 2: find the Text label, then click the nearest ComboBox row
        try:
            text_elems = win.descendants(title=label, control_type="Text")
            if not text_elems:
                text_elems = [e for e in win.descendants(title=label)]
            if text_elems:
                label_cy = (
                    text_elems[0].rectangle().top
                    + text_elems[0].rectangle().bottom
                ) // 2
                cbs = win.descendants(control_type="ComboBox")
                if cbs:
                    closest = min(
                        cbs,
                        key=lambda c: abs(
                            (c.rectangle().top + c.rectangle().bottom) // 2
                            - label_cy
                        ),
                    )
                    rect = closest.rectangle()
                    cx = (rect.left + rect.right) // 2
                    cy = (rect.top + rect.bottom) // 2
                    pyautogui.moveTo(cx, cy, duration=0.3)
                    pyautogui.click(cx, cy)
                    time.sleep(0.3)
                    log.info(f"     Label proximity lookup '{label}': PASSED")
                    return True
        except Exception as e:
            log.warning(f"     Label proximity lookup '{label}' failed: {e}")

        # Method 3: index-based fallback — Left=0, Right=1
        log.warning(
            f"     UIA name/proximity lookup '{label}': FAILED — "
            f"using index-based fallback"
        )
        cb_index = 0 if "Left" in label else 1
        cbs = win.descendants(control_type="ComboBox")
        if len(cbs) > cb_index:
            rect = cbs[cb_index].rectangle()
            cx = (rect.left + rect.right) // 2
            cy = (rect.top + rect.bottom) // 2
            pyautogui.moveTo(cx, cy, duration=0.3)
            pyautogui.click(cx, cy)
            time.sleep(0.3)
            log.info(
                f"     ComboBox[{cb_index}] index fallback for '{label}': PASSED"
            )
            return True

    except Exception as e:
        log.warning(f"     focus_combobox_by_label('{label}') failed: {e}")

    return False


def _detect_banner_state() -> bool | None:
    """Tri-state banner detector. Returns True/False when confident,
    None when UIA can't tell.

    Order of precedence:
      1. ``$OPENWATER_BANNER`` env var: ``1/true/yes`` → True,
         ``0/false/no`` → False, anything else → fall through to UIA.
      2. UIA title-based search for banner-unique text/buttons.
      3. UIA descendants walk for any Text containing the version-prompt
         phrase (handles RichText with embedded ``<b>`` tags).

    Qt's QML accessibility doesn't reliably expose plain ``Text`` items,
    so all three UIA strategies can quietly find nothing — that's the
    "None" case, which the layout code treats as "use the overlap-zone
    fudge so we hit the button either way".
    """
    env = os.environ.get("OPENWATER_BANNER", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False

    try:
        win = uia_window()
    except Exception:
        return None

    # Strategy 1: title-based search for banner-unique content.
    # We deliberately don't include "✕" (the dismiss button) because the
    # same glyph appears on other modal close buttons.
    for sig in ("A new version is available", "Download", "⚠"):
        try:
            for m in win.descendants(title=sig):
                rect = m.rectangle()
                if rect.bottom - rect.top > 0:
                    return True
        except Exception:
            continue

    # Strategy 2: walk descendants and inspect window_text. Qt RichText
    # can render either the plain string or the HTML markup, so we match
    # both 'new version is available' and any string containing '<b>'.
    try:
        for elem in win.descendants():
            try:
                text = elem.window_text() or ""
            except Exception:
                continue
            if not text:
                continue
            if (
                "new version is available" in text.lower()
                or ("<b>" in text and "version" in text.lower())
            ):
                rect = elem.rectangle()
                if rect.bottom - rect.top > 0:
                    return True
    except Exception:
        return None

    # No positive signal — could be banner absent OR Qt accessibility
    # silently dropping the banner's Text elements. Caller should treat
    # this as uncertain rather than as a definitive False.
    return None


def is_updater_banner_visible() -> bool:
    """Best-guess boolean: True if we're confident the banner is up.

    Tri-state-uncertain returns False here. Call ``_detect_banner_state``
    directly if you need to distinguish "definitely down" from "unknown".
    """
    return _detect_banner_state() is True


def selected_scan_text() -> str:
    """Read the current text of the scan-picker ComboBox in History."""
    try:
        win = uia_window()
        cb = win.child_window(control_type="ComboBox")
        if cb.exists(timeout=2):
            return cb.window_text().strip()
    except Exception:
        pass
    return ""


def panel_button_screen_pos(label: str) -> tuple[int, int] | None:
    """Compute absolute screen (x, y) for a sidebar panel button.

    Pixel-based, derived from the QML layout. Used as a fallback when
    UIA discovery fails. Returns ``None`` if the label isn't in either
    the top-anchored or bottom-anchored slot map.

    Top-anchored buttons (Start, Scan Settings, Notes, Check) measure
    from the page top, with tri-state banner handling:

      - banner detected as **visible**  → shift y by ``BANNER_OFFSET_PX``
        (36 px) so we hit the button center exactly.
      - banner detected as **hidden**   → no shift; hit center exactly.
      - **uncertain** (Qt accessibility silently drops the banner texts)
        → shift y by ``BANNER_OFFSET_PX // 2`` (18 px). This lands in the
        32-px overlap of the no-banner and with-banner hitboxes (button
        is 68 px tall, banner shift is 36 px, so the two possible button
        positions overlap by 68 − 36 = 32 px). The click hits either way.

    Bottom-anchored buttons (History, Settings) measure from the page
    bottom; they don't shift with the banner because main.qml only
    grows the page-Item's topMargin when the banner appears, leaving
    the bottom anchored to parent.bottom.

    When ``clinicalMode`` is enabled in ``app_config.json`` we use the
    clinical slot map — Notes moves up into slot 1, and
    ``Scan\\nSettings`` / ``Check`` have no slot at all (they are
    ``visible: false`` in ButtonPanel.qml and shouldn't be clicked).
    """
    w = get_app_window()
    x = w.left + _PANEL_OUTER_LEFT + _PANEL_INNER_LEFT + _PANEL_CONTENT_MARG + _PANEL_BUTTON_HALF

    if read_app_config_value("clinicalMode", False):
        top_slots = PANEL_BUTTON_SLOTS_CLINICAL
    else:
        top_slots = PANEL_BUTTON_SLOTS

    if label in top_slots:
        slot = top_slots[label]
        state = _detect_banner_state()
        if state is True:
            header_px = _PANEL_HEADER_PX + BANNER_OFFSET_PX
            y_fudge = 0
        elif state is False:
            header_px = _PANEL_HEADER_PX
            y_fudge = 0
        else:  # uncertain
            header_px = _PANEL_HEADER_PX
            y_fudge = BANNER_OFFSET_PX // 2
        y = (w.top + header_px + _PANEL_INNER_TOP + _PANEL_CONTENT_MARG
             + _PANEL_BUTTON_HALF + slot * _PANEL_SLOT_PX + y_fudge)
        return x, y

    if label in PANEL_BUTTON_BOTTOM_SLOTS:
        slot = PANEL_BUTTON_BOTTOM_SLOTS[label]
        # Slot 0 = Settings (lowest), slot 1 = History (one button above).
        y = (w.bottom - _PANEL_OUTER_BOTTOM - _PANEL_INNER_BOTTOM
             - _PANEL_CONTENT_MARG - _PANEL_BUTTON_HALF
             - slot * _PANEL_SLOT_PX)
        return x, y

    return None


# ─────────────────────────────────────────────
# Per-machine panel button calibration
# ─────────────────────────────────────────────
def _calibrate_panel_buttons() -> dict[str, tuple[int, int]]:
    """Find each panel button's screen center for the current machine.

    Walks the UIA tree once and matches every visible Text element
    against the panel-button label set. UIA returns absolute screen
    pixels (DPI-aware), so this works correctly regardless of the
    runner's DPI scale or window size — fixing the per-machine
    coordinate-drift problem the static SIDEBAR_* constants have.

    Anything UIA can't find falls back to ``panel_button_screen_pos``
    (the QML pixel layout), which assumes a default-sized window but
    at least gets us in the right zip code.

    Returns a dict mapping each known label to (cx, cy). Missing
    labels mean both UIA and the QML fallback failed for that button.
    """
    move_window_on_screen()
    rects: dict[str, tuple[int, int]] = {}

    # All variants we'll accept for each label. PyQt's Qt-accessibility
    # bridge sometimes flattens the QML "Scan\nSettings" text into a
    # single line with newline preserved, sometimes into "Scan Settings"
    # or just "Scan". Try them all; first match wins per label.
    variants: dict[str, set[str]] = {}
    for lbl in _PANEL_BUTTON_LABELS:
        variants[lbl] = {
            lbl,
            lbl.replace("\n", " "),
            lbl.replace("\n", ""),
            lbl.split("\n")[0],
        }
    # Start is special: its visible label depends on connection state
    # ("Start" / "Stop" / "Disconnected"). Add the alternates.
    variants["Start"].update({"Stop", "Disconnected"})

    try:
        win = uia_window()
    except Exception as e:
        log.warning(f"  calibrate: uia_window failed: {e}")
        win = None

    seen_centers: set[tuple[int, int]] = set()
    if win is not None:
        try:
            for elem in win.descendants():
                if len(rects) == len(_PANEL_BUTTON_LABELS):
                    break
                try:
                    txt = (elem.window_text() or "").strip()
                except Exception:
                    continue
                if not txt:
                    continue
                for lbl in _PANEL_BUTTON_LABELS:
                    if lbl in rects:
                        continue
                    if txt not in variants[lbl]:
                        continue
                    try:
                        rect = elem.rectangle()
                    except Exception:
                        continue
                    if rect.right <= rect.left or rect.bottom <= rect.top:
                        continue
                    cx = (rect.left + rect.right) // 2
                    cy = (rect.top + rect.bottom) // 2
                    if (cx, cy) in seen_centers:
                        continue
                    seen_centers.add((cx, cy))
                    rects[lbl] = (cx, cy)
                    log.info(
                        f"  calibrated panel '{lbl!r}' → ({cx}, {cy}) "
                        f"via UIA text='{txt}'"
                    )
                    break
        except Exception as e:
            log.warning(f"  calibrate: descendants walk failed: {e}")

    # Fall back to the QML pixel layout for any label UIA didn't find.
    for lbl in _PANEL_BUTTON_LABELS:
        if lbl in rects:
            continue
        pos = panel_button_screen_pos(lbl)
        if pos is not None:
            rects[lbl] = pos
            log.info(
                f"  calibrated panel '{lbl!r}' → {pos} via QML pixel "
                f"layout (UIA fallback)"
            )
        else:
            log.warning(
                f"  calibrated panel '{lbl!r}' → NOT FOUND "
                f"(UIA missed and no QML slot)"
            )

    return rects


def calibrate_panel_buttons() -> dict[str, tuple[int, int]]:
    """Public: populate the cache and return the per-machine button map."""
    global _panel_button_cache
    _panel_button_cache = _calibrate_panel_buttons()
    return _panel_button_cache


def recalibrate_panel_buttons() -> dict[str, tuple[int, int]]:
    """Public: wipe the cache and re-discover. Use after window moves
    or any state change that might invalidate cached rects."""
    global _panel_button_cache
    _panel_button_cache = None
    return calibrate_panel_buttons()


def get_panel_button_pos(label: str) -> tuple[int, int]:
    """Return cached center for ``label``, calibrating on first use."""
    global _panel_button_cache
    if _panel_button_cache is None:
        _panel_button_cache = _calibrate_panel_buttons()
    if label in _panel_button_cache:
        return _panel_button_cache[label]
    # One retry — maybe the window state changed since first calibration.
    log.warning(
        f"  get_panel_button_pos('{label!r}') miss; recalibrating once"
    )
    _panel_button_cache = _calibrate_panel_buttons()
    if label in _panel_button_cache:
        return _panel_button_cache[label]
    raise RuntimeError(
        f"Panel button '{label!r}' not found after recalibration. "
        f"Available: {list(_panel_button_cache.keys())}"
    )


def click_panel(label: str) -> None:
    """Click a sidebar panel button by its label.

    Uses per-machine calibrated coordinates (UIA-discovered with QML
    pixel-layout fallback), so it works correctly regardless of DPI
    scaling or window size. Replaces ``click_sidebar(*SIDEBAR_X, ...)``
    pattern at panel-button click sites.

    Labels: ``"Start"``, ``"Scan\\nSettings"``, ``"Notes"``, ``"Check"``,
    ``"History"``, ``"Settings"``.
    """
    ensure_visible()
    cx, cy = get_panel_button_pos(label)
    log.info(f"  click panel '{label!r}' → ({cx}, {cy})")
    pyautogui.moveTo(cx, cy, duration=0.3)
    pyautogui.click(cx, cy)
    time.sleep(SLEEP)


def click_panel_button(label: str, fallback: tuple[float, float] | None = None) -> bool:
    """Click a ButtonPanel sidebar button by its visible label text.

    Thin wrapper around :func:`click_panel`, retained for backward
    compatibility with existing callers that pass a ``fallback`` ratio
    pair. The fallback is now only used if calibration fails entirely
    — the calibrated path (UIA + QML pixel layout) handles the common
    case correctly across machines.

    Returns True if a click was issued.
    """
    try:
        click_panel(label)
        return True
    except RuntimeError as e:
        log.warning(
            f"  click_panel_button('{label}'): calibration miss ({e})"
        )

    if fallback is not None:
        rx, ry = fallback
        ensure_visible()
        w = get_app_window()
        x = int(w.left + rx * w.width)
        y = int(w.top + ry * w.height)
        banner_offset = BANNER_OFFSET_PX if is_updater_banner_visible() else 0
        if banner_offset:
            log.info(
                f"  click_panel_button('{label}'): update banner visible — "
                f"shifting click y by +{banner_offset}px"
            )
            y += banner_offset
        log.warning(
            f"  click_panel_button('{label}'): falling back to relative "
            f"coords ({rx:.3f}, {ry:.3f}) → ({x}, {y})"
        )
        pyautogui.moveTo(x, y, duration=0.3)
        pyautogui.click(x, y)
        time.sleep(SLEEP)
        return True

    log.warning(f"  click_panel_button('{label}'): no calibration match and no fallback")
    return False


# ─────────────────────────────────────────────
# Modal / dialog handling
# ─────────────────────────────────────────────
# Each modal in the bloodflow app is a QML ``Item`` overlay
# (anchors.fill: parent, visible toggle, z-index) inside the main
# Open-Motion window — not a Qt Dialog or separate Window —
# so they don't surface as distinct UIA windows. We detect which
# modal(s) are open with two passes per fingerprint:
#
#   1. Targeted ``descendants(title=...)`` query for each text
#      fingerprint. Targeted queries hit a different UIA code path
#      than the unfiltered descendants walk and reliably find Text
#      elements that the walk silently misses (same root cause as
#      ``calibrate_panel_buttons`` always falling back to the QML
#      pixel layout). Empirically this is what works for click_by_name
#      against modal contents (e.g. "Visualize BFI/BVI" in History).
#   2. ``descendants(title=..., control_type="Button")`` fallback for
#      modals that expose a uniquely-named Button. Qt Buttons are
#      always exposed via UIA even when sibling Text elements vanish.
#
# Fingerprint values must match the QML *exactly* (case + whitespace)
# because targeted queries are exact matches, not substrings. Each
# entry is (text fingerprints, button fingerprints).
_TextFingerprints = tuple[str, ...]
_ButtonFingerprints = tuple[str, ...]
MODAL_FINGERPRINTS: dict[str, tuple[_TextFingerprints, _ButtonFingerprints]] = {
    # SettingsModal (gear icon)
    "Settings":       (("Time window",), ("Run Calibration", "Check for Updates")),
    # ScanSettingsModal
    "ScanSettings":   (("Scan Settings", "Camera Configuration"), ()),
    # NotesModal
    "Notes":          (("Session Notes",), ()),
    # HistoryModal
    "History":        (("Scan History",), ("Open Folder", "Refresh")),
    # ContactQualityModal — title varies by state_; match all four
    # plus the buttons that only its preScanMode footer renders.
    "ContactQuality": (
        (
            "Checking contact quality\u2026",  # \u2026 = "…"
            "Good signal quality",
            "Contact check failed",
            "Contact Quality Notification",
        ),
        # "Dismiss" / "Retest" appear on multiple modals so they can't
        # be unique fingerprints on their own; "Start Scan" is unique
        # to ContactQualityModal in preScanMode.
        ("Start Scan",),
    ),
}


def visible_modals() -> set[str]:
    """Return the set of modal names currently visible in the app.

    Identifies each modal via the fingerprints in
    ``MODAL_FINGERPRINTS`` using targeted UIA queries (not a tree
    walk — see the comment above the dict for why). Returns names
    like ``"Settings"`` / ``"ContactQuality"``; an empty set means
    no modal is open.

    Used by tests that need to assert mutual exclusivity — see
    ``test_clinicalmode.TestModalExclusivity``.
    """
    found: set[str] = set()
    try:
        win = uia_window()
    except Exception as e:
        log.warning(f"  visible_modals: uia_window failed: {e}")
        return found

    for modal_name, (text_fps, button_fps) in MODAL_FINGERPRINTS.items():
        matched = False
        for fp in text_fps:
            try:
                if win.descendants(title=fp):
                    matched = True
                    break
            except Exception:
                continue
        if matched:
            found.add(modal_name)
            continue
        for fp in button_fps:
            try:
                if win.descendants(title=fp, control_type="Button"):
                    matched = True
                    break
            except Exception:
                continue
        if matched:
            found.add(modal_name)
    return found




# ContactQualityModal title strings for each post-``checking`` state
# (see components/ContactQualityModal.qml line ~271). Stored exactly as
# the QML renders them so targeted ``descendants(title=...)`` queries
# match. Same data also appears in MODAL_FINGERPRINTS["ContactQuality"]
# above; kept inline here so dismiss_signal_quality_modal is
# self-contained and the comment about *why* targeted queries are
# needed stays close to the call.
_CONTACT_QUALITY_POST_CHECKING_TITLES = (
    "Good signal quality",
    "Contact check failed",
    "Contact Quality Notification",
)


def dismiss_signal_quality_modal() -> bool:
    """If the contact-quality modal is past the ``checking`` state,
    click its ``Dismiss`` button.

    Two-pass detection (mirrors ``_find_modal_state`` in
    test_clinicalmode):

      1. **Targeted title query** for each post-``checking`` state
         title. Uses ``descendants(title=...)`` instead of an
         unfiltered tree walk — the walk silently misses plain QML
         Text in this app (same root cause as panel-button calibration
         falling back to the QML pixel layout).
      2. **Button fallback** if no title matched. Any of
         ``Start Scan`` / ``Dismiss`` / ``Retest`` being present
         means the modal is past ``checking`` (the footer renders
         only on ``state_ !== "checking"``); QML Buttons reliably
         surface in UIA even when sibling Text elements don't.

    Without the button fallback this helper returns False even when
    the modal is clearly on screen, which is what test_clinicalmode
    hit + fixed earlier and what test_usb_disconnect_freeze burns
    its 120 s Check polling on.

    Returns True if a Dismiss was clicked, False if the modal wasn't
    detected (still ``checking``, never opened, or already gone).
    """
    try:
        win = uia_window()
    except Exception as e:
        log.warning(f"  dismiss_signal_quality_modal: uia_window failed: {e}")
        return False

    # Pass 1: targeted title query.
    detected_via = None
    for title in _CONTACT_QUALITY_POST_CHECKING_TITLES:
        try:
            if win.descendants(title=title):
                detected_via = f"title={title!r}"
                break
        except Exception:
            continue

    # Pass 2: button fallback. Any post-checking footer button means
    # the modal is up and dismissable.
    if detected_via is None:
        for btn_name in ("Start Scan", "Dismiss", "Retest"):
            try:
                if win.descendants(title=btn_name, control_type="Button"):
                    detected_via = f"button={btn_name!r}"
                    break
            except Exception:
                continue

    if detected_via is None:
        return False

    log.info(f"  Contact-quality modal detected via {detected_via}")
    try:
        hits = win.descendants(title="Dismiss", control_type="Button")
        if hits:
            elem = hits[0]
            rect = elem.rectangle()
            cx = (rect.left + rect.right) // 2
            cy = (rect.top + rect.bottom) // 2
            log.info(f"  Clicking Dismiss button at ({cx}, {cy})")
            pyautogui.click(cx, cy)
            time.sleep(SLEEP)
            return True
    except Exception as e:
        log.warning(f"  Dismiss-button lookup failed: {e}")

    log.warning(
        f"  Modal detected via {detected_via} but Dismiss button not found"
    )
    return False


# ─────────────────────────────────────────────
# Raw histogram CSV payload validation
# ─────────────────────────────────────────────
#
# The SDK ships ``data-processing/check_csv.py`` which scans a raw
# histogram CSV for three integrity violations:
#   * ``bad_sum`` - any row whose ``sum`` column != EXPECTED_SUM
#     (2,457,606 = 1024 bins × 2400 photons/bin baseline)
#   * ``frame_id_skipped`` - non-monotonic frame_id sequencing modulo 256
#   * ``bad_frame_cam_count`` - frames missing one or more camera rows
#
# The SDK module's directory has a dash (``data-processing``), so it
# isn't importable as a package; load it by file path. The function
# itself prints results and returns ``None``, so we capture stdout and
# look for the literal ``[PASS]`` it emits on success. We surface the
# captured output on failure so the operator sees exactly which row(s)
# tripped which check.

_SDK_CHECK_CSV_PATH = Path(
    os.environ.get(
        "OPENWATER_CHECK_CSV",
        Path(__file__).resolve().parent.parent.parent
        / "openmotion-sdk" / "data-processing" / "check_csv.py",
    )
)


_CHECK_CSV_COUNT_RE = re.compile(
    r"^\s+(bad_sum|frame_id_skipped|bad_frame_cam_count):\s+(\d+)\s*$",
    re.MULTILINE,
)


def validate_raw_csv_payload(csv_path: Path) -> tuple[dict[str, int], str]:
    """Run the SDK's ``check_csv_integrity`` against ``csv_path``.

    Returns ``(counts, captured_stdout)`` where ``counts`` is the
    per-error-class tally the SDK printed:

        {"bad_sum": int, "frame_id_skipped": int, "bad_frame_cam_count": int}

    The caller decides what's a hard fail vs an acceptable artifact —
    e.g. ``bad_frame_cam_count == 1`` is normal at the truncation
    boundary (SDK closes the raw CSV mid-frame, leaving one partial
    frame with fewer cam rows), so the *raw-CSV* test treats that as
    OK while still demanding ``bad_sum == 0`` and
    ``frame_id_skipped == 0``. Setup/IO failures surface as
    ``counts == {}`` with the failure message in the second slot.
    """
    import importlib.util
    import io
    import contextlib

    if not _SDK_CHECK_CSV_PATH.exists():
        return {}, (
            f"SDK check_csv.py not found at {_SDK_CHECK_CSV_PATH}. "
            f"Set OPENWATER_CHECK_CSV to its absolute path, or check the "
            f"openmotion-sdk repo is alongside openmotion-bloodflow-app."
        )

    spec = importlib.util.spec_from_file_location(
        "openwater_sdk_check_csv", str(_SDK_CHECK_CSV_PATH)
    )
    if spec is None or spec.loader is None:
        return {}, f"Could not load module spec from {_SDK_CHECK_CSV_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "check_csv_integrity"):
        return {}, (
            f"{_SDK_CHECK_CSV_PATH} loaded but has no "
            f"``check_csv_integrity`` symbol — SDK API changed?"
        )

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            mod.check_csv_integrity(str(csv_path))
        except Exception as e:
            return {}, f"{type(e).__name__}: {e}\n{buf.getvalue()}"
    out = buf.getvalue()
    counts = {m.group(1): int(m.group(2)) for m in _CHECK_CSV_COUNT_RE.finditer(out)}
    return counts, out
