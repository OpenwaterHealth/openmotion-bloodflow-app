# tests/test_firmware_update_resume.py
"""Interrupted firmware updates (#737, epic #736).

A bare-metal unit whose firmware write fails is left in the STM32 ROM loader
with incomplete firmware; a power cycle then bricks it. The SDK retries and
finally raises UpdateInterruptedError. These tests pin what the app does with
that: tell the operator not to power off, offer Retry from the cached
download, finish it after a restart, and keep the PC awake meanwhile.
Research builds only. The SDK is mocked throughout: no USB, no network.
"""
import contextlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import firmware_resume
import motion_connector
from motion_connector import RUNNING, READY, MotionConnector
from omotion import ConnectionState
from omotion import firmware_update as sdk_fw
from omotion.dfu.progress import DFUProgress
from omotion.firmware_update import FirmwareKind, LatestInfo
from utils import keep_awake
from utils.settings_store import SettingsStore

pytestmark = pytest.mark.unit

CONSOLE_ASSET = "motion-console-fw-baremetal.bin"
SIGNED_ASSET = "motion-console-fw-signed.bin"
TAG = "1.9.0"


@pytest.fixture(autouse=True)
def _no_network_check(monkeypatch):
    monkeypatch.setattr(motion_connector, "check_latest",
                        lambda kind, **_: LatestInfo(kind=kind, tag=TAG,
                                                     asset_name=CONSOLE_ASSET))


@pytest.fixture(autouse=True)
def _sync_threads(monkeypatch):
    """Run worker threads inline."""
    monkeypatch.setattr(
        motion_connector.threading, "Thread",
        lambda **k: MagicMock(start=lambda: k["target"](*k.get("args", ()))))


@pytest.fixture
def awake(monkeypatch):
    """Record whether the stay-awake request is held."""
    state = {"held": False, "entered": 0}

    @contextlib.contextmanager
    def fake(reason=""):
        state["held"] = True
        state["entered"] += 1
        try:
            yield True
        finally:
            state["held"] = False

    monkeypatch.setattr(motion_connector, "keep_system_awake", fake)
    return state


@pytest.fixture
def downloads(monkeypatch):
    """download_firmware() that writes the release's two images."""
    calls = []

    def fake(info, dest):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        for name in (CONSOLE_ASSET, SIGNED_ASSET):
            (dest / name).write_bytes(b"\x00" * 64)
        calls.append((info, dest))
        return dest / CONSOLE_ASSET

    monkeypatch.setattr(sdk_fw, "download_firmware", fake)
    monkeypatch.setattr(firmware_resume, "download_firmware", fake)
    return calls


class FakeUpdater:
    """FirmwareUpdater stand-in. ``outcomes`` is consumed one per update():
    an exception to raise, or None for success."""

    outcomes: list = []
    calls: list = []
    awake = None

    def __init__(self, *args, **kwargs):
        pass

    def update(self, handle, bin_path, progress_cb=None, **kwargs):
        FakeUpdater.calls.append({
            "handle": handle, "bin_path": Path(bin_path), "awake": FakeUpdater.awake["held"],
            **kwargs})
        if progress_cb is not None:
            progress_cb(DFUProgress(phase="download", percent=50, bytes_written=32,
                                    message="", elapsed_s=1.0))
            progress_cb(DFUProgress(phase="erase", percent=None, bytes_written=None,
                                    message="Retrying (2/3): do not power off",
                                    elapsed_s=2.0))
        outcome = FakeUpdater.outcomes.pop(0) if FakeUpdater.outcomes else None
        if outcome is not None:
            raise outcome
        return MagicMock(success=True, message="bare-metal application installed")


@pytest.fixture
def updater(monkeypatch, awake):
    FakeUpdater.outcomes = []
    FakeUpdater.calls = []
    FakeUpdater.awake = awake
    monkeypatch.setattr(sdk_fw, "FirmwareUpdater", FakeUpdater)
    return FakeUpdater


def _connector(tmp_path, clinical=False, store=None):
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = str(tmp_path / "scans.db")
    iface.get_sdk_version.return_value = "9.9.9"
    cfg = {"engineeringMode": False, "clinicalMode": clinical,
           firmware_resume.PENDING_KEY: None}
    c = MotionConnector(
        interface=iface, app_config=dict(cfg), baseline_config=dict(cfg),
        settings_store=store, data_dir=str(tmp_path), config_dir="config",
    )
    c._state = READY
    return c


