"""Connector glue for critical errors: signal payload + bug-report routing."""

from unittest.mock import MagicMock

import pytest

import error_codes
import motion_connector
from motion_connector import MotionConnector

pytestmark = pytest.mark.unit


def _connector(tmp_path, app_config=None, connected=(True, True, True)):
    iface = MagicMock()
    iface.is_device_connected.return_value = connected
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = None
    cfg = {"engineeringMode": False}
    if app_config:
        cfg.update(app_config)
    return MotionConnector(
        interface=iface, app_config=cfg,
        data_dir=str(tmp_path), config_dir="config",
    )


_FULL_SMTP = {
    "host": "smtp.example.com", "port": 587, "username": "u",
    "password": "p", "from_addr": "app@example.com",
}


def test_raise_critical_emits_registry_payload(tmp_path):
    conn = _connector(tmp_path)
    received = []
    conn.criticalErrorRaised.connect(lambda *a: received.append(a))

    conn._raise_critical("E-101", detail="mux missing")

    assert len(received) == 1
    code, title, message, action, detail = received[0]
    entry = error_codes.lookup("E-101")
    assert code == "E-101"
    assert title == entry.title
    assert message == entry.message
    assert action == entry.suggested_action
    assert detail == "mux missing"


def test_raise_critical_unknown_code_still_emits(tmp_path):
    conn = _connector(tmp_path)
    received = []
    conn.criticalErrorRaised.connect(lambda *a: received.append(a))

    conn._raise_critical("E-777")

    assert len(received) == 1
    assert received[0][0] == "E-777"
    assert received[0][1]  # generic title, non-empty


def _healthy():
    return {"mux": True, "imu": True, "cameras": [True] * 8,
            "fpgas": [True] * 8, "all_present": True}


def test_i2c_health_all_present_raises_nothing(tmp_path):
    conn = _connector(tmp_path)
    conn._interface.left.is_connected.return_value = True
    conn._interface.left.i2c_health = _healthy()
    received = []
    conn.criticalErrorRaised.connect(lambda *a: received.append(a))

    conn._check_sensor_i2c_health("left")

    assert received == []


def test_i2c_health_missing_device_raises_e101_with_detail(tmp_path):
    conn = _connector(tmp_path)
    conn._interface.left.is_connected.return_value = True
    snap = _healthy()
    snap["cameras"] = [True, True, False, True, True, True, True, True]
    snap["imu"] = False
    snap["all_present"] = False
    conn._interface.left.i2c_health = snap
    received = []
    conn.criticalErrorRaised.connect(lambda *a: received.append(a))

    conn._check_sensor_i2c_health("left")

    assert len(received) == 1
    code, _title, _msg, _action, detail = received[0]
    assert code == "E-101"
    assert "imu" in detail
    assert "2" in detail  # camera index 2 missing


def test_i2c_health_none_raises_e102(tmp_path):
    conn = _connector(tmp_path)
    conn._interface.left.is_connected.return_value = True
    conn._interface.left.i2c_health = None
    received = []
    conn.criticalErrorRaised.connect(lambda *a: received.append(a))

    conn._check_sensor_i2c_health("left")

    assert len(received) == 1
    assert received[0][0] == "E-102"


def _notifs(conn):
    """Capture toast payloads emitted via notificationRequested."""
    out = []
    conn.notificationRequested.connect(lambda p: out.append(p))
    return out


def test_connector_connection_timeout_defaults_to_twelve_seconds(tmp_path):
    """Guards the fallback at motion_connector.py's
    `cfg.get("connectionTimeoutSec", 12)`: this applies whenever a caller
    constructs MotionConnector with a partial config dict that omits the
    key — including `_connector()` here, whose default app_config doesn't
    set it. Every other watchdog test calls `_check_connection_watchdog()`
    directly and never reads `_connection_timeout_sec`, so a regression of
    this fallback back to 30 would pass all of them silently."""
    conn = _connector(tmp_path)

    assert conn._connection_timeout_sec == 12


def test_watchdog_all_connected_no_notification(tmp_path):
    conn = _connector(tmp_path, connected=(True, True, True))
    notifs = _notifs(conn)

    conn._check_connection_watchdog()

    assert notifs == []


