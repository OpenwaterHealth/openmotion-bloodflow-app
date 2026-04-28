"""
Shared test utilities for the OpenWater BloodFlow UI test suite.

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
  - **Modal handling**: ``close_plot_window``, ``dismiss_signal_quality_modal``.

``SENSOR_OPTIONS`` is the canonical sensor-dropdown ordering used by both
the scan-settings and scan-auto-stop tests; keep it in sync with the QML
``SensorComboBox`` model if that list ever changes.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

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

# Slot index for each top-of-panel button (counted from Start = 0). The
# History and Settings buttons sit below a fillHeight spacer at the
# bottom of the panel and need a different computation; they're not in
# this map.
PANEL_BUTTON_SLOTS = {
    "Start":         0,
    "Scan\nSettings": 1,
    "Notes":          2,
    "Check":          3,
}


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
        home / "Documents" / "OpenWater Bloodflow",
        home / "Documents" / "OpenMotion",
    ]
    candidates: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        candidates.extend(root.glob("app-logs/ow-bloodflowapp-*.log"))
        candidates.extend(root.glob("**/app-logs/ow-bloodflowapp-*.log"))
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
    """Compute absolute screen (x, y) for a top-of-panel sidebar button.

    Pixel-based, derived from the QML layout. Tri-state banner handling:

      - banner detected as **visible**  → shift y by ``BANNER_OFFSET_PX``
        (36 px) so we hit the button center exactly.
      - banner detected as **hidden**   → no shift; hit center exactly.
      - **uncertain** (Qt accessibility silently drops the banner texts)
        → shift y by ``BANNER_OFFSET_PX // 2`` (18 px). This lands in the
        32-px overlap of the no-banner and with-banner hitboxes (button
        is 68 px tall, banner shift is 36 px, so the two possible button
        positions overlap by 68 − 36 = 32 px). The click hits either way.

    Returns ``None`` if the label isn't in the top-of-panel slot map
    (e.g. ``"History"``, which sits below a flex spacer).
    """
    if label not in PANEL_BUTTON_SLOTS:
        return None
    slot = PANEL_BUTTON_SLOTS[label]
    w = get_app_window()

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

    x = w.left + _PANEL_OUTER_LEFT + _PANEL_INNER_LEFT + _PANEL_CONTENT_MARG + _PANEL_BUTTON_HALF
    y = (w.top + header_px + _PANEL_INNER_TOP + _PANEL_CONTENT_MARG
         + _PANEL_BUTTON_HALF + slot * _PANEL_SLOT_PX + y_fudge)
    return x, y


def click_panel_button(label: str, fallback: tuple[float, float] | None = None) -> bool:
    """Click a ButtonPanel sidebar button by its visible label text.

    The buttons in ``components/ButtonPanel.qml`` are MouseArea-driven and
    don't expose accessible names directly, but each button renders its
    label as a ``Text`` element (e.g. ``"Scan\\nSettings"``, ``"Notes"``,
    ``"Check"``). The label is centered within the 68×68 button via
    ``ColumnLayout { anchors.centerIn: parent }``, so clicking the label's
    center lands inside the ``MouseArea`` that fills the same 68×68 item.

    Searches:
      1. UIA ``descendants(title=label)`` — exact match including newlines.
      2. Same with ``\\n`` → space, in case UIA normalises whitespace.
      3. First-line of label (``"Scan"`` for ``"Scan\\nSettings"``).

    If all three fail and ``fallback`` is supplied, click those relative
    window coords as a last resort. Returns True if any click was issued.
    """
    ensure_visible()
    win = uia_window()
    for variant in (label, label.replace("\n", " "), label.split("\n")[0]):
        try:
            matches = win.descendants(title=variant)
        except Exception:
            matches = []
        if matches:
            elem = matches[0]
            rect = elem.rectangle()
            cx = (rect.left + rect.right) // 2
            cy = (rect.top + rect.bottom) // 2
            log.info(
                f"  click_panel_button('{label}'): UIA match on '{variant}' "
                f"→ ({cx}, {cy})"
            )
            pyautogui.moveTo(cx, cy, duration=0.3)
            pyautogui.click(cx, cy)
            time.sleep(SLEEP)
            return True

    # UIA didn't find the label (Qt accessibility doesn't expose plain
    # Text inside MouseArea-driven buttons). Use the QML pixel layout
    # instead — window-size independent and banner-aware.
    pos = panel_button_screen_pos(label)
    if pos is not None:
        x, y = pos
        state = _detect_banner_state()
        if state is True:
            banner_note = " (banner detected → +36 px)"
        elif state is False:
            banner_note = " (no banner)"
        else:
            banner_note = " (banner state uncertain → +18 px overlap fudge)"
        log.info(
            f"  click_panel_button('{label}'): QML pixel layout → ({x}, {y})"
            f"{banner_note}"
        )
        pyautogui.moveTo(x, y, duration=0.3)
        pyautogui.click(x, y)
        time.sleep(SLEEP)
        return True

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
            f"  click_panel_button('{label}'): UIA lookup failed, falling "
            f"back to coords ({rx:.3f}, {ry:.3f}) → ({x}, {y})"
        )
        pyautogui.moveTo(x, y, duration=0.3)
        pyautogui.click(x, y)
        time.sleep(SLEEP)
        return True

    log.warning(f"  click_panel_button('{label}'): no UIA match and no fallback")
    return False


# ─────────────────────────────────────────────
# Modal / dialog handling
# ─────────────────────────────────────────────
def close_plot_window() -> bool:
    """Close the plot window opened by the app via Alt+F4.

    Iterates all top-level windows and closes the first one whose title
    does not match the main app keywords, avoiding closing the main app.
    """
    for w in gw.getAllWindows():
        if not w.title.strip():
            continue
        if any(k in w.title.lower() for k in APP_KEYWORDS):
            continue
        try:
            if w.isMinimized:
                w.restore()
                time.sleep(1)
            w.activate()
            time.sleep(0.5)
            log.info(f"  Closing plot window: '{w.title}'")
            pyautogui.hotkey("alt", "f4")
            time.sleep(SLEEP)
            return True
        except Exception as e:
            log.warning(f"  Could not close '{w.title}': {e}")
    log.warning("  No plot window found to close")
    return False


def dismiss_signal_quality_modal() -> bool:
    """If the 'Good signal quality' modal appears, click Dismiss.

    Returns True if a Dismiss was clicked, False if the modal wasn't found.
    """
    try:
        win = uia_window()
        signal_modal_found = False
        for elem in win.descendants():
            try:
                text = elem.window_text().strip().lower()
                if "good signal quality" in text or "signal quality" in text:
                    signal_modal_found = True
                    break
            except Exception:
                continue

        if not signal_modal_found:
            return False

        log.info("  Signal quality modal detected — looking for Dismiss button")
        for elem in win.descendants():
            try:
                if elem.window_text().strip() == "Dismiss":
                    rect = elem.rectangle()
                    cx = (rect.left + rect.right) // 2
                    cy = (rect.top + rect.bottom) // 2
                    log.info(f"  Clicking Dismiss button at ({cx}, {cy})")
                    pyautogui.click(cx, cy)
                    time.sleep(SLEEP)
                    return True
            except Exception:
                continue
        log.warning("  'Good signal quality' detected but Dismiss button not found")
    except Exception as e:
        log.warning(f"  dismiss_signal_quality_modal failed: {e}")
    return False
