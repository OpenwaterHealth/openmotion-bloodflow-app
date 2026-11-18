"""#779: the per-scan IMU CSV is opt-in and mode-gated like the raw CSVs.

``writeImuCsv`` (Settings → Data Output → "Save IMU CSV", PREFERENCE) asks
the SDK for ``ScanRequest.write_imu_csv``; the request only carries it when
``!clinicalMode || engineeringMode`` (#234 / #43 gate, re-checked at scan
start). The connector also tolerates an SDK without the field. These tests
mock the hardware seam and inspect the captured ``ScanRequest``.
"""

import pytest

import motion_connector
from config.app_config import APP_CONFIG, PREFERENCE_KEYS
from tests.test_telemetry_dev_gate import _captured_request, _make_connector

pytestmark = pytest.mark.unit


def _imu_flag(req):
    return getattr(req, "write_imu_csv", False)


def test_write_imu_csv_is_an_off_by_default_preference():
    assert APP_CONFIG["writeImuCsv"] is False
    assert "writeImuCsv" in PREFERENCE_KEYS


@pytest.mark.skipif(not motion_connector._SCAN_REQUEST_HAS_IMU_CSV,
                    reason="installed SDK predates ScanRequest.write_imu_csv")
@pytest.mark.parametrize("clinical, engineering, toggle, expected", [
    (False, False, True, True),    # research build: allowed
    (False, False, False, False),  # toggle off
    (True, False, True, False),    # plain clinical: never, even if left on
    (True, True, True, True),      # clinical + engineering unlock
])
def test_imu_csv_follows_toggle_and_mode_gate(
        tmp_path, clinical, engineering, toggle, expected):
    c = _make_connector(tmp_path, {
        "clinicalMode": clinical,
        "engineeringMode": engineering,
        "writeImuCsv": toggle,
    })
    assert _imu_flag(_captured_request(c)) is expected


def test_set_write_imu_csv_updates_config(tmp_path):
    c = _make_connector(tmp_path, {})
    c.setWriteImuCsv(True)
    assert c._write_imu_csv is True
    assert c._app_config["writeImuCsv"] is True
    c.setWriteImuCsv(False)
    assert c._app_config["writeImuCsv"] is False


def test_old_sdk_without_the_field_still_starts_the_scan(
        tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(motion_connector, "_SCAN_REQUEST_HAS_IMU_CSV", False)
    c = _make_connector(tmp_path, {"clinicalMode": False, "writeImuCsv": True})
    with caplog.at_level("WARNING"):
        req = _captured_request(c)
    assert _imu_flag(req) is False
    assert "write_imu_csv" in caplog.text
