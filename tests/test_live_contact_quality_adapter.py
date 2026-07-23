"""Unit tests for the live contact-quality adapter (issue #364).

The SDK's ContactQualityMonitor reports transitions on the pipeline runner
thread; the connector's only job is to marshal them to the main thread and
translate SDK vocabulary into what ContactQualityModal expects.
"""

import pytest

pytestmark = pytest.mark.unit


def test_cq_live_debounce_frames_is_shipped_and_whitelisted(tmp_path, monkeypatch):
    """Both halves must hold or the key is silently non-persistent: it must
    ship in config/app_config.json AND appear in the in-code defaults inside
    _load_app_config(), which filters the file and the runtime overrides to
    that whitelist. A key registered in only one place looks fine until a
    user toggles it and the change evaporates."""
    import json
    from pathlib import Path

    import main as app_main
    from utils import config_store

    repo_root = Path(__file__).resolve().parents[1]
    shipped_path = repo_root / "config" / "app_config.json"
    shipped = json.loads(shipped_path.read_text(encoding="utf-8"))

    # Half 1: the key ships in the config file.
    assert shipped["cq_live_debounce_frames"] == 80

    # Half 2: it survives the whitelist filter. Pin the loader at the shipped
    # file and redirect the writable-overrides layer at tmp_path so a local
    # app_config.local.json left over from running the app can't skew this.
    real_resource_path = config_store.resource_path

    def fake_resource_path(*parts):
        if parts == ("config", "app_config.json"):
            return shipped_path
        return real_resource_path(*parts)

    monkeypatch.setattr(config_store, "resource_path", fake_resource_path)
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path))

    assert app_main._load_app_config()["cq_live_debounce_frames"] == 80
