"""Unit tests for the append-only audit log (audit_log.py)."""
import csv
import json
import sqlite3
import threading

import pytest

from audit_log import AuditLog, gather_host_info, EV_SYSTEM_STARTUP

pytestmark = pytest.mark.unit


def test_log_creates_table_and_inserts_row(tmp_path):
    db = str(tmp_path / "scans.db")
    a = AuditLog(db)
    a.log(EV_SYSTEM_STARTUP, {"app_version": "1.2.3"})
    a.close()

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT event_type, details FROM logs"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == EV_SYSTEM_STARTUP
    assert json.loads(rows[0][1]) == {"app_version": "1.2.3"}


def test_reopen_is_idempotent(tmp_path):
    db = str(tmp_path / "scans.db")
    AuditLog(db).close()
    a = AuditLog(db)          # second open must not raise on CREATE
    a.log("system_info", None)
    a.close()


def test_none_details_stored_as_null(tmp_path):
    db = str(tmp_path / "scans.db")
    a = AuditLog(db)
    a.log("system_shutdown", None)
    a.close()
    conn = sqlite3.connect(db)
    val = conn.execute("SELECT details FROM logs").fetchone()[0]
    conn.close()
    assert val is None


def test_query_is_newest_first_and_honors_limit(tmp_path):
    a = AuditLog(str(tmp_path / "scans.db"))
    for i in range(5):
        a.log("scan_started", {"n": i})
    out = a.query(limit=3)
    a.close()
    assert len(out) == 3
    # newest (n=4) first
    assert json.loads(out[0]["details"])["n"] == 4


def test_export_csv_roundtrips(tmp_path):
    a = AuditLog(str(tmp_path / "scans.db"))
    a.log("scan_started", {"label": "owABC"})
    a.log("scan_ended", {"label": "owABC"})
    dest = tmp_path / "audit.csv"
    n = a.export_csv(str(dest))
    a.close()
    assert n == 2
    with open(dest, newline="", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
    assert reader[0]["event_type"] == "scan_started"   # oldest first
    assert json.loads(reader[0]["details"])["label"] == "owABC"


def test_no_path_is_silent_noop(tmp_path):
    a = AuditLog(None)
    assert a.enabled is False
    a.log("scan_started", {"x": 1})          # must not raise
    assert a.query() == []
    assert a.export_csv(str(tmp_path / "x.csv")) == 0
    a.close()


def test_concurrent_writes_all_land(tmp_path):
    a = AuditLog(str(tmp_path / "scans.db"))

    def worker():
        for _ in range(20):
            a.log("scan_started", {"t": 1})

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(a.query(limit=1000)) == 60
    a.close()


def test_gather_host_info_has_core_fields():
    info = gather_host_info()
    assert "hostname" in info
    assert "platform" in info
    assert "python" in info
