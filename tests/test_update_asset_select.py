import pytest
from motion_connector import _select_update_asset


CLINICAL = {"name": "Openwater-Setup-1.2.3.exe", "browser_download_url": "u/clinical"}
RESEARCH = {"name": "Openwater-Setup-1.2.3_Research.exe", "browser_download_url": "u/research"}
ZIP = {"name": "OpenMotionDriver-x64.zip", "browser_download_url": "u/zip"}


@pytest.mark.unit
def test_selects_research_bundle_for_research_variant():
    assert _select_update_asset([CLINICAL, RESEARCH, ZIP], is_research=True) == "u/research"


@pytest.mark.unit
def test_selects_clinical_bundle_for_clinical_variant():
    assert _select_update_asset([CLINICAL, RESEARCH, ZIP], is_research=False) == "u/clinical"


@pytest.mark.unit
def test_returns_none_when_no_matching_exe():
    assert _select_update_asset([ZIP], is_research=True) is None
