import json
import importlib
import sys

import pytest

from config import app_config as compiled
from utils import app_paths


def _root(monkeypatch, tmp_path):
    monkeypatch.setattr(app_paths, "DATA_ROOT_OVERRIDE", tmp_path)


def _compiled(monkeypatch, **values):
    """Pretend the build stamped these values into config/app_config.py."""
    for key, value in values.items():
        monkeypatch.setitem(compiled.APP_CONFIG, key, value)


@pytest.mark.unit
def test_load_app_config_does_not_read_the_legacy_overrides_file(tmp_path, monkeypatch):
    """Since #546 no file can set engineeringMode: the legacy
    app_config.local.json is only ever imported (preference keys) by
    main() once the settings store is open, never by the loader."""
    _root(monkeypatch, tmp_path)
    (tmp_path / "app_config.local.json").write_text(
        json.dumps({"engineeringMode": True, "bfiMax": 5.0}), encoding="utf-8"
    )
    main = importlib.import_module("main")
    cfg = main._load_app_config()
    assert cfg["engineeringMode"] is False
    assert cfg["bfiMax"] == 10.0
    assert (tmp_path / "app_config.local.json").exists()   # untouched here
    assert main._APP_CONFIG_BASELINE["engineeringMode"] is False


# --------------------------------------------------------------------------
# No environment variable steers startup
# --------------------------------------------------------------------------
#
# The OPENMOTION_CLINICAL / OPENMOTION_PORTABLE / OPENWATER_DATA_ROOT env
# overrides were removed so a packaged artifact boots identically whatever
# the host machine's environment carries. The dev-only equivalents are
# command-line flags, parsed by main._parse_dev_args for source runs only.