def _signals(c):
    out = {"finished": [], "progress": [], "offered": [], "resume_changed": 0}
    c.firmwareUpdateFinished.connect(lambda d, ok, m: out["finished"].append((d, ok, m)))
    c.firmwareUpdateProgress.connect(lambda d, s, p, m: out["progress"].append((d, s, p, m)))
    c.firmwareResumeOffered.connect(lambda d, m: out["offered"].append((d, m)))

    def bump():
        out["resume_changed"] += 1

    c.firmwareResumeChanged.connect(bump)
    return out


def _interrupted(tmp_path, *, in_dfu=True, serial="ROM-UID"):
    image = firmware_resume.cache_dir(Path(tmp_path) / "data", FirmwareKind.CONSOLE, TAG) \
        / CONSOLE_ASSET
    return motion_connector.UpdateInterruptedError(
        "the firmware write failed 3 time(s)", image_path=image,
        kind=FirmwareKind.CONSOLE, dfu_serial=serial, in_dfu=in_dfu, attempts=3)


def _start(c, device="console"):
    c._firmware_update_available[device] = True
    return c.startFirmwareUpdate(device)


# ---------------------------------------------------------------------------
# The update itself
# ---------------------------------------------------------------------------

def test_update_downloads_into_the_data_root_and_holds_the_pc_awake(
        tmp_path, updater, downloads, awake):
    c = _connector(tmp_path)
    out = _signals(c)

    assert _start(c) is True

    dest = downloads[0][1]
    assert dest == firmware_resume.cache_dir(Path(c._data_root), FirmwareKind.CONSOLE, TAG)
    assert updater.calls[0]["awake"] is True, "the PC must not sleep mid-write"
    assert awake["held"] is False and awake["entered"] == 1
    assert out["finished"][-1][1] is True
    assert c._app_config[firmware_resume.PENDING_KEY] is None, "a finished update leaves no record"
    assert c.firmwareResumeDevice == ""


def test_the_record_is_written_before_the_flash_starts(tmp_path, updater, downloads, monkeypatch):
    c = _connector(tmp_path)
    seen = {}

    def spy(self, handle, bin_path, progress_cb=None, **kwargs):
        seen["record"] = dict(c._app_config[firmware_resume.PENDING_KEY])
        return MagicMock(success=True, message="")

    monkeypatch.setattr(FakeUpdater, "update", spy)
    _start(c)

    assert seen["record"]["device"] == "console"
    assert seen["record"]["tag"] == TAG
    assert Path(seen["record"]["image"]).name == CONSOLE_ASSET
    assert seen["record"]["interrupted"] is False


def test_progress_shows_announcements_and_clean_labels(tmp_path, updater, downloads):
    c = _connector(tmp_path)
    out = _signals(c)
    _start(c)

    assert ("console", "download", 50, "Writing") in out["progress"]
    assert ("console", "erase", -1, "Retrying (2/3): do not power off") in out["progress"]


def test_interrupted_update_warns_and_offers_retry(tmp_path, updater, downloads):
    c = _connector(tmp_path)
    out = _signals(c)
    updater.outcomes = [_interrupted(tmp_path)]

    _start(c)

    dev, ok, msg = out["finished"][-1]
    assert dev == "console" and ok is False
    assert "Do not power it off" in msg and "Retry" in msg
    assert c.firmwareResumeDevice == "console"
    assert c.firmwareResumeMessage == msg
    assert out["resume_changed"] >= 1
    record = c._app_config[firmware_resume.PENDING_KEY]
    assert record["interrupted"] is True
    assert record["dfu_serial"] == "ROM-UID"
    assert Path(record["image"]).name == CONSOLE_ASSET
    assert c._firmware_update_in_progress is None


def test_unit_that_dropped_off_usb_is_told_to_reconnect(tmp_path, updater, downloads):
    c = _connector(tmp_path)
    out = _signals(c)
    updater.outcomes = [_interrupted(tmp_path, in_dfu=False)]

    _start(c)

    assert "reconnect its USB cable" in out["finished"][-1][2]
    assert c.firmwareResumeDevice == "console"


def test_an_ordinary_failure_leaves_nothing_to_resume(tmp_path, updater, downloads):
    c = _connector(tmp_path)
    out = _signals(c)
    updater.outcomes = [RuntimeError("DFU device did not appear after enter_dfu()")]

    _start(c)

    assert out["finished"][-1][1] is False
    assert c._app_config[firmware_resume.PENDING_KEY] is None
    assert c.firmwareResumeDevice == ""


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------

