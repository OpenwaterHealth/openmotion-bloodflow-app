import pytest
import motion_connector
from motion_connector import MotionConnector


@pytest.mark.unit
def test_save_app_config_delegates_diff_to_store(tmp_path, monkeypatch):
    # The writable-overrides layer is already routed at a per-test tmp dir by
    # conftest's autouse _isolate_writable_root fixture (app_paths.
    # DATA_ROOT_OVERRIDE), so nothing here can touch the repo's real config
    # even before save_overrides is stubbed below.

    # MotionConnector.__init__ wires hardware/telemetry, so bypass it with
    # __new__ and set only the attributes _save_app_config reads.
    conn = MotionConnector.__new__(MotionConnector)
    conn._app_config = {"engineeringMode": True, "clinicalMode": False}
    conn._baseline_config = {"engineeringMode": False, "clinicalMode": False}

    captured = {}
    monkeypatch.setattr(
        motion_connector.config_store,
        "save_overrides",
        lambda current, baseline: captured.update(current=current, baseline=baseline),
    )
    conn._save_app_config()

    assert captured["current"] == conn._app_config
    assert captured["baseline"] == conn._baseline_config