@pytest.mark.unit
def test_env_vars_no_longer_steer_the_build_variant(tmp_path, monkeypatch):
    _root(monkeypatch, tmp_path)
    _compiled(monkeypatch, clinicalMode=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("OPENMOTION_CLINICAL", "1")
    monkeypatch.setenv("OPENMOTION_PORTABLE", "1")

    main = importlib.import_module("main")
    cfg = main._load_app_config()

    assert cfg["clinicalMode"] is False
    assert cfg["portableMode"] is False
    assert main._APP_CONFIG_BASELINE["clinicalMode"] is False
    assert main._APP_CONFIG_BASELINE["portableMode"] is False


@pytest.mark.unit
def test_dev_flags_flip_variant_in_merged_and_baseline(tmp_path, monkeypatch):
    _root(monkeypatch, tmp_path)
    _compiled(monkeypatch, clinicalMode=False)
    monkeypatch.setattr(sys, "platform", "win32")

    main = importlib.import_module("main")
    cfg = main._load_app_config(clinical=True, portable=True)

    assert cfg["clinicalMode"] is True
    assert cfg["portableMode"] is True
    assert main._APP_CONFIG_BASELINE["clinicalMode"] is True
    assert main._APP_CONFIG_BASELINE["portableMode"] is True


@pytest.mark.unit
def test_parse_dev_args_source_run():
    main = importlib.import_module("main")
    argv = [
        "main.py", "--portable", "--research", "--data-root", "D:\\ow",
        "-platform", "offscreen",
    ]
    dev, qt_argv = main._parse_dev_args(argv, frozen=False)
    assert dev == {
        "clinical": False, "portable": True, "data_root": "D:\\ow",
        "config_override": None, "ignored": None,
    }
    # Qt's own single-dash options pass through, ours are stripped.
    assert qt_argv == ["main.py", "-platform", "offscreen"]


@pytest.mark.unit
def test_parse_dev_args_clinical_and_defaults():
    main = importlib.import_module("main")
    dev, qt_argv = main._parse_dev_args(["main.py", "--clinical"], frozen=False)
    assert dev["clinical"] is True
    assert dev["portable"] is None and dev["data_root"] is None
    assert qt_argv == ["main.py"]

    dev, qt_argv = main._parse_dev_args(["main.py"], frozen=False)
    assert dev == {
        "clinical": None, "portable": None, "data_root": None,
        "config_override": None, "ignored": None,
    }
    assert qt_argv == ["main.py"]


@pytest.mark.unit
def test_parse_dev_args_config_override_is_a_json_object():
    """--config-override (#546) replaces editing a config file for source
    runs — the HIL harness forces engineeringMode / clinicalMode with it."""
    main = importlib.import_module("main")
    dev, qt_argv = main._parse_dev_args(
        ["main.py", "--config-override", '{"engineeringMode": true, "tecTripTempC": 42}'],
        frozen=False,
    )
    assert dev["config_override"] == {"engineeringMode": True, "tecTripTempC": 42}
    assert qt_argv == ["main.py"]
    with pytest.raises(SystemExit):
        main._parse_dev_args(["main.py", "--config-override", "[1, 2]"], frozen=False)
    with pytest.raises(SystemExit):
        main._parse_dev_args(["main.py", "--config-override", "{nope"], frozen=False)


@pytest.mark.unit
def test_parse_dev_args_frozen_build_ignores_the_flags():
    """A packaged artifact's variant and data root are baked in at build time
    (#233); the flags must be dropped, not applied, and still kept away from
    Qt."""
    main = importlib.import_module("main")
    argv = ["Open-Motion.exe", "--clinical", "--portable", "--data-root", "X",
            "--config-override", '{"engineeringMode": true}']
    dev, qt_argv = main._parse_dev_args(argv, frozen=True)
    assert dev["clinical"] is None
    assert dev["portable"] is None
    assert dev["data_root"] is None
    assert dev["config_override"] is None
    assert dev["ignored"] == {
        "clinical": True, "portable": True, "data_root": "X",
        "config_override": {"engineeringMode": True},
    }
    assert qt_argv == ["Open-Motion.exe"]


@pytest.mark.unit
def test_pin_qt_environment_scrubs_host_knobs_and_pins_ours():
    main = importlib.import_module("main")
    env = {
        "QT_QPA_PLATFORM": "offscreen",
        "QT_SCALE_FACTOR": "2",
        "QT_QUICK_CONTROLS_STYLE": "Basic",
        "QT_QUICK_CONTROLS_MATERIAL_THEME": "Light",
        # Set by PyInstaller's runtime hook — must survive.
        "QT_PLUGIN_PATH": r"C:\bundle\PyQt6\Qt6\plugins",
        "QML2_IMPORT_PATH": r"C:\bundle\PyQt6\Qt6\qml",
        "PATH": r"C:\bundle",
        "UNRELATED": "keep",
    }
    removed = main._pin_qt_environment(env)

    assert set(removed) == {"QT_QPA_PLATFORM", "QT_SCALE_FACTOR"}
    assert "QT_QPA_PLATFORM" not in env and "QT_SCALE_FACTOR" not in env
    assert env["QT_QUICK_CONTROLS_STYLE"] == "Material"
    assert env["QT_QUICK_CONTROLS_MATERIAL_THEME"] == "Dark"
    assert env["QT_LOGGING_RULES"] == "qt.qpa.fonts=false"
    assert env["QT_PLUGIN_PATH"] == r"C:\bundle\PyQt6\Qt6\plugins"
    assert env["QML2_IMPORT_PATH"] == r"C:\bundle\PyQt6\Qt6\qml"
    assert env["PATH"] == r"C:\bundle"
    assert env["UNRELATED"] == "keep"


@pytest.mark.unit
def test_pin_qt_environment_is_idempotent_on_a_clean_env():
    main = importlib.import_module("main")
    env = {}
    assert main._pin_qt_environment(env) == []
    assert env == dict(main._QT_ENV_PINNED)


# --------------------------------------------------------------------------
# macOS is research-only (SDK refuses the scan-db keystore on darwin)
# --------------------------------------------------------------------------
#
# tmp_path is requested as a fixture so pytest materializes it during setup —
# resolving it *after* sys.platform is faked to "darwin" makes pytest take its
# POSIX branch and call os.getuid(), which does not exist on Windows.

def _config_with(tmp_path, monkeypatch, platform, stamped, **dev_flags):
    """``stamped`` plays the build's CLINICAL_MODE stamp."""
    _root(monkeypatch, tmp_path)
    _compiled(monkeypatch, **stamped)
    monkeypatch.setattr(sys, "platform", platform)
    main = importlib.import_module("main")
    return main, main._load_app_config(**dev_flags)


@pytest.mark.unit
def test_macos_forces_research_mode(tmp_path, monkeypatch):
    """clinicalMode drives require_encrypted_db, and the SDK refuses the
    keystore on darwin — so a clinical macOS session cannot start at all."""
    _main, cfg = _config_with(tmp_path, monkeypatch, "darwin", {"clinicalMode": True})
    assert cfg["clinicalMode"] is False


@pytest.mark.unit
def test_macos_forces_research_mode_in_the_baseline_too(tmp_path, monkeypatch):
    # the connector reads the baseline, so leaving it True would still hand a
    # clinical flag to everything downstream of the merged config
    main, _cfg = _config_with(tmp_path, monkeypatch, "darwin", {"clinicalMode": True})
    assert main._APP_CONFIG_BASELINE["clinicalMode"] is False


@pytest.mark.unit
def test_macos_beats_the_clinical_dev_flag(tmp_path, monkeypatch):
    """--clinical normally wins over the config. "macOS is never clinical" is
    absolute, so the platform gate has to win over it in turn — otherwise the
    flag just produces a crash instead of a research build."""
    _main, cfg = _config_with(tmp_path, monkeypatch, "darwin", {}, clinical=True)
    assert cfg["clinicalMode"] is False


@pytest.mark.unit
def test_non_macos_still_honors_clinical_mode(tmp_path, monkeypatch):
    # the gate must not leak off darwin — Windows is the clinical platform
    _main, cfg = _config_with(tmp_path, monkeypatch, "win32", {"clinicalMode": True})
    assert cfg["clinicalMode"] is True


@pytest.mark.unit
def test_non_macos_still_honors_the_clinical_dev_flag(tmp_path, monkeypatch):
    _main, cfg = _config_with(
        tmp_path, monkeypatch, "win32", {"clinicalMode": False}, clinical=True
    )
    assert cfg["clinicalMode"] is True
