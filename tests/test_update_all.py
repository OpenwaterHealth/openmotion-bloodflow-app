# tests/test_update_all.py
""""Update all" orchestration + the cached app-update offer (#514).

The connector owns the order (sensors, console, then the app install that
quits us), stops at the first firmware failure, and refuses to start while
a scan or any other update runs. No hardware, no network: the flash and the
app install are stubbed at _flash_device / app_updater.apply_update.
"""
from unittest.mock import MagicMock

import pytest

import motion_connector
from motion_connector import MotionConnector, READY, RUNNING

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_network_check(monkeypatch):
    monkeypatch.setattr(motion_connector, "check_latest", lambda kind, **_: None)


@pytest.fixture
def sync_threads(monkeypatch):
    """Run every worker thread inline so the test sees its end state."""
    monkeypatch.setattr(
        motion_connector.threading, "Thread",
        lambda **k: MagicMock(start=lambda: k["target"](*k.get("args", ()))))


@pytest.fixture
def fake_updater(monkeypatch):
    calls = []
    mod = MagicMock()
    mod.apply_update.side_effect = lambda c, url: calls.append(url) or True
    monkeypatch.setattr(motion_connector, "app_updater", mod)
    return calls


def _connector(tmp_path, clinical=False):
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = str(tmp_path / "scans.db")
    iface.get_sdk_version.return_value = "9.9.9"
    c = MotionConnector(
        interface=iface,
        app_config={"engineeringMode": False, "clinicalMode": clinical},
        data_dir=str(tmp_path), config_dir="config",
    )
    c._state = READY
    return c


def _offer_everything(c):
    for dev in ("console", "left", "right"):
        c._firmware_update_available[dev] = True
    c.updateAvailable.emit("9.9.9", "https://x/Open-Motion-Research-Setup-9.9.9.exe")


def _record_flashes(c, monkeypatch, fail_on=None):
    flashed = []

    def fake_flash(dev):
        assert c._firmware_update_in_progress == dev
        flashed.append(dev)
        c._firmware_update_in_progress = None
        return dev != fail_on

    monkeypatch.setattr(c, "_flash_device", fake_flash)
    return flashed


def _finished(c):
    out = []
    c.batchUpdateFinished.connect(lambda ok, msg: out.append((ok, msg)))
    return out


# ── App offer cache ───────────────────────────────────────────────────────

def test_app_offer_is_cached_and_withdrawn(tmp_path):
    c = _connector(tmp_path)
    assert c.appUpdateAvailable is False
    c.updateAvailable.emit("2.0.0", "https://x/setup.exe")
    assert (c.appUpdateAvailable, c.appUpdateLatest, c.appUpdateUrl) == (
        True, "2.0.0", "https://x/setup.exe")
    c.updateNotAvailable.emit()
    assert (c.appUpdateAvailable, c.appUpdateLatest) == (False, "")


# ── Order ─────────────────────────────────────────────────────────────────

def test_firmware_first_sensors_then_console_then_app(
        tmp_path, monkeypatch, sync_threads, fake_updater):
    c = _connector(tmp_path)
    _offer_everything(c)
    order = []
    c.batchUpdateStep.connect(order.append)
    flashed = _record_flashes(c, monkeypatch)
    out = _finished(c)

    assert c.startUpdateAll() is True

    assert order == ["left", "right", "console", "app"]
    assert flashed == ["left", "right", "console"]
    assert fake_updater == ["https://x/Open-Motion-Research-Setup-9.9.9.exe"]
    assert out and out[-1][0] is True
    assert c.batchUpdateRunning is False and c.updateBusy is False


def test_only_pending_items_run(tmp_path, monkeypatch, sync_threads, fake_updater):
    c = _connector(tmp_path)
    c._firmware_update_available["right"] = True   # no app offer, one device
    flashed = _record_flashes(c, monkeypatch)
    out = _finished(c)
    assert c.startUpdateAll() is True
    assert flashed == ["right"]
    assert fake_updater == []
    assert out[-1][0] is True and "power-cycle" in out[-1][1].lower()


