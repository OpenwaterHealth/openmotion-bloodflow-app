"""Unified update gating (#386): _beta_enabled, _select_release, app beta path,
and the refresh/withdraw hook that ties engineering mode to the beta channel."""
from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector

pytestmark = pytest.mark.unit


def _connector(tmp_path, **cfg):
    iface = MagicMock()
    iface.is_device_connected.return_value = (False, False, False)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = str(tmp_path / "scans.db")
    iface.get_sdk_version.return_value = "9.9.9"
    return MotionConnector(
        interface=iface, app_config=cfg,
        data_dir=str(tmp_path), config_dir="config",
    )


@pytest.mark.parametrize("clinical,eng,beta,expected", [
    (False, True,  True,  True),    # research + eng + toggle -> beta
    (False, True,  False, False),   # toggle off
    (False, False, True,  False),   # eng off -> no beta even if toggle on
    (True,  True,  True,  False),   # clinical never opts in
])
def test_beta_enabled_matrix(tmp_path, clinical, eng, beta, expected):
    c = _connector(tmp_path, clinicalMode=clinical,
                   engineeringMode=eng, downloadBetaUpdates=beta)
    assert c._beta_enabled() is expected


# ── Refresh / withdraw on engineering-mode or beta-toggle change ──────────

def test_eng_mode_change_refreshes_both_updaters_in_research(tmp_path):
    c = _connector(tmp_path, clinicalMode=False, engineeringMode=False,
                   downloadBetaUpdates=False)
    fw, app = [], []
    c._refresh_firmware_update_check = lambda: fw.append(1)
    c.checkForUpdates = lambda: app.append(1)
    c.setConfig("engineeringMode", True)
    assert fw == [1], "firmware detection must re-run on engineering-mode change"
    assert app == [1], "app updater must re-check on engineering-mode change"


def test_eng_mode_change_makes_no_network_call_in_clinical(tmp_path):
    c = _connector(tmp_path, clinicalMode=True, engineeringMode=False,
                   downloadBetaUpdates=False)
    fw, app = [], []
    c._refresh_firmware_update_check = lambda: fw.append(1)
    c.checkForUpdates = lambda: app.append(1)
    c.setConfig("engineeringMode", True)
    assert fw == [1], "the refresh hook must fire on engineering-mode change"
    assert app == [], "clinical build must not make an outbound app-update call"