def test_watchdog_is_a_warning_not_a_critical_modal(tmp_path):
    """E-104/E-106 are downgraded: a yellow warning toast, never the modal."""
    conn = _connector(tmp_path, connected=(False, False, False))
    crit = []
    conn.criticalErrorRaised.connect(lambda *a: crit.append(a))
    notifs = _notifs(conn)

    conn._check_connection_watchdog()

    assert crit == []
    assert all(n["type"] == "warning" for n in notifs)


def test_watchdog_both_missing_shows_single_system_not_found(tmp_path):
    conn = _connector(tmp_path, connected=(False, False, False))
    notifs = _notifs(conn)

    conn._check_connection_watchdog()

    assert len(notifs) == 1
    assert notifs[0]["type"] == "warning"
    assert "System not found" in notifs[0]["text"]


def test_watchdog_console_only_missing_warns(tmp_path):
    conn = _connector(tmp_path, connected=(False, True, False))
    notifs = _notifs(conn)

    conn._check_connection_watchdog()

    assert len(notifs) == 1
    assert notifs[0]["type"] == "warning"
    assert "Console" in notifs[0]["text"]
    assert "System not found" not in notifs[0]["text"]


def test_watchdog_sensor_only_missing_warns(tmp_path):
    conn = _connector(tmp_path, connected=(True, False, False))
    notifs = _notifs(conn)

    conn._check_connection_watchdog()

    assert len(notifs) == 1
    assert notifs[0]["type"] == "warning"
    assert "Sensor" in notifs[0]["text"]


def test_watchdog_toast_auto_dismisses_after_10s(tmp_path):
    """The connection warning auto-dismisses after 10 s (#314) rather than
    staying sticky, so it doesn't nag while the user explores the no-device
    sample scan. Still user-dismissible via the close button."""
    conn = _connector(tmp_path, connected=(False, False, False))
    notifs = _notifs(conn)

    conn._check_connection_watchdog()

    assert len(notifs) == 1
    assert notifs[0]["durationMs"] == 10000
    assert notifs[0]["dismissible"] is True


def test_watchdog_respects_min_sensors_config(tmp_path):
    conn = _connector(tmp_path, app_config={"minSensors": 2},
                      connected=(True, True, False))  # only one sensor
    notifs = _notifs(conn)

    conn._check_connection_watchdog()

    assert len(notifs) == 1
    assert notifs[0]["type"] == "warning"
    assert "Sensor" in notifs[0]["text"]


# ── Startup watchdog vs a device that is still connecting (#667) ─────────


class _Timers:
    """Stands in for motion_connector.QTimer: records singleShot calls
    instead of arming real timers, so a test fires them explicitly."""

    def __init__(self):
        self.shots = []

    def singleShot(self, ms, fn):
        self.shots.append((ms, fn))


def _timers(monkeypatch):
    # Patched after the connector is built: its constructor makes QTimer
    # instances, which the stand-in does not provide.
    timers = _Timers()
    monkeypatch.setattr(motion_connector, "QTimer", timers)
    return timers


def _sdk_state(conn, name, state):
    """The SDK's own state change, before its queued event reaches the
    connector: the GUI thread is busy, e.g. in the console's connect-time
    setup."""
    getattr(conn._interface, name).state = state


def _state_event(conn, name, old, new, reason="poll_arrived"):
    """A state change the connector has handled. The SDK sets its state
    before it emits, so the live state already reads ``new``."""
    _sdk_state(conn, name, new)
    handle = MagicMock()
    handle.name = name
    conn._on_handle_state_changed_impl(handle, old, new, reason)


def _watchdog_rechecks(timers):
    return [fn for ms, fn in timers.shots
            if ms == motion_connector._CONNECTION_WATCHDOG_CONNECTING_GRACE_SEC * 1000]


def _dismissed_tags(conn):
    out = []
    conn.notificationDismissByTagRequested.connect(lambda t: out.append(t))
    return out


