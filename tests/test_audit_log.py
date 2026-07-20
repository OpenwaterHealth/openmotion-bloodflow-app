"""Unit tests for the append-only audit log (audit_log.py)."""
import csv
import datetime
import json
import sqlite3
import threading

import pytest

from audit_log import (
    AuditLog,
    gather_host_info,
    parse_date_bound,
    EV_SYSTEM_STARTUP,
)

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
    assert len(a.query()) == 1
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


def test_count_returns_total_rows(tmp_path):
    a = AuditLog(str(tmp_path / "scans.db"))
    for _ in range(7):
        a.log("scan_started", {"x": 1})
    assert a.count() == 7
    a.close()
    assert a.count() == 0          # no-op after close


def test_gather_host_info_has_core_fields():
    info = gather_host_info()
    assert "hostname" in info
    assert "platform" in info
    assert "python" in info


def test_log_and_query_after_close_do_not_raise(tmp_path):
    a = AuditLog(str(tmp_path / "scans.db"))
    a.log("scan_started", {"x": 1})
    a.close()
    # After close the instance must stay fail-soft.
    a.log("scan_started", {"x": 2})   # must not raise
    assert a.query() == []
    assert a.export_csv(str(tmp_path / "after_close.csv")) == 0


def test_empty_dict_details_is_serialized_not_null(tmp_path):
    db = str(tmp_path / "scans.db")
    a = AuditLog(db)
    a.log("scan_ended", {})
    a.close()
    import sqlite3 as _sql
    conn = _sql.connect(db)
    val = conn.execute("SELECT details FROM logs").fetchone()[0]
    conn.close()
    assert val == "{}"


# ── Query filters (#226) ────────────────────────────────────────────────

def test_query_filters_by_event_type(tmp_path):
    a = AuditLog(str(tmp_path / "scans.db"))
    a.log("scan_started", {"n": 1})
    a.log("scan_ended", {"n": 2})
    a.log("scan_started", {"n": 3})
    out = a.query(event_type="scan_started")
    a.close()
    assert [e["event_type"] for e in out] == ["scan_started"] * 2
    # still newest first
    assert [json.loads(e["details"])["n"] for e in out] == [3, 1]


def test_query_filters_by_epoch_range(tmp_path, monkeypatch):
    import audit_log as mod
    # Modern epochs — Windows can't .astimezone() near-1970 timestamps.
    t0 = 1_750_000_000.0
    a = AuditLog(str(tmp_path / "scans.db"))
    for ts, n in ((t0 + 100, 1), (t0 + 200, 2), (t0 + 300, 3)):
        monkeypatch.setattr(mod.time, "time", lambda ts=ts: ts)
        a.log("scan_started", {"n": n})
    # since is inclusive
    out = a.query(since_epoch=t0 + 200)
    assert [json.loads(e["details"])["n"] for e in out] == [3, 2]
    # until is exclusive (a whole-day upper bound is next-day midnight)
    out = a.query(until_epoch=t0 + 200)
    assert [json.loads(e["details"])["n"] for e in out] == [1]
    # combined range
    out = a.query(since_epoch=t0 + 150, until_epoch=t0 + 250)
    assert [json.loads(e["details"])["n"] for e in out] == [2]
    a.close()


def test_query_text_search_matches_details_case_insensitive(tmp_path):
    a = AuditLog(str(tmp_path / "scans.db"))
    a.log("scan_started", {"label": "owABC"})
    a.log("scan_started", {"label": "other"})
    out = a.query(contains="owabc")
    a.close()
    assert len(out) == 1
    assert json.loads(out[0]["details"])["label"] == "owABC"


def test_query_text_search_matches_event_type(tmp_path):
    a = AuditLog(str(tmp_path / "scans.db"))
    a.log("calibration_started", None)
    a.log("scan_started", None)
    out = a.query(contains="calib")
    a.close()
    assert len(out) == 1
    assert out[0]["event_type"] == "calibration_started"


def test_query_text_search_escapes_like_wildcards(tmp_path):
    # A "%" in the needle must match literally, not as a LIKE wildcard.
    a = AuditLog(str(tmp_path / "scans.db"))
    a.log("scan_started", {"progress": "50% done"})
    a.log("scan_started", {"progress": "plain"})
    out = a.query(contains="%")
    assert len(out) == 1
    assert "50%" in out[0]["details"]
    # Same for "_" (would otherwise match any single character).
    out = a.query(contains="50_")
    a.close()
    assert out == []


def test_query_combined_filters_honor_limit(tmp_path):
    a = AuditLog(str(tmp_path / "scans.db"))
    for i in range(5):
        a.log("scan_started", {"n": i})
        a.log("scan_ended", {"n": i})
    out = a.query(limit=3, event_type="scan_started")
    a.close()
    # limit applies to the FILTERED rows, newest first
    assert [json.loads(e["details"])["n"] for e in out] == [4, 3, 2]


def test_query_filters_when_disabled_return_empty(tmp_path):
    a = AuditLog(None)
    assert a.query(event_type="scan_started", contains="x") == []
    a.close()


def test_distinct_event_types_sorted_unique(tmp_path):
    a = AuditLog(str(tmp_path / "scans.db"))
    a.log("scan_started", None)
    a.log("scan_ended", None)
    a.log("scan_started", None)
    a.log("calibration_started", None)
    assert a.distinct_event_types() == [
        "calibration_started", "scan_ended", "scan_started",
    ]
    a.close()


def test_distinct_event_types_disabled_returns_empty():
    a = AuditLog(None)
    assert a.distinct_event_types() == []
    a.close()


# ── parse_date_bound (#226) ─────────────────────────────────────────────

def test_parse_date_bound_start_is_local_midnight():
    want = datetime.datetime(2026, 6, 15).timestamp()
    assert parse_date_bound("2026-06-15") == want


def test_parse_date_bound_end_is_next_day_midnight():
    want = datetime.datetime(2026, 6, 16).timestamp()
    assert parse_date_bound("2026-06-15", end=True) == want


def test_parse_date_bound_tolerates_whitespace():
    want = datetime.datetime(2026, 6, 15).timestamp()
    assert parse_date_bound("  2026-06-15  ") == want


@pytest.mark.parametrize("bad", [None, "", "garbage", "2026-13-40",
                                 "15/06/2026", "2026-06"])
def test_parse_date_bound_invalid_returns_none(bad):
    assert parse_date_bound(bad) is None
    assert parse_date_bound(bad, end=True) is None
