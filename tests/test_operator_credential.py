"""Operator credential for scan deletion and the audit log (#703).

The check runs in Python, so calling deleteScans() or the audit-log slots
directly (bypassing the QML prompt) is refused too. Windows' LogonUserW is
stubbed: these tests never submit a real credential.
"""
import json
import os
import sys
from unittest.mock import MagicMock

import pytest

import motion_connector
from motion_connector import _ENGINEERING_PASSWORD, MotionConnector
from utils import operator_auth

pytestmark = pytest.mark.unit

GOOD = "right-password"
ACCOUNT = "HOST\\alice"


@pytest.fixture
def windows_auth(monkeypatch):
    """Stub the OS check: GOOD is the logged-in account's password."""
    calls = []

    def _verify(pw):
        calls.append(pw)
        return pw == GOOD

    monkeypatch.setattr(operator_auth, "supported", lambda: True)
    monkeypatch.setattr(operator_auth, "verify_password", _verify)
    monkeypatch.setattr(operator_auth, "current_account", lambda: ACCOUNT)
    return calls


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(motion_connector, "_grant_clock", lambda: now[0])
    return now


def _connector(tmp_path):
    iface = MagicMock()
    iface.is_device_connected.return_value = (False, False, False)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = str(tmp_path / "scans.db")
    iface.get_sdk_version.return_value = "9.9.9"
    return MotionConnector(
        interface=iface,
        app_config={"engineeringMode": False},
        data_dir=str(tmp_path),
        config_dir="config",
    )


def _events(c, event_type):
    return [json.loads(e["details"] or "{}") for e in c._audit.query(limit=1000)
            if e["event_type"] == event_type]


def _session(tmp_path, label="L1"):
    from omotion.ScanDatabase import ScanDatabase
    db = ScanDatabase(db_path=str(tmp_path / "scans.db"))
    try:
        return db.create_session(session_label=label, session_start=1.0,
                                 session_meta={})
    finally:
        db.close()


def _session_ids(tmp_path):
    from omotion.ScanDatabase import ScanDatabase
    db = ScanDatabase(db_path=str(tmp_path / "scans.db"))
    try:
        return [s["id"] for s in db.iter_sessions()]
    finally:
        db.close()


# ── authorizeOperator ───────────────────────────────────────────────────

def test_wrong_password_is_refused_and_audited(tmp_path, windows_auth):
    c = _connector(tmp_path)
    assert c.authorizeOperator("wrong", "delete") is False
    assert _events(c, "operator_auth_failed") == [
        {"operator": ACCOUNT, "scope": "delete"}]
    assert c._operator_grants == {}


def test_right_password_grants_and_is_audited(tmp_path, windows_auth):
    c = _connector(tmp_path)
    assert c.authorizeOperator(GOOD, "audit") is True
    assert _events(c, "operator_authenticated") == [
        {"operator": ACCOUNT, "scope": "audit"}]


def test_unknown_scope_never_reaches_the_check(tmp_path, windows_auth):
    c = _connector(tmp_path)
    assert c.authorizeOperator(GOOD, "calibrate") is False
    assert windows_auth == []


def test_the_shared_password_is_not_an_operator_credential(tmp_path, windows_auth):
    c = _connector(tmp_path)
    assert c.authorizeOperator(_ENGINEERING_PASSWORD, "delete") is False


def test_without_the_os_check_the_engineering_password_is_used(tmp_path, monkeypatch):
    # macOS (Research-only) has no OS check here; it keeps the old prompt.
    monkeypatch.setattr(operator_auth, "supported", lambda: False)
    c = _connector(tmp_path)
    assert c.operatorUsesWindowsAccount() is False
    assert c.authorizeOperator("wrong", "delete") is False
    assert c.authorizeOperator(_ENGINEERING_PASSWORD, "delete") is True


# ── deleteScans ─────────────────────────────────────────────────────────

def test_direct_delete_without_the_credential_is_refused(tmp_path, windows_auth):
    sid = _session(tmp_path)
    c = _connector(tmp_path)

    assert c.deleteScans([sid]) == 0

    assert _session_ids(tmp_path) == [sid]
    assert _events(c, "scan_delete_refused")
    assert _events(c, "scan_deleted") == []


def test_delete_with_the_credential_records_the_operator(tmp_path, windows_auth):
    sid = _session(tmp_path)
    c = _connector(tmp_path)

    assert c.authorizeOperator(GOOD, "delete") is True
    assert c.deleteScans([sid]) == 1

    assert _session_ids(tmp_path) == []
    assert _events(c, "scan_deleted") == [
        {"session_ids": [sid], "count": 1, "operator": ACCOUNT}]


def test_a_delete_grant_covers_one_delete(tmp_path, windows_auth):
    a, b = _session(tmp_path, "A"), _session(tmp_path, "B")
    c = _connector(tmp_path)

    c.authorizeOperator(GOOD, "delete")
    assert c.deleteScans([a]) == 1
    assert c.deleteScans([b]) == 0
    assert _session_ids(tmp_path) == [b]


