"""Unit tests for the engineering Logs window's file seam.

components/LogViewerWindow.qml polls ``MotionConnector.readAppLog()`` on a
GUI-thread QTimer; these tests cover the Python side it wraps
(``utils/log_tail.py`` + the two connector slots): incremental offsets,
the bounded first read, truncation recovery, shared-read behavior against
a writer that keeps the file open (main.py's logging FileHandler), and
the latest-log fallback glob. No app-config keys are involved, so no
FORCE_APP_CONFIG here (the engineeringMode gate is QML-side only).
"""

from unittest.mock import MagicMock

import pytest

from utils.log_tail import LogTail, latest_log_path

pytestmark = pytest.mark.unit


def _write(path, text):
    path.write_text(text, encoding="utf-8")


# ── LogTail.read_new ─────────────────────────────────────────────────────

def test_first_read_returns_existing_content(tmp_path):
    log = tmp_path / "open-motion-x.log"
    _write(log, "line 1\nline 2\n")

    assert LogTail(log).read_new() == "line 1\nline 2\n"


def test_subsequent_reads_return_only_appended(tmp_path):
    log = tmp_path / "open-motion-x.log"
    _write(log, "line 1\n")
    tail = LogTail(log)
    assert tail.read_new() == "line 1\n"

    with open(log, "a", encoding="utf-8") as f:
        f.write("line 2\n")

    assert tail.read_new() == "line 2\n"
    assert tail.read_new() == ""  # nothing new


def test_missing_file_returns_empty_then_recovers(tmp_path):
    log = tmp_path / "open-motion-x.log"
    tail = LogTail(log)
    assert tail.read_new() == ""

    _write(log, "late arrival\n")
    assert tail.read_new() == "late arrival\n"


def test_truncation_restarts_from_top(tmp_path):
    log = tmp_path / "open-motion-x.log"
    _write(log, "old old old old\n")
    tail = LogTail(log)
    tail.read_new()

    _write(log, "fresh\n")  # rewritten shorter than the old offset

    assert tail.read_new() == "fresh\n"


def test_first_read_is_capped_and_starts_on_a_line_boundary(tmp_path):
    log = tmp_path / "open-motion-x.log"
    full = "".join(f"row {i:04d}\n" for i in range(100))
    _write(log, full)

    text = LogTail(log, max_initial_bytes=100).read_new()

    assert 0 < len(text) <= 100
    assert full.endswith(text)                    # a true suffix...
    assert full[-len(text) - 1] == "\n"           # ...starting after a \n
    assert text.startswith("row ")                # no partial first line


def test_poll_cap_drains_backlog_across_calls(tmp_path):
    log = tmp_path / "open-motion-x.log"
    _write(log, "seed\n")
    tail = LogTail(log, max_poll_bytes=16)
    tail.read_new()

    appended = "".join(f"bulk line {i}\n" for i in range(10))
    with open(log, "a", encoding="utf-8") as f:
        f.write(appended)

    got = ""
    for _ in range(100):
        chunk = tail.read_new()
        if not chunk:
            break
        assert len(chunk.encode("utf-8")) <= 17  # poll cap (+1 carried "\r")
        got += chunk
    assert got == appended


def test_shared_read_while_writer_holds_the_file_open(tmp_path):
    # main.py's FileHandler keeps the log open for writing for the whole
    # run; reading must neither fail nor lock the writer out (Windows).
    log = tmp_path / "open-motion-x.log"
    tail = LogTail(log)
    with open(log, "w", encoding="utf-8") as writer:
        writer.write("while open 1\n")
        writer.flush()
        assert tail.read_new() == "while open 1\n"

        writer.write("while open 2\n")
        writer.flush()
        assert tail.read_new() == "while open 2\n"


# ── latest_log_path ──────────────────────────────────────────────────────

def test_latest_log_path_picks_newest_by_timestamp_name(tmp_path):
    _write(tmp_path / "open-motion-20260101_090000.log", "old")
    _write(tmp_path / "open-motion-20260102_120000.log", "new")
    _write(tmp_path / "open-motion-20260102_080000.log", "mid")
    _write(tmp_path / "unrelated.log", "ignore me")

    assert latest_log_path(tmp_path).endswith(
        "open-motion-20260102_120000.log")


def test_latest_log_path_empty_or_missing_dir(tmp_path):
    assert latest_log_path(tmp_path) == ""
    assert latest_log_path(tmp_path / "nope") == ""


# ── Connector slots (appLogPath / readAppLog) ────────────────────────────

def _connector(tmp_path, log_path=""):
    """MotionConnector with a mocked interface, mirroring the pattern in
    test_console_debug_flags.py. scan_db_path=None keeps the audit log a
    no-op; _save_app_config is stubbed so nothing touches the repo config.
    """
    from motion_connector import MotionConnector

    iface = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = None
    c = MotionConnector(
        interface=iface, app_config={}, data_dir=str(tmp_path),
        config_dir="config", log_path=log_path,
    )
    c._save_app_config = MagicMock()
    return c


def test_app_log_path_prefers_the_path_main_handed_in(tmp_path):
    log = tmp_path / "logs" / "open-motion-20260702_120000.log"
    log.parent.mkdir()
    _write(log, "hello\n")

    c = _connector(tmp_path, log_path=str(log))

    assert c.appLogPath() == str(log)


def test_app_log_path_falls_back_to_newest_in_logs_dir(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    _write(logs / "open-motion-20260701_000000.log", "old\n")
    newest = logs / "open-motion-20260702_000000.log"
    _write(newest, "new\n")

    c = _connector(tmp_path)  # no log_path from main.py

    assert c.appLogPath() == str(newest)


def test_app_log_path_empty_when_no_log_exists(tmp_path):
    c = _connector(tmp_path)
    assert c.appLogPath() == ""
    assert c.readAppLog() == ""


def test_read_app_log_is_incremental(tmp_path):
    log = tmp_path / "logs" / "open-motion-20260702_120000.log"
    log.parent.mkdir()
    _write(log, "boot\n")
    c = _connector(tmp_path, log_path=str(log))

    assert c.readAppLog() == "boot\n"

    with open(log, "a", encoding="utf-8") as f:
        f.write("scan started\n")

    assert c.readAppLog() == "scan started\n"
    assert c.readAppLog() == ""
