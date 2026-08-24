"""Connect-time camera warm-up (#494).

_run_sensor_init_impl programs camera FPGAs + writes default registers while
the init worker already has the cameras powered for the ID-cache fill, so the
first Start press doesn't pay the one-time bring-up. Best-effort by contract:
every failure path must fall through to today's behavior (scan-start
configure does the work) without raising out of the init worker.
"""

from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector

pytestmark = pytest.mark.unit

READY = 0x01
PROGRAMMED = 0x02
CONFIGURED = 0x04


class _FakeSensor:
    """Status-driven fake: get_camera_status serves queued snapshots."""

    def __init__(self, snapshots, program_ok=True, configure_ok=True):
        self._snapshots = list(snapshots)
        self._program_ok = program_ok
        self._configure_ok = configure_ok
        self.programmed = []
        self.configured = []

    def get_camera_status(self, mask):
        snap = self._snapshots.pop(0) if self._snapshots else None
        if snap is None:
            return None
        return {i: snap[i] for i in range(8) if mask & (1 << i)}

    def program_fpga(self, camera_position, manual_process):
        self.programmed.append(camera_position)
        return self._program_ok

    def camera_configure_registers(self, camera_position):
        self.configured.append(camera_position)
        return self._configure_ok


def _make_iface():
    iface = MagicMock()
    iface.is_device_connected.return_value = (False, False, False)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    return iface


@pytest.fixture
def connector(tmp_path):
    c = MotionConnector(
        interface=_make_iface(),
        app_config={"engineeringMode": False},
        data_dir=str(tmp_path),
        config_dir="config",
    )
    return c


def test_warm_cameras_touch_nothing(connector):
    sensor = _FakeSensor([[READY | PROGRAMMED | CONFIGURED] * 8] * 2)
    connector._warm_up_cameras("left", sensor)
    assert sensor.programmed == []
    assert sensor.configured == []


def test_cold_cameras_programmed_then_configured_in_bulk(connector):
    sensor = _FakeSensor(
        [
            [READY] * 8,
            [READY | PROGRAMMED] * 8,
        ]
    )
    connector._warm_up_cameras("left", sensor)
    assert sensor.programmed == [1 << i for i in range(8)]
    assert sensor.configured == [0xFF]


def test_failed_program_is_not_configured_blind(connector):
    # Camera 0 never reaches PROGRAMMED on the re-read; only camera 1 may
    # have its registers written.
    first = [READY, READY] + [READY | PROGRAMMED | CONFIGURED] * 6
    second = [READY, READY | PROGRAMMED] + [READY | PROGRAMMED | CONFIGURED] * 6
    sensor = _FakeSensor([first, second])
    connector._warm_up_cameras("left", sensor)
    assert sensor.programmed == [0x01, 0x02]
    assert sensor.configured == [0x02]


def test_status_read_failure_is_quiet(connector):
    sensor = _FakeSensor([None])
    connector._warm_up_cameras("left", sensor)
    assert sensor.programmed == []
    assert sensor.configured == []


def test_exception_never_escapes_the_init_worker(connector):
    sensor = _FakeSensor([[READY] * 8])
    sensor.program_fpga = MagicMock(side_effect=RuntimeError("usb died"))
    connector._warm_up_cameras("left", sensor)  # must not raise


@pytest.fixture
def init_connector(tmp_path, monkeypatch):
    """Connector wired for _run_sensor_init_impl with a mocked warm-up."""
    import motion_connector as mc

    monkeypatch.setattr(mc.time, "sleep", lambda *_: None)
    iface = _make_iface()
    iface.left.is_connected.return_value = True
    iface.left.enable_camera_power.return_value = True
    iface.left.i2c_health = {"all_present": True}

    def build(power_off_unused):
        c = MotionConnector(
            interface=iface,
            app_config={
                "engineeringMode": False,
                "powerOffUnusedCameras": power_off_unused,
            },
            data_dir=str(tmp_path),
            config_dir="config",
        )
        c._leftSensorConnected = True
        c._warm_up_cameras = MagicMock()
        return c, iface

    return build


def test_init_warms_up_when_cameras_stay_powered(init_connector):
    connector, iface = init_connector(power_off_unused=False)
    connector._run_sensor_init_impl("left")
    connector._warm_up_cameras.assert_called_once_with("left", iface.left)
    iface.left.disable_camera_power.assert_not_called()


def test_init_skips_warm_up_when_powering_off_unused(init_connector):
    connector, iface = init_connector(power_off_unused=True)
    connector._run_sensor_init_impl("left")
    connector._warm_up_cameras.assert_not_called()
    iface.left.disable_camera_power.assert_called_once_with(0xFF)
