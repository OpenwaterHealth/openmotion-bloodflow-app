"""Settings → Engineering switches for sensor-module fans (#463) and the
forceLaserFail interlock-test flag (#464).

Sensor fans: ``setFanControl`` drives the sensor firmware's fan command and
keeps the QML-facing ``_last_fan_status`` cache truthful (notify signal
``sensorFanChanged`` → ``left/rightSensorFanOn`` properties). The
connect-time ``getFanControlStatus`` poll seeds the same cache and emits the
same notify when the polled state differs.

forceLaserFail: ``setForceLaserFail`` persists the flag and updates the
runtime cache that ``set_laser_power_from_config`` reads at console connect.
Deliberately no live push — laser params are written once per console
connect, and a tripped interlock needs a console power-cycle anyway.
"""

from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector

pytestmark = pytest.mark.unit


def _connector(tmp_path, app_config=None, console=True, left=True, right=True):
    """Build a MotionConnector with a mocked interface.

    ``scan_db_path=None`` makes the audit log a no-op, and we stub
    ``_save_app_config`` so a toggle never writes the real
    ``config/app_config.json`` during tests.
    """
    iface = MagicMock()
    iface.is_device_connected.return_value = (console, left, right)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = None
    iface.left.is_connected.return_value = left
    iface.right.is_connected.return_value = right
    iface.left.set_fan_control.return_value = True
    iface.right.set_fan_control.return_value = True
    c = MotionConnector(
        interface=iface, app_config=app_config or {},
        data_dir=str(tmp_path), config_dir="config",
    )
    c._save_app_config = MagicMock()
    return c, iface


def _count_emits(signal):
    """Attach a call counter to a pyqtSignal; emits are synchronous here."""
    calls = []
    signal.connect(lambda: calls.append(1))
    return calls


# ── Sensor fans (#463) ────────────────────────────────────────────────────

def test_set_fan_updates_cache_property_and_notifies(tmp_path):
    c, iface = _connector(tmp_path)
    emits = _count_emits(c.sensorFanChanged)

    assert c.setFanControl("left", True) is True

    iface.left.set_fan_control.assert_called_once_with(True)
    assert c._last_fan_status["left"] is True
    assert c.leftSensorFanOn is True
    assert c.rightSensorFanOn is False  # untouched side stays unknown/off
    assert len(emits) == 1


def test_set_fan_off_right_side(tmp_path):
    c, iface = _connector(tmp_path)
    c._last_fan_status["right"] = True

    assert c.setFanControl("right", False) is True

    iface.right.set_fan_control.assert_called_once_with(False)
    assert c._last_fan_status["right"] is False
    assert c.rightSensorFanOn is False


def test_set_fan_failure_leaves_cache_and_stays_silent(tmp_path):
    c, iface = _connector(tmp_path)
    iface.left.set_fan_control.return_value = False
    emits = _count_emits(c.sensorFanChanged)

    assert c.setFanControl("left", True) is False

    assert c._last_fan_status["left"] is None
    assert c.leftSensorFanOn is False
    assert emits == []


def test_set_fan_disconnected_sensor_makes_no_hardware_call(tmp_path):
    c, iface = _connector(tmp_path, left=False)

    assert c.setFanControl("left", True) is False

    iface.left.set_fan_control.assert_not_called()
    assert c._last_fan_status["left"] is None


def test_get_fan_status_seeds_cache_and_notifies_once(tmp_path):
    # The connect-time sensor-init poll goes through getFanControlStatus;
    # it must seed the cache + notify, but stay quiet on a same-value poll.
    c, iface = _connector(tmp_path)
    iface.left.get_fan_control_status.return_value = True
    emits = _count_emits(c.sensorFanChanged)

    assert c.getFanControlStatus("left") is True
    assert c.leftSensorFanOn is True
    assert len(emits) == 1

    assert c.getFanControlStatus("left") is True
    assert len(emits) == 1  # unchanged status → no re-notify


# ── forceLaserFail (#464) ─────────────────────────────────────────────────

def test_force_laser_fail_enable_persists_cache_and_config(tmp_path):
    c, iface = _connector(tmp_path, {"forceLaserFail": False})
    emits = _count_emits(c.appConfigChanged)

    c.setForceLaserFail(True)

    assert c._force_laser_fail is True
    assert c._app_config["forceLaserFail"] is True
    c._save_app_config.assert_called_once()
    assert len(emits) == 1
    # No live push: the fault set only loads with the laser params at the
    # next console connect (which the interlock test needs anyway).
    iface.apply_laser_power.assert_not_called()


def test_force_laser_fail_disable(tmp_path):
    c, iface = _connector(tmp_path, {"forceLaserFail": True})

    c.setForceLaserFail(False)

    assert c._force_laser_fail is False
    assert c._app_config["forceLaserFail"] is False
    iface.apply_laser_power.assert_not_called()


def test_force_laser_fail_cache_reaches_laser_apply(tmp_path):
    # The runtime cache the toggle writes is the one the connect-time laser
    # bring-up reads — flag flows through as apply_laser_power(force_fault=).
    c, iface = _connector(tmp_path, {"forceLaserFail": False})
    iface.apply_laser_power.return_value = True

    c.setForceLaserFail(True)
    c.set_laser_power_from_config(iface)

    iface.apply_laser_power.assert_called_once_with(force_fault=True)
