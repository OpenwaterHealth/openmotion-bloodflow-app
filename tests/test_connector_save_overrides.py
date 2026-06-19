import pytest
import motion_connector
from motion_connector import MotionConnector


@pytest.mark.unit
def test_save_app_config_delegates_diff_to_store(tmp_path, monkeypatch):
    # Redirect resource_path("config", ...) to a throwaway dir so the pre-impl
    # ("red") version of _save_app_config can't clobber the repo's real
    # config/app_config.json when this test first runs.
    (tmp_path / "app_config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("OPENWATER_CONFIG_DIR", str(tmp_path))

    # MotionConnector.__init__ wires hardware/telemetry, so bypass it with
    # __new__ and set only the attributes _save_app_config reads.
    conn = MotionConnector.__new__(MotionConnector)
    conn._app_config = {"developerMode": True, "reducedMode": False}
    conn._baseline_config = {"developerMode": False, "reducedMode": False}

    captured = {}
    monkeypatch.setattr(
        motion_connector.config_store,
        "save_overrides",
        lambda current, baseline: captured.update(current=current, baseline=baseline),
    )
    conn._save_app_config()

    assert captured["current"] == conn._app_config
    assert captured["baseline"] == conn._baseline_config
