"""
Unit tests for the low-storage checks (issue #506).

What this exercises
-------------------
  - ``utils.disk_space`` — the free-space query (including a not-yet-
    created data root) and the ``below`` threshold decision.
  - ``MotionConnector.check_startup_storage`` — E-107 critical modal at
    startup under ``minFreeDiskMb`` (1 GB).
  - ``MotionConnector.checkStorageForScan`` — the Start-button gate (also
    the startCapture backstop): E-305 critical modal under 1 GB.
  - ``MotionConnector._on_dropout_check`` → ``_check_scan_storage`` — a
    running scan gets ONE warning toast when free space drops under 1 GB,
    and is stopped gracefully with a warning toast (no modal) under
    ``scanStopFreeDiskMb`` (100 MB).

Connector methods are called unbound on a fake connector, same pattern as
tests/test_scan_stall_watchdog.py.

Marker
------
``pytest.mark.unit`` — pure Python, no QApplication, no hardware.

Run with:  pytest tests/test_low_storage_guard.py -v
"""

import sys
from collections import namedtuple
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import motion_connector  # noqa: E402
from motion_connector import MotionConnector  # noqa: E402
from utils import disk_space  # noqa: E402

pytestmark = pytest.mark.unit

MB = disk_space.MB


# ── utils.disk_space ────────────────────────────────────────────────────


def test_free_bytes_reads_nearest_existing_ancestor(tmp_path):
    missing = tmp_path / "not" / "created" / "yet"
    free = disk_space.free_bytes(str(missing))
    assert isinstance(free, int) and free > 0


def test_free_bytes_returns_none_when_query_fails(monkeypatch, tmp_path):
    def boom(_path):
        raise OSError("device not ready")
    monkeypatch.setattr(disk_space.shutil, "disk_usage", boom)
    assert disk_space.free_bytes(str(tmp_path)) is None


def test_below():
    assert disk_space.below(1023 * MB, 1024) is True
    assert disk_space.below(1024 * MB, 1024) is False
    assert disk_space.below(None, 1024) is False   # unknown never blocks
    assert disk_space.below(0, 0) is False         # disabled
    assert disk_space.below(0, -1) is False


# ── fake connector ──────────────────────────────────────────────────────


class _Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class _FakeConnector:
    """Just the attributes the storage checks touch."""

    check_startup_storage = MotionConnector.check_startup_storage
    checkStorageForScan = MotionConnector.checkStorageForScan
    _storage_detail = MotionConnector._storage_detail
    _storage_check_due = MotionConnector._storage_check_due
    _check_scan_storage = MotionConnector._check_scan_storage
    _stop_scan_low_storage = MotionConnector._stop_scan_low_storage
    _dismiss_dropout_toasts = MotionConnector._dismiss_dropout_toasts
    _note_scan_abort = MotionConnector._note_scan_abort

    def __init__(self, data_root):
        self._data_root = data_root
        self._min_free_disk_mb = 1024.0
        self._scan_stop_free_disk_mb = 100.0
        self._storage_next_check_mono = 0.0
        self._storage_warned = False
        self._scan_abort_notified = False
        self._scan_abort_code = None
        self._scan_abort_reason = ""
        self._camera_dropped = {("left", 2)}
        self._capture_running = True
        self._trigger_state = "OFF"  # watchdog returns right after the hook
        self.captureLog = _Signal()
        self.notificationDismissByTagRequested = _Signal()
        self.criticals = []
        self.toasts = []
        self.stop_calls = 0

    def _scan_elapsed_str(self):
        return "00:02:05"

    def _raise_critical(self, code, detail=""):
        self.criticals.append((code, detail))

    def notify(self, text, type_="info", duration_ms=4000,
               dismissible=True, tag=""):
        self.toasts.append((text, type_, duration_ms, tag))
        return len(self.toasts)

    def stopCapture(self):
        self.stop_calls += 1


_Usage = namedtuple("_Usage", "total used free")


@pytest.fixture
def free_space(monkeypatch):
    """Set the free space every disk_usage query reports, in bytes."""
    state = {"free": 100 * 1024 * MB}

    def fake_usage(_path):
        return _Usage(0, 0, state["free"])

    monkeypatch.setattr(disk_space.shutil, "disk_usage", fake_usage)
    return state


@pytest.fixture
def fake(tmp_path):
    return _FakeConnector(str(tmp_path))


def _tick(fake):
    """One watchdog tick with the poll rate limit cleared."""
    fake._storage_next_check_mono = 0.0
    MotionConnector._on_dropout_check(fake)


# ── 1. startup (E-107) ──────────────────────────────────────────────────


