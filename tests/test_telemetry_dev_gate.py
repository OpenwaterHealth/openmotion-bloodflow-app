"""Issue #43 / #234 / #471: per-scan CSV outputs are gated on the mode flags.

The SDK's ``ScanRequest`` defaults ``write_telemetry_csv=True``, so the
connector must explicitly gate it when building the main scan request:

- Telemetry CSV: ``engineeringMode`` AND the persisted ``writeTelemetryCsv``
  toggle (Settings → Engineering → "Save telemetry CSV", #471). Both
  default False — fail closed for clinical use (issue #43), and the
  toggle is re-checked at scan start so a stale ``writeTelemetryCsv=true``
  never writes on a plain clinical build.
- Raw histogram CSV tee: allowed when NOT ``clinicalMode``, or when
  ``engineeringMode`` is unlocked (#234 — research users get raw CSVs).
  The persisted ``writeRawCsv`` toggle is additionally gated on those
  flags, so a plain Clinical build never writes raw CSVs even if a prior
  research / engineering session left the toggle enabled (#43 invariant).

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
    """Clinical mode → no telemetry CSV, no raw CSV tee — even if a prior
    research/engineering session left both toggles enabled (#43)."""
    connector = _make_connector(
        tmp_path,
        {"clinicalMode": True, "engineeringMode": False,
         "writeRawCsv": True, "rawCsvDurationSec": 60,
         "writeTelemetryCsv": True},
    )
    req = _captured_request(connector)
    assert req.write_telemetry_csv is False
    assert req.raw_save_max_duration_s == 0


def test_research_mode_enables_raw_csv_but_not_telemetry(tmp_path):
    """Research build (clinicalMode=False, #234) → raw CSV tee available;
    telemetry CSV stays engineering-only: the #471 toggle alone must not
    unlock it without engineeringMode."""
    connector = _make_connector(
        tmp_path,
        {"clinicalMode": False, "engineeringMode": False,
         "writeRawCsv": True, "rawCsvDurationSec": 60,
         "writeTelemetryCsv": True},
    )
    req = _captured_request(connector)
    assert req.write_telemetry_csv is False
    assert req.raw_save_max_duration_s == 60.0


def test_missing_engineering_mode_key_fails_closed_for_telemetry(tmp_path):
    """No engineeringMode key at all → no telemetry CSV (fail closed),
    even with the #471 toggle on. A missing clinicalMode key defaults
    False (main.py's baseline default is the Research distribution;
    shipped Clinical artifacts always bake clinicalMode=true), so the
    raw tee follows the writeRawCsv toggle."""
    connector = _make_connector(
        tmp_path, {"writeRawCsv": True, "writeTelemetryCsv": True})
    req = _captured_request(connector)
    assert req.write_telemetry_csv is False
    assert req.raw_save_max_duration_s is None  # unbounded — whole scan


def test_engineering_mode_enables_telemetry_and_raw_csv(tmp_path):
    """engineeringMode=True + the #471 opt-in → both engineering outputs."""
    connector = _make_connector(
        tmp_path,
        {
            "clinicalMode": False,
            "engineeringMode": True,
            "writeRawCsv": True,
            "rawCsvDurationSec": 60,
            "writeTelemetryCsv": True,
        },
    )
    req = _captured_request(connector)
    assert req.write_telemetry_csv is True
    assert req.raw_save_max_duration_s == 60.0


def test_engineering_mode_without_telemetry_toggle_fails_closed(tmp_path):
    """#471: engineeringMode alone no longer writes the telemetry CSV —
    a config missing the writeTelemetryCsv key stays opt-out."""
    connector = _make_connector(
        tmp_path, {"clinicalMode": False, "engineeringMode": True})
    assert connector._write_telemetry_csv is False
    req = _captured_request(connector)
    assert req.write_telemetry_csv is False


def test_engineering_mode_with_telemetry_toggle_off(tmp_path):
    """#471: the switch explicitly off keeps telemetry CSV off in
    engineering mode."""
    connector = _make_connector(
        tmp_path,
        {"clinicalMode": False, "engineeringMode": True,
         "writeTelemetryCsv": False},
    )
    req = _captured_request(connector)
    assert req.write_telemetry_csv is False


def test_engineering_unlock_on_clinical_build_restores_outputs(tmp_path):
    """clinicalMode=True + engineeringMode=True (+ both opt-ins) → both
    outputs. The unlock (EngineeringUnlockModal) exists precisely so
    technicians can pull diagnostics from a clinical unit."""
    connector = _make_connector(
        tmp_path,
        {"clinicalMode": True, "engineeringMode": True,
         "writeRawCsv": True, "rawCsvDurationSec": 60,
         "writeTelemetryCsv": True},
    )
    req = _captured_request(connector)
    assert req.write_telemetry_csv is True
    assert req.raw_save_max_duration_s == 60.0


def test_set_write_telemetry_csv_persists_cache_and_config(tmp_path):
    """The Settings → Engineering switch slot (#471) updates the runtime
    cache the scan gate reads AND the persisted config, notifying QML."""
    connector = _make_connector(
        tmp_path, {"clinicalMode": False, "engineeringMode": True})
    connector._save_app_config = MagicMock()
    emits = []
    connector.appConfigChanged.connect(lambda: emits.append(1))

    connector.setWriteTelemetryCsv(True)

    assert connector._write_telemetry_csv is True
    assert connector._app_config["writeTelemetryCsv"] is True
    connector._save_app_config.assert_called_once()
    assert len(emits) == 1

    # The cache the slot writes is the one startCapture gates on — the
    # very next scan after flipping the switch writes the telemetry CSV.
    req = _captured_request(connector)
    assert req.write_telemetry_csv is True

    connector.setWriteTelemetryCsv(False)
    assert connector._write_telemetry_csv is False
    assert connector._app_config["writeTelemetryCsv"] is False


def test_research_mode_respects_raw_csv_toggle_off(tmp_path):
    """Research mode but writeRawCsv off → raw tee still omitted."""
    connector = _make_connector(
        tmp_path,
        {"clinicalMode": False, "engineeringMode": False,
         "writeRawCsv": False},
    )
    req = _captured_request(connector)
    assert req.raw_save_max_duration_s == 0


def test_write_raw_csv_defaults_false(tmp_path):
    """writeRawCsv missing from config → fail closed (no raw output)."""
    connector = _make_connector(
        tmp_path, {"clinicalMode": False, "engineeringMode": False})
    assert connector._write_raw_csv is False
    req = _captured_request(connector)
    assert req.raw_save_max_duration_s == 0
