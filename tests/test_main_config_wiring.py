import json
import importlib
import pytest


@pytest.mark.unit
def test_load_app_config_applies_local_override(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path))
    (tmp_path / "app_config.local.json").write_text(
        json.dumps({"engineeringMode": True}), encoding="utf-8"
    )
    main = importlib.import_module("main")
    cfg = main._load_app_config()
    assert cfg["engineeringMode"] is True              # override applied over baseline
    # baseline is stashed for the connector (value comes from the shipped
    # config file, so assert presence, not a specific value).
    assert "engineeringMode" in main._APP_CONFIG_BASELINE
