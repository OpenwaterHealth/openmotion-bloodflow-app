"""
test_delete_audit_no_password.py — scan deletion and the audit log need no
password in any build: Delete asks only "are you sure", View Logs opens
directly, and both stay audited (openmotion-bloodflow-app#703, shipped in
#707 commit f3a5959, 1.5.4-dev.4; also covers #455 for Research).

The test makes its own throwaway scan and deletes only that one, so no
real data is touched:

Steps:
  1. A ~8 s throwaway scan is started and stopped (clinical gate
     handled); its session id and label come from the app log; the
     post-scan Notes modal is closed.
  2. History opens; the newest row (the throwaway) is selected; Delete
     opens the plain confirm — Cancel + Delete, no password prompt
     button, no new text input.
  3. Confirming deletes it: "deleteScans: removed session <id>" is
     logged and History lists one scan fewer.
  4. Settings → View Logs opens the audit log directly (its "Export CSV"
     button appears, no password prompt button).
  5. The audit log is exported through the native Save dialog; the CSV
     holds a scan_deleted row for the throwaway's session id and an
     audit_log_viewed row, both written during this test. Reading the
     export works on both variants (the clinical scans.db is encrypted).

Preconditions
- App running (either variant), console + a sensor, phantom in place.

Marked ``release``: runs a short laser scan and deletes it, ~2 min.
"""

from __future__ import annotations

import csv
import json
import re
import time
from datetime import datetime
from pathlib import Path

import pyautogui
import pytest

from conftest import SLEEP, log, require_focus, uia_window
from hil_helpers import (
    click_element_center,
    click_panel,
    find_app_log,
    log_size,
    move_window_on_screen,
    wait_for_pattern,
)

pytestmark = pytest.mark.release

RE_SCAN_STARTED = re.compile(r"Full scan started: subject=(\S+)")
RE_SCAN_ENDED = re.compile(r"Full scan ended")
RE_DB_SESSION = re.compile(r"Scan notes saved to DB session '([^']+)'")
RE_HISTORY_OPENED = re.compile(r"\[History\] opened\D+(\d+) scan")
RE_REMOVED = re.compile(r"deleteScans: removed session (\d+)")
RE_AUDIT_EXPORTED = re.compile(r"exportAuditLogCsv: wrote (\d+) rows -> (.+?)\s*$")
PASSWORD_PROMPT_BUTTONS = ("Unlock",)
THROWAWAY_SCAN_S = 8

STATE: dict = {}


# ─────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────
def _log_since(off: int) -> str:
    with Path(STATE["log"]).open("rb") as f:
        f.seek(off)
        return f.read().decode("utf-8", errors="replace")


def _buttons() -> list:
    try:
        return list(uia_window().descendants(control_type="Button"))
    except Exception as e:
        log.warning(f"  UIA button scan failed: {e}")
        return []


def _button_names() -> list[str]:
    return [(b.window_text() or "").strip() for b in _buttons()]


def _click_button(name: str, timeout: float = 10.0, exact: bool = True) -> str:
    """Pixel-click a UIA Button by name (QML Buttons ignore Invoke)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for b in _buttons():
            n = (b.window_text() or "").strip()
            if (n == name) if exact else (name in n):
                click_element_center(b, n)
                time.sleep(SLEEP)
                return n
        time.sleep(0.5)
    return ""


def _activate_button(name: str, timeout: float = 10.0) -> bool:
    """Give a Button keyboard focus through UIA and press Space. Used for
    buttons inside scrolled QML panels (Settings): Qt keeps reporting
    their pre-scroll position to UIA, so a pixel click would miss."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for b in _buttons():
            if (b.window_text() or "").strip() == name:
                b.set_focus()
                time.sleep(0.3)
                pyautogui.press("space")
                time.sleep(SLEEP)
                return True
        time.sleep(0.5)
    return False


def _edit_count() -> int:
    try:
        return len(uia_window().descendants(control_type="Edit"))
    except Exception:
        return -1


