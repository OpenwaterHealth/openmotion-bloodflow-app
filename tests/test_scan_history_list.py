"""get_scan_list — History must list DB-backed scans, not just corrected CSVs."""

from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector
from omotion.ScanDatabase import ScanDatabase

pytestmark = pytest.mark.unit


def _connector(tmp_path, scan_db_path=None):
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = scan_db_path  # explicit: MagicMock default would be truthy
    return MotionConnector(
        interface=iface, app_config={"developerMode": False},
        data_dir=str(tmp_path), config_dir="config",
    )


def _session(db_path, label, start):
    db = ScanDatabase(db_path=db_path)
    db.create_session(session_label=label, session_start=start,
                      session_notes=None, session_meta={})
    db.close()


def _write_csv(tmp_path, name, text="x\n"):
    """Scan CSVs live under <root>/data (self._data_root), not the root."""
    path = tmp_path / "data" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_get_scan_list_includes_db_session_without_csv(tmp_path):
    """A reduced-mode scan writes no corrected CSV — it must still appear,
    sourced from the scan DB's sessions."""
    db_path = str(tmp_path / "scans.db")
    _session(db_path, "20260528_211930_ow6I2NJL", 1.0)
    c = _connector(tmp_path, scan_db_path=db_path)
    assert "20260528_211930_ow6I2NJL" in c.get_scan_list()


def test_get_scan_list_merges_csv_and_db_without_duplicates(tmp_path):
    _write_csv(tmp_path, "20260528_120000_subjA.csv", "frame_id,timestamp_s\n")
    db_path = str(tmp_path / "scans.db")
    _session(db_path, "20260528_120000_subjA", 1.0)   # same scan, also in DB
    _session(db_path, "20260528_130000_subjB", 2.0)   # DB-only scan
    c = _connector(tmp_path, scan_db_path=db_path)

    scans = c.get_scan_list()
    assert scans.count("20260528_120000_subjA") == 1  # merged, not duplicated
    assert "20260528_130000_subjB" in scans
    # newest first
    assert scans.index("20260528_130000_subjB") < scans.index("20260528_120000_subjA")


def test_get_scan_list_csv_only_when_no_db(tmp_path):
    _write_csv(tmp_path, "20260528_120000_subjA.csv")
    c = _connector(tmp_path, scan_db_path=None)
    assert c.get_scan_list() == ["20260528_120000_subjA"]


def test_get_scan_list_skips_aux_csvs(tmp_path):
    _write_csv(tmp_path, "20260528_120000_subjA_telemetry.csv")
    _write_csv(tmp_path, "20260528_120000_subjA_left_mask66_raw.csv")
    c = _connector(tmp_path, scan_db_path=None)
    assert c.get_scan_list() == []
