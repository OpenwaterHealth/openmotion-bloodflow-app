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


@pytest.mark.unit
def test_start_device_monitoring_does_not_block_gui_thread():
    """issue #223: main() calls this after the QML window is already
    visible and before app.exec() starts pumping messages. A blocking wait
    here starves Windows' taskbar icon/thumbnail negotiation for the
    freshly-shown window and can leave it showing the generic icon —
    reproduced on real hardware where console handshake (~5s) routinely
    outlasts the old 2s wait cap. Already-attached devices still reach the
    UI asynchronously via the same signal path used for hotplug."""
    main = importlib.import_module("main")

    calls = []

    class FakeMotionInterface:
        def start(self, **kwargs):
            calls.append(kwargs)

    main._start_device_monitoring(FakeMotionInterface())

    assert calls == [{"wait": False}]