def test_a_delete_grant_expires(tmp_path, windows_auth, clock):
    sid = _session(tmp_path)
    c = _connector(tmp_path)

    c.authorizeOperator(GOOD, "delete")
    clock[0] += motion_connector._OPERATOR_GRANT_TTL_S["delete"] + 1
    assert c.deleteScans([sid]) == 0
    assert _session_ids(tmp_path) == [sid]


def test_an_audit_grant_does_not_allow_deletion(tmp_path, windows_auth):
    sid = _session(tmp_path)
    c = _connector(tmp_path)

    c.authorizeOperator(GOOD, "audit")
    assert c.deleteScans([sid]) == 0


# ── audit log ───────────────────────────────────────────────────────────

def test_audit_reads_are_empty_without_the_credential(tmp_path, windows_auth):
    c = _connector(tmp_path)
    assert c._audit.query()                       # startup events exist
    assert c.auditLogEntries() == []
    assert c.filteredAuditLogEntries({}) == []
    assert c.auditEventTypes() == []
    c.recordAuditLogViewed()
    assert _events(c, "audit_log_viewed") == []


def test_direct_export_without_the_credential_is_refused(tmp_path, windows_auth):
    c = _connector(tmp_path)
    dest = str(tmp_path / "audit.csv")

    assert c.exportAuditLogCsv(dest) == ""

    assert not os.path.exists(dest)
    assert _events(c, "audit_log_export_refused")


def test_audit_access_with_the_credential_records_the_operator(tmp_path, windows_auth):
    c = _connector(tmp_path)
    dest = str(tmp_path / "audit.csv")

    assert c.authorizeOperator(GOOD, "audit") is True
    c.recordAuditLogViewed()
    assert c.filteredAuditLogEntries({})
    assert c.auditEventTypes()
    assert c.exportAuditLogCsv(dest) == dest

    assert os.path.exists(dest)
    assert _events(c, "audit_log_viewed")[0]["operator"] == ACCOUNT
    assert _events(c, "audit_log_exported")[0]["operator"] == ACCOUNT


def test_closing_the_logs_modal_ends_audit_access(tmp_path, windows_auth):
    c = _connector(tmp_path)
    c.authorizeOperator(GOOD, "audit")
    c.endAuditLogSession()
    assert c.filteredAuditLogEntries({}) == []
    assert c.exportAuditLogCsv(str(tmp_path / "audit.csv")) == ""


def test_audit_access_expires_when_idle_and_slides_with_use(tmp_path, windows_auth, clock):
    c = _connector(tmp_path)
    ttl = motion_connector._OPERATOR_GRANT_TTL_S["audit"]
    c.authorizeOperator(GOOD, "audit")

    clock[0] += ttl - 1
    assert c.filteredAuditLogEntries({})          # still open; slides
    clock[0] += ttl - 1
    assert c.filteredAuditLogEntries({})          # still open
    clock[0] += ttl + 1
    assert c.filteredAuditLogEntries({}) == []    # idle too long


# ── utils.operator_auth ─────────────────────────────────────────────────

def test_verify_password_splits_the_account_for_logon(monkeypatch):
    seen = []
    monkeypatch.setattr(operator_auth, "supported", lambda: True)
    monkeypatch.setattr(operator_auth, "_windows_account", lambda: "HOST\\alice")
    monkeypatch.setattr(operator_auth, "_logon",
                        lambda u, d, p: seen.append((u, d, p)) or True)

    assert operator_auth.verify_password("pw") is True
    assert seen == [("alice", "HOST", "pw")]


@pytest.mark.parametrize("pw", ["", None, 123])
def test_verify_password_rejects_without_calling_windows(monkeypatch, pw):
    seen = []
    monkeypatch.setattr(operator_auth, "supported", lambda: True)
    monkeypatch.setattr(operator_auth, "_windows_account", lambda: "HOST\\alice")
    monkeypatch.setattr(operator_auth, "_logon", lambda *a: seen.append(a) or True)

    assert operator_auth.verify_password(pw) is False
    assert seen == []


def test_verify_password_is_false_off_windows(monkeypatch):
    monkeypatch.setattr(operator_auth, "supported", lambda: False)
    assert operator_auth.verify_password("pw") is False


def test_verify_password_never_raises(monkeypatch):
    def _boom(*_a):
        raise OSError("advapi32 unavailable")
    monkeypatch.setattr(operator_auth, "supported", lambda: True)
    monkeypatch.setattr(operator_auth, "_windows_account", lambda: "HOST\\alice")
    monkeypatch.setattr(operator_auth, "_logon", _boom)
    assert operator_auth.verify_password("pw") is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows account lookup")
def test_current_account_names_the_windows_user():
    account = operator_auth.current_account()
    assert "\\" in account and account != "unknown"
