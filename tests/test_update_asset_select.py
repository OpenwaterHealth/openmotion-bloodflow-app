import pytest
from motion_connector import _select_update_asset


CLINICAL = {"name": "Open-Motion-Setup-1.2.3.exe", "browser_download_url": "u/clinical"}
RESEARCH = {"name": "Open-Motion-Research-Setup-1.2.3.exe", "browser_download_url": "u/research"}
RESEARCH_ZIP = {"name": "Open-Motion-Research-1.2.3.zip", "browser_download_url": "u/research-zip"}
DRIVER_ZIP = {"name": "OpenMotionDriver-x64.zip", "browser_download_url": "u/zip"}

# pre-1.4.0 naming — releases in the wild that a build may still be pointed at
LEGACY_CLINICAL = {"name": "Openwater-Setup-1.2.3.exe", "browser_download_url": "u/old-clinical"}
LEGACY_RESEARCH = {"name": "Openwater-Setup-1.2.3_Research.exe", "browser_download_url": "u/old-research"}


@pytest.mark.unit
def test_selects_research_bundle_for_research_variant():
    assets = [DRIVER_ZIP, RESEARCH_ZIP, CLINICAL, RESEARCH]
    assert _select_update_asset(assets, is_research=True) == "u/research"


@pytest.mark.unit
def test_selects_clinical_bundle_for_clinical_variant():
    assets = [DRIVER_ZIP, RESEARCH_ZIP, RESEARCH, CLINICAL]
    assert _select_update_asset(assets, is_research=False) == "u/clinical"


@pytest.mark.unit
def test_returns_none_when_no_matching_exe():
    assert _select_update_asset([DRIVER_ZIP, RESEARCH_ZIP], is_research=True) is None


@pytest.mark.unit
def test_selects_legacy_named_bundles():
    assets = [DRIVER_ZIP, LEGACY_CLINICAL, LEGACY_RESEARCH]
    assert _select_update_asset(assets, is_research=True) == "u/old-research"
    assert _select_update_asset(assets, is_research=False) == "u/old-clinical"
