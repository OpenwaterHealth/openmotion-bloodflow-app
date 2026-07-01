"""Issue #43 (regression): telemetry / raw CSV output must be gated on
engineeringMode.

The SDK's ``ScanRequest`` defaults ``write_telemetry_csv=True``, so the
connector must explicitly pass ``engineeringMode`` (default False — fail
closed for clinical use) when building the main scan request. The raw
histogram CSV tee is a engineering-only Settings toggle, so its persisted
``writeRawCsv`` value must additionally be gated on engineeringMode.

These tests mock the hardware seam (``interface.start_scan``) and assert
on the captured ``ScanRequest`` — no hardware, no app launch.
"""

from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector

pytestmark = pytest.mark.unit


def _make_connector(tmp_path, app_config):
    iface = MagicMock()
    iface.console = MagicMock()
    iface.left = MagicMock()
    iface.right = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow = MagicMock()
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.start_scan.return_value = True
    # LiveScanSource treats scan_db_path as an optional path string; a
    # bare MagicMock attribute would leak into os.path calls.
    iface.scan_db_path = None

    c = MotionConnector(
        interface=iface,
        app_config=app_config,
        data_dir=str(tmp_path),
        config_dir="config",
    )
    c._consoleConnected = True
    c._leftSensorConnected = True
    c._rightSensorConnected = True
    return c


def _captured_request(connector):
    ok = connector.startCapture("subj", 5, 0x66, 0x66, False)
    assert ok is True
    connector._interface.start_scan.assert_called_once()
    return connector._interface.start_scan.call_args.args[0]


def test_clinical_mode_disables_telemetry_and_raw_csv(tmp_path):
    """engineeringMode=False → no telemetry CSV, no raw CSV tee — even if a
    prior engineering session left writeRawCsv enabled in the config."""
    connector = _make_connector(
        tmp_path,
        {"engineeringMode": False, "writeRawCsv": True},
    )
    req = _captured_request(connector)
    assert req.write_telemetry_csv is False
    assert req.raw_save_max_duration_s == 0


def test_missing_developer_mode_key_fails_closed(tmp_path):
    """No engineeringMode key at all → same as clinical mode (fail closed)."""
    connector = _make_connector(tmp_path, {"writeRawCsv": True})
    req = _captured_request(connector)
    assert req.write_telemetry_csv is False
    assert req.raw_save_max_duration_s == 0


def test_developer_mode_enables_telemetry_and_raw_csv(tmp_path):
    """engineeringMode=True restores both engineering outputs."""
    connector = _make_connector(
        tmp_path,
        {
            "engineeringMode": True,
            "writeRawCsv": True,
            "rawCsvDurationSec": 60,
        },
    )
    req = _captured_request(connector)
    assert req.write_telemetry_csv is True
    assert req.raw_save_max_duration_s == 60.0


def test_developer_mode_respects_raw_csv_toggle_off(tmp_path):
    """engineeringMode=True but writeRawCsv off → raw tee still omitted."""
    connector = _make_connector(
        tmp_path,
        {"engineeringMode": True, "writeRawCsv": False},
    )
    req = _captured_request(connector)
    assert req.write_telemetry_csv is True
    assert req.raw_save_max_duration_s == 0


def test_write_raw_csv_defaults_false(tmp_path):
    """writeRawCsv missing from config → fail closed (no raw output)."""
    connector = _make_connector(tmp_path, {"engineeringMode": True})
    assert connector._write_raw_csv is False
    req = _captured_request(connector)
    assert req.raw_save_max_duration_s == 0