def test_startup_under_1gb_raises_e107(free_space, fake):
    free_space["free"] = 900 * MB
    fake.check_startup_storage()
    assert len(fake.criticals) == 1
    code, detail = fake.criticals[0]
    assert code == "E-107"
    assert "900 MB free" in detail


def test_startup_with_space_or_unknown_is_silent(free_space, monkeypatch, fake):
    fake.check_startup_storage()
    monkeypatch.setattr(disk_space, "free_bytes", lambda _p: None)
    fake.check_startup_storage()
    assert fake.criticals == []


# ── 4. Start button (E-305) ─────────────────────────────────────────────


def test_start_under_1gb_raises_e305_and_refuses(free_space, fake):
    free_space["free"] = 1000 * MB
    assert fake.checkStorageForScan() is False
    assert [c[0] for c in fake.criticals] == ["E-305"]
    assert "1000 MB free" in fake.criticals[0][1]
    # A refusal is not a scan abort: nothing started, nothing to record.
    assert fake._scan_abort_code is None
    assert fake.stop_calls == 0


def test_start_allowed_at_or_above_1gb(free_space, fake):
    free_space["free"] = 1024 * MB
    assert fake.checkStorageForScan() is True
    assert fake.criticals == []


def test_start_disabled_or_unknown_never_blocks(free_space, monkeypatch, fake):
    free_space["free"] = 1 * MB
    fake._min_free_disk_mb = 0
    assert fake.checkStorageForScan() is True
    fake._min_free_disk_mb = 1024.0
    monkeypatch.setattr(disk_space, "free_bytes", lambda _p: None)
    assert fake.checkStorageForScan() is True
    assert fake.criticals == []


# ── 2. mid-scan warning at 1 GB ─────────────────────────────────────────


def test_scan_warns_once_when_crossing_1gb(free_space, fake):
    free_space["free"] = 2048 * MB
    _tick(fake)
    assert fake.toasts == []

    free_space["free"] = 900 * MB
    _tick(fake)
    assert len(fake.toasts) == 1
    text, type_, duration_ms, tag = fake.toasts[0]
    assert type_ == "warning"
    assert "900 MB free" in text
    assert duration_ms == 0  # sticky until dismissed
    assert tag == motion_connector._LOW_STORAGE_TOAST_TAG

    # Still under 1 GB on later ticks: no repeat.
    free_space["free"] = 800 * MB
    _tick(fake)
    assert len(fake.toasts) == 1
    assert fake.stop_calls == 0
    assert fake.criticals == []


# ── 3. mid-scan stop at 100 MB ──────────────────────────────────────────


def test_scan_stops_under_100mb_with_toast_not_modal(free_space, fake):
    free_space["free"] = 90 * MB
    _tick(fake)

    assert fake.stop_calls == 1
    assert fake.criticals == []  # graceful stop, no critical modal
    assert fake._scan_abort_notified is True
    assert fake._scan_abort_code == "E-306"  # scan_ended audit cause
    text, type_, duration_ms, tag = fake.toasts[-1]
    assert type_ == "warning"
    assert text.startswith("Scan stopped")
    assert "almost full" in text and "90 MB free" in text
    assert tag == motion_connector._LOW_STORAGE_TOAST_TAG
    # Per-camera dropout toasts are superseded (#489 pattern).
    assert [c[0] for c in fake.notificationDismissByTagRequested.calls] == [
        "dropout_left_2"]

    # One-shot: later ticks don't stop again.
    _tick(fake)
    assert fake.stop_calls == 1


def test_scan_keeps_running_at_100mb_and_above(free_space, fake):
    free_space["free"] = 100 * MB
    _tick(fake)
    assert fake.stop_calls == 0


def test_storage_check_runs_even_with_trigger_off(free_space, fake):
    """The pipeline writes during the whole capture, not just trigger-ON."""
    fake._trigger_state = "OFF"
    free_space["free"] = 50 * MB
    _tick(fake)
    assert fake.stop_calls == 1


def test_watchdog_polls_storage_at_most_every_interval(monkeypatch, fake):
    calls = []

    def counting_free(path):
        calls.append(path)
        return 100 * 1024 * MB

    monkeypatch.setattr(disk_space, "free_bytes", counting_free)
    now = {"t": 1000.0}
    monkeypatch.setattr(motion_connector.time, "monotonic", lambda: now["t"])

    MotionConnector._on_dropout_check(fake)       # first tick checks
    now["t"] += 1.0
    MotionConnector._on_dropout_check(fake)       # 1 s later: skipped
    assert len(calls) == 1
    now["t"] += motion_connector._STORAGE_CHECK_INTERVAL_S
    MotionConnector._on_dropout_check(fake)
    assert len(calls) == 2