def _press_start_and_wait_for_scan(start_off: int, timeout: float = 240.0) -> str:
    click_panel("Start")
    deadline = time.time() + timeout
    clicked_gate = False
    while time.time() < deadline:
        line = wait_for_pattern(RE_SCAN_STARTED, STATE["log"], start_off, 2)
        if line:
            return line
        if not clicked_gate and _click_button("Start Scan", timeout=0.5):
            log.info("  contact-quality gate: clicked 'Start Scan'")
            clicked_gate = True
    pytest.fail(f"no 'Full scan started' line within {timeout:.0f} s of clicking Start")


def _open_history(off: int) -> int:
    click_panel("History")
    line = wait_for_pattern(RE_HISTORY_OPENED, STATE["log"], off, 10)
    assert line, "no '[History] opened' line within 10 s of clicking History"
    time.sleep(2)
    return int(RE_HISTORY_OPENED.search(line).group(1))


def _select_newest_history_row() -> None:
    """Rows are plain Text; locate the first one from the search Edit
    (34 px logical high, first row 73 logical px below it)."""
    edits = [e.rectangle() for e in uia_window().descendants(control_type="Edit")]
    assert edits, "History search field not exposed over UIA; cannot locate rows"
    search = min(edits, key=lambda r: r.top)
    scale = (search.bottom - search.top) / 34.0
    pyautogui.click(int(search.left + 10 * scale), int(search.bottom + 73 * scale))
    time.sleep(1.5)


def _accept_save_dialog(title: str, timeout: float = 15.0) -> bool:
    """Accept the native Save dialog (Win32 #32770) with its default name;
    answer Yes to an overwrite prompt."""
    from pywinauto import Desktop
    win32 = Desktop(backend="win32")

    def _find(rx):
        try:
            for w in win32.windows(class_name="#32770"):
                if rx.match(w.window_text() or ""):
                    return w
        except Exception:
            pass
        return None

    def _press(dlg, names, fallback):
        try:
            dlg.set_focus()
            time.sleep(0.3)
            for b in dlg.descendants():
                if (b.window_text() or "").replace("&", "").strip() in names:
                    b.click_input()
                    return
        except Exception:
            pass
        fallback()

    rx = re.compile(rf"(?i)^{re.escape(title)}$")
    dlg, deadline = None, time.time() + timeout
    while dlg is None and time.time() < deadline:
        dlg = _find(rx)
        time.sleep(0.3)
    if dlg is None:
        return False
    _press(dlg, ("Save",), lambda: pyautogui.press("enter"))
    rx_confirm = re.compile(r"(?i)^Confirm Save As$")
    deadline = time.time() + 6
    while time.time() < deadline:
        p = _find(rx_confirm)
        if p is not None:
            _press(p, ("Yes",), lambda: pyautogui.hotkey("alt", "y"))
            break
        time.sleep(0.4)
    return True