def test_firmware_failure_stops_before_app(
        tmp_path, monkeypatch, sync_threads, fake_updater):
    c = _connector(tmp_path)
    _offer_everything(c)
    flashed = _record_flashes(c, monkeypatch, fail_on="right")
    out = _finished(c)

    assert c.startUpdateAll() is True

    assert flashed == ["left", "right"], "console must not flash after a failure"
    assert fake_updater == [], "app must not install after a firmware failure"
    assert out[-1][0] is False and "right sensor" in out[-1][1].lower()
    assert c.updateBusy is False


def test_app_install_failure_reported(tmp_path, monkeypatch, sync_threads, fake_updater):
    c = _connector(tmp_path)
    c.updateAvailable.emit("9.9.9", "https://x/setup.exe")
    motion_connector.app_updater.apply_update.side_effect = lambda c_, url: False
    out = _finished(c)
    assert c.startUpdateAll() is True
    assert out[-1][0] is False
    assert c.updateBusy is False


def test_app_step_skipped_when_updater_not_in_build(
        tmp_path, monkeypatch, sync_threads):
    monkeypatch.setattr(motion_connector, "app_updater", None)
    c = _connector(tmp_path)
    c.updateAvailable.emit("9.9.9", "https://x/setup.exe")
    assert c._batch_update_plan() == []


# ── Gates ─────────────────────────────────────────────────────────────────

def test_refused_in_clinical(tmp_path, monkeypatch, sync_threads, fake_updater):
    c = _connector(tmp_path, clinical=True)
    _offer_everything(c)
    flashed = _record_flashes(c, monkeypatch)
    assert c.startUpdateAll() is False
    assert flashed == [] and fake_updater == []


def test_refused_during_scan(tmp_path, monkeypatch, sync_threads, fake_updater):
    c = _connector(tmp_path)
    _offer_everything(c)
    c._state = RUNNING
    flashed = _record_flashes(c, monkeypatch)
    out = _finished(c)
    assert c.startUpdateAll() is False
    assert flashed == [] and "scan" in out[-1][1].lower()


def test_refused_while_another_update_runs(tmp_path, monkeypatch, fake_updater):
    c = _connector(tmp_path)
    _offer_everything(c)
    c._firmware_update_in_progress = "console"
    out = _finished(c)
    assert c.startUpdateAll() is False
    assert "in progress" in out[-1][1].lower()


def test_refused_with_nothing_to_update(tmp_path):
    c = _connector(tmp_path)
    assert c.startUpdateAll() is False


def test_single_flash_refused_while_batch_runs(tmp_path):
    c = _connector(tmp_path)
    c._firmware_update_available["left"] = True
    c._batch_update_running = True
    assert c.startFirmwareUpdate("left") is False


# ── After a successful flash ──────────────────────────────────────────────

def test_successful_flash_withdraws_the_offer(tmp_path, monkeypatch):
    """A flashed device runs the old image until power-cycled and cannot
    enter DFU again; it must drop out of the banner and Update all."""
    c = _connector(tmp_path)
    c._firmware_versions["left"] = "1.0.0"
    c._firmware_update_available["left"] = True
    c._firmware_update_in_progress = "left"
    c._interface.left = MagicMock()

    info = MagicMock(tag="1.1.0")
    monkeypatch.setattr(motion_connector, "check_latest", lambda kind, **_: info)
    import omotion.firmware_update as fw
    monkeypatch.setattr(fw, "download_firmware", lambda info, dest: "x.bin")
    monkeypatch.setattr(
        fw, "FirmwareUpdater",
        lambda: MagicMock(update=lambda h, p, progress_cb=None: MagicMock(success=True)))

    assert c._flash_device("left") is True
    assert c._firmware_update_available["left"] is False
    assert c._firmware_update_in_progress is None
    assert c._batch_update_plan() == []