def _interrupt(c, tmp_path, updater):
    updater.outcomes = [_interrupted(tmp_path)]
    _start(c)
    updater.calls.clear()


def test_retry_resumes_from_dfu_with_the_cached_image(tmp_path, updater, downloads):
    c = _connector(tmp_path)
    _interrupt(c, tmp_path, updater)
    out = _signals(c)

    assert c.retryFirmwareUpdate("console") is True

    call = updater.calls[0]
    assert call["handle"] is None, "the unit is already in DFU"
    assert call["bin_path"].name == CONSOLE_ASSET
    assert call["bin_path"].parent == firmware_resume.cache_dir(
        Path(c._data_root), FirmwareKind.CONSOLE, TAG)
    assert call["kind"] is FirmwareKind.CONSOLE
    assert call["dfu_serial"] == "ROM-UID"
    assert call["awake"] is True
    assert out["finished"][-1][1] is True
    assert c.firmwareResumeDevice == ""
    assert c._app_config[firmware_resume.PENDING_KEY] is None


def test_a_failed_retry_keeps_the_unit_resumable(tmp_path, updater, downloads):
    c = _connector(tmp_path)
    _interrupt(c, tmp_path, updater)
    updater.outcomes = [RuntimeError("no DFU device found")]

    c.retryFirmwareUpdate("console")

    assert c.firmwareResumeDevice == "console"
    assert c._app_config[firmware_resume.PENDING_KEY] is not None


def test_a_retry_that_is_interrupted_again_stays_resumable(tmp_path, updater, downloads):
    c = _connector(tmp_path)
    _interrupt(c, tmp_path, updater)
    updater.outcomes = [_interrupted(tmp_path)]

    c.retryFirmwareUpdate("console")

    assert c.firmwareResumeDevice == "console"
    assert c._app_config[firmware_resume.PENDING_KEY]["interrupted"] is True


def test_retry_redownloads_a_cleaned_out_cache(tmp_path, updater, downloads):
    c = _connector(tmp_path)
    _interrupt(c, tmp_path, updater)
    folder = firmware_resume.cache_dir(Path(c._data_root), FirmwareKind.CONSOLE, TAG)
    for f in folder.iterdir():
        f.unlink()
    downloads.clear()

    c.retryFirmwareUpdate("console")

    info, dest = downloads[0]
    assert (info.tag, info.asset_name, dest) == (TAG, CONSOLE_ASSET, folder)
    assert updater.calls[0]["bin_path"] == folder / CONSOLE_ASSET


def test_retry_without_a_record_is_refused(tmp_path, updater):
    c = _connector(tmp_path)
    out = _signals(c)

    assert c.retryFirmwareUpdate("console") is False
    assert updater.calls == []
    assert "No interrupted update" in out["finished"][-1][2]


def test_retry_is_refused_in_a_clinical_build(tmp_path, updater):
    c = _connector(tmp_path, clinical=True)
    c._app_config[firmware_resume.PENDING_KEY] = firmware_resume.make_record(
        "console", FirmwareKind.CONSOLE, TAG, CONSOLE_ASSET, interrupted=True)

    assert c.retryFirmwareUpdate("console") is False
    assert updater.calls == []


def test_retry_is_refused_during_a_scan(tmp_path, updater, downloads):
    c = _connector(tmp_path)
    _interrupt(c, tmp_path, updater)
    c._state = RUNNING

    assert c.retryFirmwareUpdate("console") is False
    assert updater.calls == []


def test_another_device_cannot_be_updated_while_a_unit_waits(tmp_path, updater, downloads):
    """The SDK would refuse anyway (a second DFU device is ambiguous); say why."""
    c = _connector(tmp_path)
    _interrupt(c, tmp_path, updater)
    out = _signals(c)

    assert _start(c, "left") is False
    assert "Finish the interrupted console" in out["finished"][-1][2]


def test_the_device_reconnecting_drops_the_resume_state(tmp_path, updater, downloads):
    c = _connector(tmp_path)
    _interrupt(c, tmp_path, updater)
    handle = MagicMock()
    handle.name = "console"
    c._interface.console = handle

    c._on_handle_state_changed_impl(
        handle, ConnectionState.DISCONNECTED, ConnectionState.CONNECTED, "usb")

    assert c.firmwareResumeDevice == ""
    assert c._app_config[firmware_resume.PENDING_KEY] is None