# ─────────────────────────────────────────────
# tests
# ─────────────────────────────────────────────
@pytest.mark.incremental
class TestDeleteAuditNoPassword:
    """Delete = plain confirm; View Logs = no prompt; both audited."""

    def test_01_throwaway_scan(self, app):
        """A short scan is recorded so the test deletes only its own data."""
        move_window_on_screen()
        require_focus()
        lp = find_app_log()
        assert lp, "app log not found"
        STATE["log"] = lp
        STATE["t_start"] = time.time()
        off = log_size(lp)
        line = _press_start_and_wait_for_scan(off)
        STATE["subject"] = RE_SCAN_STARTED.search(line).group(1)
        time.sleep(THROWAWAY_SCAN_S)
        click_panel("Start")                      # Start toggles to Stop
        assert wait_for_pattern(RE_SCAN_ENDED, lp, off, 60), "scan did not end within 60 s of Stop"
        db = wait_for_pattern(RE_DB_SESSION, lp, off, 30)
        assert db, "no 'Scan notes saved to DB session' line for the throwaway scan"
        STATE["label"] = RE_DB_SESSION.search(db).group(1)
        time.sleep(2)
        pyautogui.press("escape")                 # post-scan Notes modal
        time.sleep(SLEEP)
        log.info(f"  throwaway scan {STATE['label']} (subject {STATE['subject']})")
        assert STATE["subject"] in STATE["label"], "session label does not carry the scan's subject"

    def test_02_delete_opens_plain_confirm(self, app):
        """Newest History row selected; Delete shows Cancel/Delete only — no password prompt, no new text input."""
        off = log_size(STATE["log"])
        STATE["n_before"] = _open_history(off)
        _select_newest_history_row()
        edits_before = _edit_count()
        footer = _click_button("Delete", exact=False)
        assert "(1)" in footer, f"row not selected — footer Delete reads {footer!r}"
        time.sleep(1)
        names = _button_names()
        log.info(f"  buttons with the prompt open: {names}; edits {_edit_count()} (before {edits_before})")
        assert "Cancel" in names and "Delete" in names, f"confirm prompt not open: {names}"
        assert not any(n in PASSWORD_PROMPT_BUTTONS for n in names), f"a password prompt opened: {names}"
        assert _edit_count() == edits_before, "a text input appeared with the prompt (password field)"

    def test_03_confirm_deletes_the_throwaway(self, app):
        """Confirming deletes it: 'deleteScans: removed session <id>' and History lists one scan fewer."""
        off = log_size(STATE["log"])
        assert _click_button("Delete", exact=True), "confirm prompt's Delete button not found"
        line = wait_for_pattern(RE_REMOVED, STATE["log"], off, 10)
        assert line, "no 'deleteScans: removed session' line after confirming"
        STATE["session_id"] = int(RE_REMOVED.search(line).group(1))
        log.info(f"  deleted session id {STATE['session_id']}")
        click_panel("History")                    # close
        time.sleep(SLEEP)
        n_after = _open_history(log_size(STATE["log"]))
        click_panel("History")
        time.sleep(SLEEP)
        log.info(f"  History: {STATE['n_before']} scan(s) before, {n_after} after")
        assert n_after == STATE["n_before"] - 1, f"History count {STATE['n_before']} -> {n_after}, expected -1"

    def test_04_view_logs_opens_without_prompt(self, app):
        """Settings → View Logs opens the audit log directly (Export CSV visible, no password prompt)."""
        click_panel("Settings")
        time.sleep(1.5)
        assert _activate_button("View Logs"), "View Logs button not found in Settings"
        time.sleep(1.5)
        names = _button_names()
        log.info(f"  buttons after View Logs: {names}")
        assert "Export CSV" in names, f"audit log did not open (no 'Export CSV' button): {names}"
        assert not any(n in PASSWORD_PROMPT_BUTTONS for n in names), f"a password prompt opened: {names}"

    def test_05_audit_export_has_both_events(self, app):
        """The exported audit CSV holds scan_deleted for the throwaway and audit_log_viewed, both from this test."""
        off = log_size(STATE["log"])
        assert _activate_button("Export CSV"), "audit log Export CSV button not found"
        assert _accept_save_dialog("Export Audit Log CSV"), "the 'Export Audit Log CSV' dialog did not appear"
        line = wait_for_pattern(RE_AUDIT_EXPORTED, STATE["log"], off, 15)
        assert line, "no 'exportAuditLogCsv: wrote N rows' line"
        path = Path(RE_AUDIT_EXPORTED.search(line).group(2))
        log.info(f"  audit log exported: {line.strip()[-120:]}")
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        recent = [r for r in rows if float(r.get("ts_epoch") or 0) >= STATE["t_start"] - 1]
        deleted = [r for r in recent if r["event_type"] == "scan_deleted"]
        viewed = [r for r in recent if r["event_type"] == "audit_log_viewed"]
        for r in deleted + viewed:
            log.info(f"  {r['ts_iso']}  {r['event_type']}  {r['details'][:120]}")
        assert any(STATE["session_id"] in (json.loads(r["details"] or "{}").get("session_ids") or [])
                   and json.loads(r["details"] or "{}").get("count") == 1 for r in deleted), (
            f"no scan_deleted row for session {STATE['session_id']} with count 1"
        )
        assert viewed, "no audit_log_viewed row written by opening View Logs"
        pyautogui.press("escape")                 # close the audit log
        time.sleep(SLEEP)