def test_watchdog_defers_while_sensor_connecting_qa_sequence(tmp_path, monkeypatch):
    """The QA log behind #667: console up, the right sensor went
    DISCONNECTED -> CONNECTING 0.4 s before the deadline and reached
    CONNECTED 1.7 s after it. No E-106 toast at all."""
    from omotion import ConnectionState as S
    conn = _connector(tmp_path, connected=(True, False, False))
    timers = _timers(monkeypatch)
    notifs = _notifs(conn)

    _state_event(conn, "right", S.DISCONNECTED, S.CONNECTING)
    conn._check_connection_watchdog()

    assert notifs == []
    rechecks = _watchdog_rechecks(timers)
    assert len(rechecks) == 1

    _state_event(conn, "right", S.CONNECTING, S.CONNECTED)
    rechecks[0]()

    assert [n for n in notifs if n["tag"] == "connection-watchdog"] == []
    assert len(_watchdog_rechecks(timers)) == 1  # the re-check is final


def test_watchdog_defers_on_sdk_connecting_before_its_event_arrives(
        tmp_path, monkeypatch):
    """The 1.5.4-dev.4 QA failure (#667): the console's connect-time setup
    held the GUI thread ~3.7 s, the left sensor went CONNECTING in the SDK
    inside that window, and the overdue deadline ran before the queued
    CONNECTING event. The connector's own flags still said "missing"; the
    SDK's live state says "connecting", so the check waits."""
    from omotion import ConnectionState as S
    conn = _connector(tmp_path, connected=(True, False, False))
    timers = _timers(monkeypatch)
    notifs = _notifs(conn)

    _sdk_state(conn, "left", S.CONNECTING)
    conn._check_connection_watchdog()

    assert notifs == []
    rechecks = _watchdog_rechecks(timers)
    assert len(rechecks) == 1

    _state_event(conn, "left", S.DISCONNECTED, S.CONNECTING)
    _state_event(conn, "left", S.CONNECTING, S.CONNECTED, "ping_ok")
    rechecks[0]()

    assert [n for n in notifs if n["tag"] == "connection-watchdog"] == []


def test_watchdog_counts_sdk_connected_before_its_event_arrives(
        tmp_path, monkeypatch):
    """A sensor the SDK already reports CONNECTED is not missing, even with
    its CONNECTED event still queued: no warning and no re-check."""
    from omotion import ConnectionState as S
    conn = _connector(tmp_path, connected=(True, False, False))
    timers = _timers(monkeypatch)
    notifs = _notifs(conn)

    _sdk_state(conn, "right", S.CONNECTED)
    conn._check_connection_watchdog()

    assert notifs == []
    assert _watchdog_rechecks(timers) == []


def test_watchdog_recheck_reports_sensor_that_failed_to_connect(
        tmp_path, monkeypatch):
    from omotion import ConnectionState as S
    conn = _connector(tmp_path, connected=(True, False, False))
    timers = _timers(monkeypatch)
    notifs = _notifs(conn)

    _state_event(conn, "right", S.DISCONNECTED, S.CONNECTING)
    conn._check_connection_watchdog()
    _state_event(conn, "right", S.CONNECTING, S.DISCONNECTED, "connect_failed")
    _watchdog_rechecks(timers)[0]()

    assert len(notifs) == 1
    assert "Sensor not detected" in notifs[0]["text"]
    assert notifs[0]["tag"] == "connection-watchdog"


def test_watchdog_recheck_reports_sensor_still_connecting(tmp_path, monkeypatch):
    """A sensor that keeps failing its handshake spends most of its time in
    CONNECTING. The re-check is final: it reports it instead of waiting
    again, or such a sensor would never be reported."""
    from omotion import ConnectionState as S
    conn = _connector(tmp_path, connected=(True, False, False))
    timers = _timers(monkeypatch)
    notifs = _notifs(conn)

    _state_event(conn, "right", S.DISCONNECTED, S.CONNECTING)
    conn._check_connection_watchdog()
    _state_event(conn, "right", S.CONNECTING, S.DISCONNECTED, "connect_failed")
    _state_event(conn, "right", S.DISCONNECTED, S.CONNECTING)
    _watchdog_rechecks(timers)[0]()

    assert len(notifs) == 1
    assert "Sensor not detected" in notifs[0]["text"]
    assert len(_watchdog_rechecks(timers)) == 1