# ---------------------------------------------------------------------------
# Startup: a unit already waiting in DFU
# ---------------------------------------------------------------------------

def _with_record(c, **kwargs):
    kwargs.setdefault("dfu_serial", "ROM-UID")
    kwargs.setdefault("interrupted", True)
    c._app_config[firmware_resume.PENDING_KEY] = firmware_resume.make_record(
        "left", FirmwareKind.SENSOR, TAG, "motion-sensor-fw-baremetal-fpga.bin", **kwargs)


def test_constructing_a_connector_does_not_arm_the_usb_probe(tmp_path, monkeypatch):
    """main.py arms it. Armed by the constructor, every connector a unit test
    builds queued one, and a test that pumps the event loop fired them all at
    once against real USB (a native crash mid-suite)."""
    armed = []
    monkeypatch.setattr(MotionConnector, "arm_firmware_resume_probe",
                        lambda self: armed.append(self))
    _connector(tmp_path)
    assert armed == []


@pytest.mark.parametrize("clinical, armed", [(False, True), (True, False)])
def test_the_probe_is_armed_only_in_research_builds(tmp_path, monkeypatch, clinical, armed):
    c = _connector(tmp_path, clinical=clinical)
    timers = []
    monkeypatch.setattr(motion_connector.QTimer, "singleShot",
                        lambda ms, fn: timers.append(ms))

    c.arm_firmware_resume_probe()

    assert bool(timers) is armed


def test_startup_offers_to_finish_a_recorded_update(tmp_path, monkeypatch):
    monkeypatch.setattr(firmware_resume, "waiting_dfu_serials", lambda: ["ROM-UID"])
    c = _connector(tmp_path)
    _with_record(c)
    out = _signals(c)

    c._probe_interrupted_firmware_update()

    assert out["offered"] and out["offered"][0][0] == "left"
    assert "left sensor" in out["offered"][0][1]
    assert "Do not power it off" in out["offered"][0][1]
    assert c.firmwareResumeDevice == "left"


def test_startup_warns_about_a_waiting_unit_it_has_no_record_of(tmp_path, monkeypatch):
    monkeypatch.setattr(firmware_resume, "waiting_dfu_serials", lambda: ["OTHER"])
    c = _connector(tmp_path)
    _with_record(c)                            # recorded for a different chip
    out = _signals(c)

    c._probe_interrupted_firmware_update()

    assert out["offered"][0][0] == ""
    assert "do not power it off" in out["offered"][0][1]
    assert c.firmwareResumeDevice == ""


def test_startup_without_a_waiting_unit_drops_a_stale_record(tmp_path, monkeypatch):
    monkeypatch.setattr(firmware_resume, "waiting_dfu_serials", lambda: [])
    c = _connector(tmp_path)
    _with_record(c)
    out = _signals(c)

    c._probe_interrupted_firmware_update()

    assert out["offered"] == []
    assert c._app_config[firmware_resume.PENDING_KEY] is None


def test_startup_probe_never_runs_in_a_clinical_build(tmp_path, monkeypatch):
    probe = MagicMock(return_value=["ROM-UID"])
    monkeypatch.setattr(firmware_resume, "waiting_dfu_serials", probe)
    c = _connector(tmp_path, clinical=True)
    out = _signals(c)

    c._probe_interrupted_firmware_update()

    probe.assert_not_called()
    assert out["offered"] == []


def test_a_waiting_unit_suppresses_the_sample_scan_offer(tmp_path, monkeypatch):
    monkeypatch.setattr(firmware_resume, "waiting_dfu_serials", lambda: ["ROM-UID"])
    c = _connector(tmp_path)
    c._interface.is_device_connected.return_value = (False, False, False)
    c._probe_interrupted_firmware_update()
    offers = []
    c.sampleScanOfferRequested.connect(lambda: offers.append(1))
    monkeypatch.setattr(c, "_any_device_connected", lambda: False)

    c._maybe_offer_sample_scan()

    assert offers == []


def test_the_record_survives_a_restart(tmp_path, updater, downloads):
    """STATE tier: written to the scans.db settings table, read back at launch."""
    store = SettingsStore(tmp_path / "settings.db")
    c = _connector(tmp_path, store=store)
    updater.outcomes = [_interrupted(tmp_path)]
    _start(c)

    saved = SettingsStore(tmp_path / "settings.db").load()

    record = firmware_resume.parse_record(saved.get(firmware_resume.PENDING_KEY))
    assert record is not None and record["device"] == "console"
    assert record["interrupted"] is True


