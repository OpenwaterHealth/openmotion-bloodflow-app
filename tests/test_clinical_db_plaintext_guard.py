"""
test_clinical_db_plaintext_guard.py — a Clinical build never creates or opens a
plaintext scans.db (openmotion-bloodflow-app#683, fixed by #690 merge
76fd655, shipped from 1.5.4-dev.2).

Fresh install: the SDK creates scans.db with SQLCipher (key in the Windows
Credential Manager), so the file does not start with the plaintext SQLite
header "SQLite format 3\\0". Plaintext scans.db left in a Clinical data
folder (a pre-1.5.0 clinical portable folder, or a Research folder reused
by a Clinical build): startup raises the E-108 critical modal "Scan storage
inaccessible", the SDK refuses to open the file, and Start is refused with
E-301. The file is never read into or rewritten.

The plaintext case runs on a throwaway copy of the Clinical exe in its own
portable folder under %TEMP%, so no real data folder is touched.

Steps:
  1. Fresh install: every Clinical data folder on the bench (the running
     Clinical build, the Clinical Engineering Tool, any other Clinical
     portable folder found) holds an encrypted scans.db; a Research
     scans.db is plaintext (control).
  2. A copy of the Clinical exe is launched against a data folder holding
     a small plaintext scans.db: "CRITICAL E-108" is logged and the
     critical modal is up (Dismiss / Copy details).
  3. Dismiss, then Start (clinical contact-quality gate → "Start Scan"):
     the scan is refused with "CRITICAL E-301"; no "Full scan started".
  4. The plaintext scans.db is byte-identical afterwards; the copy is
     closed and the normal build relaunched.

Preconditions
- The Clinical 1.5.4-dev.x portable build on disk (taken from the running
  process, or set CLINICAL_EXE); console + sensors on USB, phantom in place.

Marked ``release``: the clinical gate's 1 s contact-quality check fires
the laser briefly; ~2 min.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

import psutil
import pyautogui
import pytest

from conftest import SLEEP, ensure_visible, log, uia_window
from console_bench import close_app
from hil_helpers import (
    calibrate_panel_buttons,
    click_element_center,
    click_panel,
    move_window_on_screen,
    wait_for_pattern,
)

pytestmark = [pytest.mark.release, pytest.mark.sdk_direct]

SQLITE_HEADER = b"SQLite format 3\x00"
RE_E108 = re.compile(r"CRITICAL E-108")
RE_E301 = re.compile(r"CRITICAL E-301")
RE_SCAN_STARTED = re.compile(r"Full scan started")
RE_VARIANT = re.compile(r"Build variant:\s+(.+?)\s*$", re.M)
RE_CONNECTED = re.compile(r"Handle console -> CONNECTED")
MODAL_BUTTONS = ("Dismiss", "Contact Support", "Copy details")
DOCS = Path.home() / "Documents" / "OpenMotion"

STATE: dict = {}


def _is_encrypted(db: Path) -> bool:
    with db.open("rb") as f:
        return f.read(16) != SQLITE_HEADER


def _variant_of(exe_dir: Path) -> str:
    logs = sorted((exe_dir / "logs").glob("open-motion-*.log"), key=lambda p: p.stat().st_mtime)
    for lp in reversed(logs):
        for line in lp.read_bytes().decode("utf-8", errors="replace").splitlines()[:80]:
            m = RE_VARIANT.search(line)
            if m:
                return m.group(1).strip()
    return ""


def _buttons() -> list[str]:
    try:
        return [(b.window_text() or "").strip() for b in uia_window().descendants(control_type="Button")]
    except Exception:
        return []


def _click_button(name: str, timeout: float = 10.0) -> bool:
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


@pytest.fixture(scope="class")
def clinical_copy(app):
    """A throwaway portable copy of the Clinical exe with a plaintext
    data/scans.db; closes it and relaunches the original build after."""
    original = ""
    for p in psutil.process_iter(["name", "exe"]):
        if (p.info.get("name") or "").lower() == "open-motion.exe" and p.info.get("exe"):
            original = p.info["exe"]
    clinical = os.environ.get("CLINICAL_EXE", "")
    if not clinical:
        cands = [e for e in DOCS.rglob("Open-Motion.exe")
                 if "Research" not in str(e) and "Service" not in str(e) and "Engineering" not in str(e)
                 and _variant_of(e.parent) == "Clinical"]
        clinical = str(max(cands, key=lambda p: p.stat().st_mtime)) if cands else ""
    if not clinical:
        pytest.skip("no Clinical Open-Motion.exe found (set CLINICAL_EXE)")
    root = Path(tempfile.mkdtemp(prefix="clinical683_")) / "Open-Motion"
    (root / "data").mkdir(parents=True)
    shutil.copy2(clinical, root / "Open-Motion.exe")
    db = root / "data" / "scans.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, session_label TEXT)")
    con.execute("INSERT INTO sessions (session_label) VALUES ('plaintext-683')")
    con.commit()
    con.close()
    STATE.update(root=root, db=db, db_hash=hashlib.sha256(db.read_bytes()).hexdigest(),
                 clinical=clinical, original=original)
    log.info(f"  Clinical exe {clinical} copied to {root}; plaintext scans.db {db.stat().st_size} bytes")
    close_app()
    try:
        yield root
    finally:
        close_app()
        if original:
            subprocess.Popen([original], cwd=str(Path(original).parent))
            log.info(f"  relaunched {original}")


@pytest.mark.incremental
class TestClinicalDbEncryption:
    """Clinical never runs on a plaintext scans.db."""

    def test_01_fresh_clinical_dbs_are_encrypted(self, app):
        """Every Clinical data folder on the bench holds an encrypted scans.db; Research's is plaintext."""
        rows = []
        for exe in DOCS.rglob("Open-Motion.exe"):
            db = exe.parent / "data" / "scans.db"
            if not db.exists() or db.stat().st_size == 0:
                continue
            variant = _variant_of(exe.parent)
            if not variant:
                continue
            rows.append((variant, _is_encrypted(db), db))
        for v, enc, db in rows:
            log.info(f"  {v:<55} {'encrypted' if enc else 'PLAINTEXT'}  {db}")
        clinical = [r for r in rows if r[0].startswith("Clinical")]
        research = [r for r in rows if r[0].startswith("Research")]
        assert clinical, "no Clinical data folder with a scans.db found"
        assert all(enc for _, enc, _ in clinical), "a Clinical scans.db is plaintext"
        assert research and not any(enc for _, enc, _ in research), "control failed: Research scans.db not plaintext"

    def test_02_plaintext_db_raises_e108(self, clinical_copy):
        """Clinical copy launched on a plaintext scans.db: CRITICAL E-108 logged, critical modal up."""
        root = clinical_copy
        subprocess.Popen([str(root / "Open-Motion.exe")], cwd=str(root))
        deadline = time.time() + 60
        lp = None
        while time.time() < deadline and lp is None:
            logs = sorted((root / "logs").glob("open-motion-*.log")) if (root / "logs").exists() else []
            lp = logs[-1] if logs else None
            time.sleep(0.5)
        assert lp, "the Clinical copy wrote no log"
        STATE["log"] = lp
        e108 = wait_for_pattern(RE_E108, lp, 0, 40)
        text = lp.read_bytes().decode("utf-8", errors="replace")
        variant = RE_VARIANT.search(text)
        log.info(f"  variant: {variant.group(1) if variant else '?'}; {e108.strip()[24:220] if e108 else 'no E-108'}")
        assert variant and variant.group(1).strip() == "Clinical", "the copy is not a Clinical build"
        assert e108, "no CRITICAL E-108 within 40 s of launching on a plaintext scans.db"
        deadline = time.time() + 30
        while time.time() < deadline and not ensure_visible():
            time.sleep(1)
        move_window_on_screen()
        time.sleep(2)
        names = _buttons()
        log.info(f"  buttons with the modal up: {names}")
        assert "Dismiss" in names, f"critical modal not shown: {names}"

    def test_03_start_refused_e301(self, clinical_copy):
        """Dismiss, then Start → contact-quality gate → Start Scan: CRITICAL E-301, no scan started."""
        lp = STATE["log"]
        assert _click_button("Dismiss"), "Dismiss not found"
        assert wait_for_pattern(RE_CONNECTED, lp, 0, 60), "console never CONNECTED in the copy"
        time.sleep(3)
        calibrate_panel_buttons()
        off = lp.stat().st_size
        click_panel("Start")
        deadline = time.time() + 120
        clicked_gate = False
        e301 = None
        while time.time() < deadline and not e301:
            e301 = wait_for_pattern(RE_E301, lp, off, 2)
            if not e301 and not clicked_gate and _click_button("Start Scan", timeout=0.5):
                log.info("  contact-quality gate: clicked 'Start Scan'")
                clicked_gate = True
        tail = lp.read_bytes()[off:].decode("utf-8", errors="replace")
        log.info(f"  {e301.strip()[24:220] if e301 else 'no E-301'}")
        assert e301, "Start was not refused with E-301 within 120 s"
        assert not RE_SCAN_STARTED.search(tail), "a scan started on the plaintext database"
        _click_button("Dismiss", timeout=5)

    def test_04_plaintext_db_untouched(self, clinical_copy):
        """The plaintext scans.db is byte-identical after the session."""
        close_app()
        h = hashlib.sha256(STATE["db"].read_bytes()).hexdigest()
        log.info(f"  scans.db sha256 before {STATE['db_hash'][:16]}…, after {h[:16]}…")
        assert h == STATE["db_hash"], "the plaintext scans.db was modified"
