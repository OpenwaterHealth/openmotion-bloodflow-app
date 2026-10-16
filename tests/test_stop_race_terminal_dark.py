"""
test_stop_race_terminal_dark.py — a Stop in the first ~2 s of a scan no longer
races the scan's own trigger start (openmotion-bloodflow-app#726, fixed in
openmotion-sdk#318 / PR #320, SDK 1.13.0, ships in 1.5.4-rc.0).

Before the fix, a Stop that landed after camera setup but before the worker's
start_trigger let the trigger start anyway: the laser kept firing ~1-1.5 s
after Stop, cancel_scan read a lit frame as the terminal dark, every masked
camera logged ``ERROR TERMINAL DARK CONTAMINATED`` (or MISSING) plus an
integrity event, the log warned "Scan completed with the trigger clock still
open", and the session was deleted as empty with nothing shown to the operator.

With the fix: Stop during pre-flight prevents the trigger from starting, and a
Stop that races start_trigger is ordered against it. Ethan's check: press Stop
within ~2 s of Start; the laser stops at Stop; no TERMINAL DARK
CONTAMINATED/MISSING errors; no "trigger clock still open" warning; the next
contact-quality check on those cameras is clean.

Steps (Clinical or Research, console + sensors on the phantom):
  1. App connected, sidebar calibrated.
  2. STOP_DELAYS_S scans: click Start (and the Clinical 'Start Scan' gate),
     click Stop N s after the 'startCapture(' line, read the log slice for
     that scan: 'Full scan ended', TERMINAL DARK lines, the trigger-clock
     warning, CRITICAL lines, the terminal fsync count, session deletion.
     Fails if any scan logged a TERMINAL DARK error or the trigger-clock
     warning.
  3. A pre-scan Check after the sweep reaches its OK state (cameras healthy).
Run with OPENWATER_EXE pointing at the build under test.
"""
from __future__ import annotations

import re
import time

import pytest

from conftest import log, require_focus
from hil_helpers import find_app_log, log_size, move_window_on_screen, recalibrate_panel_buttons, click_panel
from test_notes_empty_session_toast import (RE_SCAN_ENDED, RE_SESSION_DELETED, _click_button,
                                            _start_and_stop_at_once)

STOP_DELAYS_S = (0.3, 0.6, 0.9, 1.2, 1.5, 1.8, 2.1, 2.4)   # after the 'startCapture(' line
RE_TERMINAL_DARK = re.compile(r"TERMINAL DARK (CONTAMINATED|MISSING)[^\n]*")
RE_CLOCK_OPEN = re.compile(r"trigger clock still open[^\n]*")
RE_CRITICAL = re.compile(r"CRITICAL E-\d{3}[^\n]*")
RE_FSYNC = re.compile(r"terminal fsync count reported: (\d+)")
RE_INTEGRITY = re.compile(r"integrity event: TerminalDarkResult[^\n]*")
RE_TRIGGER = re.compile(r"^(\S+ \S+) - \w+ - \S+ - ([^\n]*(?:[Tt]rigger|cancel)[^\n]*)$", re.M)
STATE: dict = {}


@pytest.mark.incremental
class TestStopRaceTerminalDark:

    def test_01_app_connected(self, app):
        move_window_on_screen()
        require_focus()
        pos = recalibrate_panel_buttons()
        log.info(f"  sidebar recalibrated: Start at {pos.get('Start')}")
        log_path = find_app_log()
        assert log_path, "app log not found"
        text = log_path.read_text(encoding="utf-8", errors="replace")
        assert "Handle console -> CONNECTED" in text, "console not CONNECTED"
        sides = [s for s in ("left", "right") if f"Handle {s} -> CONNECTED" in text]
        assert sides, "no sensor CONNECTED"
        sdk = re.search(r"SDK:\s+(\S+)", text)
        log.info(f"  log {log_path.name}; sensors {sides}; SDK {sdk.group(1) if sdk else '?'}")
        STATE["log"] = log_path

    def test_02_stop_within_two_seconds_sweep(self, app):
        log_path = STATE["log"]
        rows = []
        for delay in STOP_DELAYS_S:
            off = log_size(log_path)
            t0 = time.time()
            how, duration, deleted = _start_and_stop_at_once(log_path, delay)
            time.sleep(3)
            text = log_path.read_text(encoding="utf-8", errors="replace")[off:]
            dark = RE_TERMINAL_DARK.findall(text)
            clock = RE_CLOCK_OPEN.findall(text)
            crit = RE_CRITICAL.findall(text)
            fsync = RE_FSYNC.findall(text)
            integ = RE_INTEGRITY.findall(text)
            trig = [m.group(2)[:110] for m in RE_TRIGGER.finditer(text)]
            row = dict(delay=delay, how=how, duration=duration, deleted=deleted, dark=len(dark), clock=len(clock),
                       critical=crit, fsync=fsync, integrity=len(integ))
            rows.append(row)
            log.info(f"  Stop {delay:.1f} s after startCapture: scan {how} after {duration}; terminal-dark errors {len(dark)}, "
                     f"integrity events {len(integ)}, clock-open warnings {len(clock)}, critical {crit or 'none'}, "
                     f"fsync {fsync}, session deleted as empty: {deleted}")
            for l in trig[:8]:
                log.info(f"      {l}")
            time.sleep(4)        # let the app settle before the next Start
        STATE["rows"] = rows
        bad = [r for r in rows if r["dark"] or r["clock"]]
        assert not bad, ("TERMINAL DARK / trigger-clock lines after an early Stop: "
                         + "; ".join(f"delay {r['delay']} s: dark={r['dark']} clock={r['clock']}" for r in bad))
        assert not any(r["critical"] for r in rows), f"CRITICAL during the sweep: {[r['critical'] for r in rows if r['critical']]}"

    def test_03_pre_scan_check_clean_afterwards(self, app):
        """The cameras that were in the early-stopped scans come through the next contact-quality check."""
        log_path = STATE["log"]
        off = log_size(log_path)
        click_panel("Check")
        deadline = time.time() + 90
        state = None
        while time.time() < deadline:
            from conftest import uia_window  # noqa: PLC0415
            from test_clinicalmode import _find_modal_state  # noqa: PLC0415
            try:
                state = _find_modal_state(uia_window())
            except Exception:
                state = None
            if state in ("ok", "error", "warnings"):
                break
            time.sleep(0.5)
        text = log_path.read_text(encoding="utf-8", errors="replace")[off:]
        crit = RE_CRITICAL.findall(text)
        log.info(f"  contact-quality modal state after the sweep: {state}; critical {crit or 'none'}")
        _click_button("Dismiss", timeout=3) or _click_button("Close", timeout=1)
        assert state == "ok", f"contact-quality check ended in state {state!r} after the early-stop sweep"
        assert not crit, f"CRITICAL during the check: {crit}"