# ---------------------------------------------------------------------------
# firmware_resume helpers
# ---------------------------------------------------------------------------

def test_cache_dir_is_per_product_and_release(tmp_path):
    a = firmware_resume.cache_dir(tmp_path, FirmwareKind.SENSOR, "1.8.2")
    b = firmware_resume.cache_dir(tmp_path, FirmwareKind.SENSOR, "1.8.3-rc.1")
    assert a != b and a.parent == b.parent
    assert firmware_resume.cache_dir(tmp_path, FirmwareKind.CONSOLE, "../x").name == ".._x"


@pytest.mark.parametrize("value", [
    None, "x", {}, {"device": "middle", "kind": "console", "tag": "1", "image": "a"},
    {"device": "console", "kind": "toaster", "tag": "1", "image": "a"},
    {"device": "console", "kind": "console", "tag": "1", "image": 5},
    {"device": "console", "kind": "console", "tag": "1", "image": "a", "dfu_serial": 7},
])
def test_malformed_records_are_ignored(value):
    assert firmware_resume.parse_record(value) is None


def test_resume_uses_only_the_apps_cache_folder(tmp_path, downloads, monkeypatch):
    registered = []
    monkeypatch.setattr(firmware_resume, "register_download",
                        lambda path, kind, tag: registered.append((Path(path).name, tag)))
    folder = firmware_resume.cache_dir(tmp_path, FirmwareKind.CONSOLE, TAG)
    folder.mkdir(parents=True)
    for name in (CONSOLE_ASSET, SIGNED_ASSET, "notes.txt"):
        (folder / name).write_bytes(b"\x00")
    record = firmware_resume.make_record(
        "console", FirmwareKind.CONSOLE, TAG, Path("C:/elsewhere") / CONSOLE_ASSET)

    image = firmware_resume.image_for_resume(record, tmp_path)

    assert image == folder / CONSOLE_ASSET
    assert sorted(registered) == [(CONSOLE_ASSET, TAG), (SIGNED_ASSET, TAG)]


def test_resume_refuses_a_file_that_is_not_a_firmware_asset(tmp_path):
    record = firmware_resume.make_record(
        "console", FirmwareKind.CONSOLE, TAG, tmp_path / "evil.bin")
    with pytest.raises(RuntimeError, match="refusing"):
        firmware_resume.image_for_resume(record, tmp_path)


# ---------------------------------------------------------------------------
# Keeping the PC awake
# ---------------------------------------------------------------------------

def test_keep_awake_holds_and_releases_on_windows(monkeypatch):
    calls = []
    monkeypatch.setattr(keep_awake.sys, "platform", "win32")
    monkeypatch.setattr(keep_awake, "_set_thread_execution_state",
                        lambda flags: calls.append(flags) or 0x80000000)

    with keep_awake.keep_system_awake("test") as held:
        assert held is True
        assert calls == [keep_awake.ES_CONTINUOUS | keep_awake.ES_SYSTEM_REQUIRED
                         | keep_awake.ES_DISPLAY_REQUIRED]

    assert calls[-1] == keep_awake.ES_CONTINUOUS


def test_keep_awake_releases_when_the_update_raises(monkeypatch):
    calls = []
    monkeypatch.setattr(keep_awake.sys, "platform", "win32")
    monkeypatch.setattr(keep_awake, "_set_thread_execution_state",
                        lambda flags: calls.append(flags) or 1)

    with pytest.raises(RuntimeError):
        with keep_awake.keep_system_awake():
            raise RuntimeError("write failed")

    assert calls[-1] == keep_awake.ES_CONTINUOUS


def test_keep_awake_is_a_no_op_off_windows(monkeypatch):
    called = MagicMock()
    monkeypatch.setattr(keep_awake.sys, "platform", "darwin")
    monkeypatch.setattr(keep_awake, "_set_thread_execution_state", called)

    with keep_awake.keep_system_awake() as held:
        assert held is False
    called.assert_not_called()


def test_keep_awake_never_raises_over_a_refused_request(monkeypatch):
    monkeypatch.setattr(keep_awake.sys, "platform", "win32")

    def boom(flags):
        raise OSError("no kernel32")

    monkeypatch.setattr(keep_awake, "_set_thread_execution_state", boom)

    with keep_awake.keep_system_awake() as held:
        assert held is False
