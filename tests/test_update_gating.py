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
