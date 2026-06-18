import pytest
from motion_connector import _select_update_asset


CLINICAL = {"name": "OpenWater-Setup-1.2.3.exe", "browser_download_url": "u/clinical"}
RUO = {"name": "OpenWater-Setup-1.2.3_RUO.exe", "browser_download_url": "u/ruo"}
ZIP = {"name": "OpenMotionDriver-x64.zip", "browser_download_url": "u/zip"}


@pytest.mark.unit
def test_selects_ruo_bundle_for_ruo_variant():
    assert _select_update_asset([CLINICAL, RUO, ZIP], is_ruo=True) == "u/ruo"


@pytest.mark.unit
def test_selects_clinical_bundle_for_clinical_variant():
    assert _select_update_asset([CLINICAL, RUO, ZIP], is_ruo=False) == "u/clinical"


@pytest.mark.unit
def test_returns_none_when_no_matching_exe():
    assert _select_update_asset([ZIP], is_ruo=True) is None
