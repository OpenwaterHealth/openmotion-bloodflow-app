import json
import pytest
from utils import config_store


DEFAULTS = {"developerMode": False, "reducedMode": False, "leftMask": 0x66, "bfiMax": 10.0}


@pytest.mark.unit
def test_load_merges_overrides_over_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path))
    (tmp_path / "app_config.local.json").write_text(
        json.dumps({"developerMode": True}), encoding="utf-8"
    )
    baseline, merged = config_store.load_app_config(DEFAULTS)
    assert baseline["developerMode"] is False      # baseline untouched
    assert merged["developerMode"] is True          # override wins
    assert merged["bfiMax"] == 10.0                 # untouched key flows through


@pytest.mark.unit
def test_save_writes_only_diff_against_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path))
    baseline = dict(DEFAULTS)
    current = {**DEFAULTS, "developerMode": True}
    config_store.save_overrides(current, baseline)
    written = json.loads((tmp_path / "app_config.local.json").read_text(encoding="utf-8"))
    assert written == {"developerMode": True}       # only the changed key


@pytest.mark.unit
def test_load_with_no_override_file_returns_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path))
    baseline, merged = config_store.load_app_config(DEFAULTS)
    assert merged == baseline
