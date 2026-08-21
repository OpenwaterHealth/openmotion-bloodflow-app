"""``debugFidCorruptTest`` config flag -> firmware ``DEBUG_FLAG_FID_CORRUPT`` (0x800).

Config-only, like ``deferHistoSend``/``debugHistoStallTest``: read from the app
config at construction into ``_fid_corrupt_test`` and folded into the sensor
debug-flag bitmask by ``_compute_sensor_debug_flags``, which ``_run_sensor_init``
pushes to each sensor at connect. The firmware bit arms on-command frame_id
corruption bursts — the etch-a-sketch repro (sensor-fw#123 / sdk#220). It has
to be a config key because the app re-pushes its computed mask on every
connect, which would wipe a flag armed out-of-band (bloodflow-app#444).
"""

from unittest.mock import MagicMock

import pytest

from motion_connector import DEBUG_FLAG_FID_CORRUPT, MotionConnector
from omotion.config import DEBUG_FLAG_HISTO_CMP

pytestmark = pytest.mark.unit


def _connector(tmp_path, app_config=None, left=True, right=True):
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, left, right)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = None
    iface.left.is_connected.return_value = left
    iface.right.is_connected.return_value = right
    iface.left.set_debug_flags.return_value = True
    iface.right.set_debug_flags.return_value = True
    c = MotionConnector(
        interface=iface, app_config=app_config or {},
        data_dir=str(tmp_path), config_dir="config",
    )
    c._save_app_config = MagicMock()
    return c, iface


def test_constant_matches_firmware_bit():
    # Guarded import falls back to the literal on SDKs that predate the
    # constant; either way it must be the firmware's 0x800.
    assert DEBUG_FLAG_FID_CORRUPT == 0x800


def test_defaults_off_when_absent(tmp_path):
    c, _ = _connector(tmp_path, {})

    assert c._fid_corrupt_test is False
    assert c._compute_sensor_debug_flags() & DEBUG_FLAG_FID_CORRUPT == 0


def test_enabled_sets_bit(tmp_path):
    c, _ = _connector(tmp_path, {"debugFidCorruptTest": True})

    assert c._fid_corrupt_test is True
    assert c._compute_sensor_debug_flags() == DEBUG_FLAG_FID_CORRUPT


def test_combines_with_histo_cmp(tmp_path):
    c, _ = _connector(tmp_path, {"debugFidCorruptTest": True, "histoCmp": True})

    assert c._compute_sensor_debug_flags() == (
        DEBUG_FLAG_FID_CORRUPT | DEBUG_FLAG_HISTO_CMP
    )


def test_pushed_to_connected_sensor_at_init(tmp_path):
    c, iface = _connector(tmp_path, {"debugFidCorruptTest": True})
    c._leftSensorConnected = True

    c._run_sensor_init("left")

    iface.left.set_debug_flags.assert_called_once_with(DEBUG_FLAG_FID_CORRUPT)