def test_watchdog_defers_while_console_connecting(tmp_path, monkeypatch):
    from omotion import ConnectionState as S
    conn = _connector(tmp_path, connected=(False, True, False))
    timers = _timers(monkeypatch)
    notifs = _notifs(conn)

    _state_event(conn, "console", S.DISCONNECTED, S.CONNECTING)
    conn._check_connection_watchdog()

    assert notifs == []
    assert len(_watchdog_rechecks(timers)) == 1


def test_watchdog_ignores_connecting_device_it_does_not_need(
        tmp_path, monkeypatch):
    """Only a device the warning is about can defer it: a sensor that is
    connecting does not hold back a missing-console warning."""
    from omotion import ConnectionState as S
    conn = _connector(tmp_path, connected=(False, True, False))
    timers = _timers(monkeypatch)
    notifs = _notifs(conn)

    _state_event(conn, "right", S.DISCONNECTED, S.CONNECTING)
    conn._check_connection_watchdog()

    assert len(notifs) == 1
    assert "Console not detected" in notifs[0]["text"]
    assert _watchdog_rechecks(timers) == []


def test_watchdog_toast_withdrawn_when_missing_sensor_connects(
        tmp_path, monkeypatch):
    from omotion import ConnectionState as S
    conn = _connector(tmp_path, connected=(True, False, False))
    _timers(monkeypatch)
    dismissed = _dismissed_tags(conn)

    conn._check_connection_watchdog()
    assert "connection-watchdog" not in dismissed

    _state_event(conn, "left", S.DISCONNECTED, S.CONNECTING)
    assert "connection-watchdog" not in dismissed
    _state_event(conn, "left", S.CONNECTING, S.CONNECTED)

    assert dismissed.count("connection-watchdog") == 1

    # A later reconnect has no watchdog toast to take down.
    _state_event(conn, "left", S.CONNECTED, S.DISCONNECTED, "unplugged")
    _state_event(conn, "left", S.DISCONNECTED, S.CONNECTED)
    assert dismissed.count("connection-watchdog") == 1


def test_watchdog_system_not_found_toast_stays_until_sensor_connects(
        tmp_path, monkeypatch):
    from omotion import ConnectionState as S
    conn = _connector(tmp_path, connected=(False, False, False))
    _timers(monkeypatch)
    dismissed = _dismissed_tags(conn)

    conn._check_connection_watchdog()
    _state_event(conn, "console", S.DISCONNECTED, S.CONNECTED, "found")
    assert "connection-watchdog" not in dismissed

    _state_event(conn, "right", S.DISCONNECTED, S.CONNECTED, "found")
    assert dismissed.count("connection-watchdog") == 1


def test_connect_without_watchdog_toast_dismisses_nothing(tmp_path, monkeypatch):
    from omotion import ConnectionState as S
    conn = _connector(tmp_path, connected=(True, False, False))
    _timers(monkeypatch)
    dismissed = _dismissed_tags(conn)

    _state_event(conn, "left", S.DISCONNECTED, S.CONNECTED, "found")

    assert "connection-watchdog" not in dismissed


def test_send_bug_report_selects_smtp_when_configured(tmp_path, monkeypatch):
    conn = _connector(tmp_path, app_config={"bug_report_smtp": _FULL_SMTP})
    chosen = []
    monkeypatch.setattr(conn, "_send_bug_report_smtp",
                        lambda *a, **k: chosen.append("smtp"))
    monkeypatch.setattr(conn, "_send_bug_report_fallback",
                        lambda *a, **k: chosen.append("fallback"))

    conn.sendBugReport("E-101")

    assert chosen == ["smtp"]


def test_send_bug_report_falls_back_without_smtp(tmp_path, monkeypatch):
    conn = _connector(tmp_path)  # no bug_report_smtp configured
    chosen = []
    monkeypatch.setattr(conn, "_send_bug_report_smtp",
                        lambda *a, **k: chosen.append("smtp"))
    monkeypatch.setattr(conn, "_send_bug_report_fallback",
                        lambda *a, **k: chosen.append("fallback"))

    conn.sendBugReport("E-101")

    assert chosen == ["fallback"]
