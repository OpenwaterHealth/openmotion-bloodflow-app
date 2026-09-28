"""
Unit tests for the low-storage scan guard (issue #506).

What this exercises
-------------------
  - ``utils.disk_space`` — the pure helpers: free-space query (including a
    not-yet-created data root), the scan-size estimate, the start
    shortfall and the mid-scan floor decision.
  - ``MotionConnector._storage_allows_scan`` — the pre-scan gate that
    refuses a scan with the E-305 modal before any scan state is touched.
  - ``MotionConnector._abort_scan_low_storage`` and the watchdog hook in
    ``_on_dropout_check`` — the mid-scan stop with the E-306 modal.

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


def test_estimate_db_only_scales_with_duration_and_cameras():
    one = disk_space.estimate_scan_bytes(60, 1)
    assert one == disk_space.DB_BYTES_PER_CAMERA_S * 60
    assert disk_space.estimate_scan_bytes(60, 16) == 16 * one
    assert disk_space.estimate_scan_bytes(60, 0) == 0


def test_estimate_raw_csv_contract():
    base = disk_space.estimate_scan_bytes(100, 1, raw_csv_max_s=0)
    raw_rate = disk_space.RAW_CSV_BYTES_PER_CAMERA_S
    # None = raw for the whole scan.
    assert disk_space.estimate_scan_bytes(100, 1, None) == base + raw_rate * 100
    # A cap shorter than the scan bounds the raw part...
    assert disk_space.estimate_scan_bytes(100, 1, 30) == base + raw_rate * 30
    # ...and one longer than the scan does not inflate it.
    assert disk_space.estimate_scan_bytes(100, 1, 999) == base + raw_rate * 100


def test_estimate_auto_export_adds_a_db_sized_copy():
    base = disk_space.estimate_scan_bytes(100, 2)
    assert disk_space.estimate_scan_bytes(100, 2, auto_export=True) == 2 * base


def test_start_shortfall():
    assert disk_space.start_shortfall(2000, 500, 1000) is None
    assert disk_space.start_shortfall(1500, 500, 1000) is None  # exactly fits
    assert disk_space.start_shortfall(1200, 500, 1000) == 300
    # Unknown free space never blocks.
    assert disk_space.start_shortfall(None, 500, 1000) is None


def test_below_floor():
    assert disk_space.below_floor(100, 256) is True
    assert disk_space.below_floor(256, 256) is True
    assert disk_space.below_floor(257, 256) is False
    assert disk_space.below_floor(None, 256) is False
    assert disk_space.below_floor(0, 0) is False  # disabled


# ── fake connector ──────────────────────────────────────────────────────


class _Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class _FakeConnector:
    """Just the attributes the storage-guard methods touch."""

    _storage_allows_scan = MotionConnector._storage_allows_scan
    _storage_check_due = MotionConnector._storage_check_due
    _abort_scan_low_storage = MotionConnector._abort_scan_low_storage
    _dismiss_dropout_toasts = MotionConnector._dismiss_dropout_toasts
    _note_scan_abort = MotionConnector._note_scan_abort
    _auto_export_enabled = MotionConnector._auto_export_enabled

    def __init__(self, data_root):
        self._data_root = data_root
        self._app_config = {"clinicalMode": False, "engineeringMode": False}
        self._write_raw_csv = False
        self._raw_csv_duration_sec = None
        self._scan_min_free_disk_mb = 1024.0
        self._scan_stop_free_disk_mb = 256.0
        self._storage_next_check_mono = 0.0
        self._scan_abort_notified = False
        self._scan_abort_code = None
        self._scan_abort_reason = ""
        self._camera_dropped = {("left", 2)}
        self._capture_running = True
        self._trigger_state = "OFF"  # watchdog returns right after the hook
        self.captureLog = _Signal()
        self.notificationDismissByTagRequested = _Signal()
        self.criticals = []
        self.stop_calls = 0

    def _scan_elapsed_str(self):
        return "00:02:05"

    def _raise_critical(self, code, detail=""):
        self.criticals.append((code, detail))

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


# ── pre-scan gate (E-305) ───────────────────────────────────────────────


def test_start_allowed_with_ample_space(free_space, tmp_path):
    fake = _FakeConnector(str(tmp_path))
    assert MotionConnector._storage_allows_scan(fake, 3600, 0xFF, 0xFF)
    assert fake.criticals == []


def test_start_refused_below_reserve_raises_e305(free_space, tmp_path):
    free_space["free"] = 900 * MB  # under the 1024 MB reserve alone
    fake = _FakeConnector(str(tmp_path))
    assert not MotionConnector._storage_allows_scan(fake, 60, 0xC3, 0xC3)
    assert len(fake.criticals) == 1
    code, detail = fake.criticals[0]
    assert code == "E-305"
    assert "900 MB free" in detail
    assert "1024 MB reserve" in detail
    # A refusal is not a scan abort: nothing started, nothing to record.
    assert fake._scan_abort_code is None
    assert fake.stop_calls == 0


def test_raw_csv_estimate_can_refuse_a_long_research_scan(free_space, tmp_path):
    # 5 GB free: fine for an hour DB-only, not with raw CSVs on 16 cameras
    # (~9 GB estimated).
    free_space["free"] = 5 * 1024 * MB
    fake = _FakeConnector(str(tmp_path))
    assert MotionConnector._storage_allows_scan(fake, 3600, 0xFF, 0xFF)
    fake._write_raw_csv = True
    assert not MotionConnector._storage_allows_scan(fake, 3600, 0xFF, 0xFF)
    assert fake.criticals[-1][0] == "E-305"


def test_clinical_build_ignores_stale_raw_csv_toggle(free_space, tmp_path):
    """Raw CSVs are never written on a plain clinical build (#43), so a
    stale writeRawCsv toggle must not inflate the estimate either."""
    free_space["free"] = 5 * 1024 * MB
    fake = _FakeConnector(str(tmp_path))
    fake._app_config["clinicalMode"] = True
    fake._write_raw_csv = True
    assert MotionConnector._storage_allows_scan(fake, 3600, 0xFF, 0xFF)


def test_start_gate_disabled_and_unknown_free_never_block(
        free_space, monkeypatch, tmp_path):
    free_space["free"] = 1 * MB
    fake = _FakeConnector(str(tmp_path))
    fake._scan_min_free_disk_mb = 0
    assert MotionConnector._storage_allows_scan(fake, 60, 0xFF, 0xFF)

    fake._scan_min_free_disk_mb = 1024.0
    monkeypatch.setattr(disk_space, "free_bytes", lambda _p: None)
    assert MotionConnector._storage_allows_scan(fake, 60, 0xFF, 0xFF)
    assert fake.criticals == []


# ── mid-scan stop (E-306) ───────────────────────────────────────────────


def test_abort_low_storage_raises_e306_and_stops_capture(tmp_path):
    fake = _FakeConnector(str(tmp_path))
    MotionConnector._abort_scan_low_storage(fake, 200 * MB)

    assert fake._scan_abort_notified is True
    assert fake.stop_calls == 1
    assert fake.criticals == [
        ("E-306", "200 MB free on the data drive (scan elapsed 00:02:05)")]
    assert fake._scan_abort_code == "E-306"
    # The modal supersedes per-camera dropout toasts (#489 pattern).
    assert [c[0] for c in fake.notificationDismissByTagRequested.calls] == [
        "dropout_left_2"]
    (msg,) = fake.captureLog.calls[0]
    assert "almost full" in msg and "[00:02:05]" in msg


def test_watchdog_stops_scan_at_floor_even_with_trigger_off(
        free_space, tmp_path):
    free_space["free"] = 100 * MB
    fake = _FakeConnector(str(tmp_path))
    MotionConnector._on_dropout_check(fake)
    assert fake.stop_calls == 1
    assert fake.criticals[0][0] == "E-306"

    # One-shot: later ticks don't stack a second modal.
    fake._storage_next_check_mono = 0.0
    MotionConnector._on_dropout_check(fake)
    assert fake.stop_calls == 1


def test_watchdog_leaves_scan_running_above_floor(free_space, tmp_path):
    free_space["free"] = 300 * MB
    fake = _FakeConnector(str(tmp_path))
    MotionConnector._on_dropout_check(fake)
    assert fake.stop_calls == 0
    assert fake.criticals == []


def test_watchdog_polls_storage_at_most_every_interval(
        monkeypatch, tmp_path):
    calls = []

    def counting_free(path):
        calls.append(path)
        return 100 * 1024 * MB

    monkeypatch.setattr(disk_space, "free_bytes", counting_free)
    now = {"t": 1000.0}
    monkeypatch.setattr(motion_connector.time, "monotonic", lambda: now["t"])
    fake = _FakeConnector(str(tmp_path))

    MotionConnector._on_dropout_check(fake)       # first tick checks
    now["t"] += 1.0
    MotionConnector._on_dropout_check(fake)       # 1 s later: skipped
    assert len(calls) == 1
    now["t"] += motion_connector._STORAGE_CHECK_INTERVAL_S
    MotionConnector._on_dropout_check(fake)
    assert len(calls) == 2
