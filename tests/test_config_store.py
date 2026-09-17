"""Unit tests for the compiled, tiered app config (#546).

There is no configuration file any more: ``config/app_config.py`` compiles
the shipped values in, each key in exactly one tier, and the only persisted
layer is the ``settings`` table in scans.db (PREFERENCE / STATE keys only).
"""
import json

import pytest

from config import app_config as compiled
from utils import config_store

pytestmark = pytest.mark.unit


# ── the compiled module ─────────────────────────────────────────────────

def test_every_key_has_exactly_one_tier():
    tiers = (compiled.CONSTANT_KEYS, compiled.SESSION_KEYS,
             compiled.PREFERENCE_KEYS, compiled.STATE_KEYS)
    union = set().union(*tiers)
    assert union == set(compiled.APP_CONFIG)
    assert sum(len(t) for t in tiers) == len(union)          # pairwise disjoint
    for key in compiled.APP_CONFIG:
        assert compiled.tier_of(key) in (
            compiled.CONSTANT, compiled.SESSION, compiled.PREFERENCE, compiled.STATE)
    assert compiled.tier_of("noSuchKey") is None


def test_compiled_config_is_a_copy():
    a = config_store.compiled_config()
    a["leftMask"] = 0
    a["altCameraGains"].append(99)
    b = config_store.compiled_config()
    assert b["leftMask"] == compiled.APP_CONFIG["leftMask"] == 102
    assert b["altCameraGains"] == [16, 4, 2, 1, 1, 2, 4, 16]


def test_build_variant_keys_are_constants():
    """#233 carried forward: the Clinical/Research split and the install
    layout are never runtime-overridable."""
    assert compiled.tier_of("clinicalMode") == compiled.CONSTANT
    assert compiled.tier_of("portableMode") == compiled.CONSTANT
    assert compiled.APP_CONFIG["clinicalMode"] is compiled.CLINICAL_MODE
    assert compiled.CLINICAL_MODE is False   # repo value = Research default


def test_engineering_and_debug_flags_are_session_only():
    """Everything the engineering interface can change resets at launch:
    the unlock is per session, so nothing it enables may outlive it."""
    for key in ("engineeringMode", "forceLaserFail", "debugHistoStallTest",
                "histoCmp", "sensorDebugLogging", "consoleDebugLogging",
                "altCameraSettingsEnabled", "altLaserPulseWidthEnabled",
                "writeTelemetryCsv", "downloadBetaUpdates"):
        assert compiled.tier_of(key) == compiled.SESSION, key
        assert not config_store.is_persisted(key), key


def test_display_preferences_persist():
    for key in ("leftMask", "rightMask", "bfiMin", "bfiMax", "plotWindowSec",
                "darkMode", "liquidGlass", "showAxisLabels", "writeRawCsv",
                "autoScale", "autoScalePerPlot", "bviLowPassEnabled"):
        assert compiled.tier_of(key) == compiled.PREFERENCE, key
        assert config_store.is_persisted(key), key


def test_restore_flags_are_persisted_state():
    for key in ("altCameraSettingsDirty", "altLaserPulseWidthDirty",
                "altLaserSafetyCeilingDirty"):
        assert compiled.tier_of(key) == compiled.STATE, key
        assert compiled.APP_CONFIG[key] is False


def test_retired_keys_are_gone():
    """cameraFakeData (dead firmware path) and uncorrectedOnly (read into an
    attribute nothing consumed) were dropped with the JSON."""
    assert "cameraFakeData" not in compiled.APP_CONFIG
    assert "uncorrectedOnly" not in compiled.APP_CONFIG


def test_updater_source_and_smtp_are_compiled_constants():
    """An overridable update endpoint / SMTP block was an attack surface in
    the plaintext overrides file; both are compiled in now."""
    for key in ("updateRepo", "updateApiUrl", "bug_report_smtp"):
        assert compiled.tier_of(key) == compiled.CONSTANT, key
        assert compiled.APP_CONFIG[key] is None


# ── dev overrides ───────────────────────────────────────────────────────

def test_apply_dev_overrides_applies_known_and_ignores_unknown():
    cfg = config_store.compiled_config()
    applied = config_store.apply_dev_overrides(
        cfg, {"engineeringMode": True, "tecTripTempC": 42, "bogus": 1})
    assert applied == {"engineeringMode", "tecTripTempC"}
    assert cfg["engineeringMode"] is True
    assert cfg["tecTripTempC"] == 42
    assert "bogus" not in cfg
    assert config_store.apply_dev_overrides(cfg, None) == set()


# ── saved preferences ───────────────────────────────────────────────────

def test_apply_saved_preferences_uses_only_persisted_tiers():
    cfg = config_store.compiled_config()
    used = config_store.apply_saved_preferences(cfg, {
        "bfiMax": 5.0,                  # preference: applied
        "altCameraSettingsDirty": True, # state: applied
        "engineeringMode": True,        # session: ignored
        "tecTripTempC": 1,              # constant: ignored
        "clinicalMode": True,           # constant: ignored
        "retiredKey": 7,                # unknown: ignored
    })
    assert used == {"bfiMax", "altCameraSettingsDirty"}
    assert cfg["bfiMax"] == 5.0
    assert cfg["altCameraSettingsDirty"] is True
    assert cfg["engineeringMode"] is False
    assert cfg["tecTripTempC"] == 40
    assert cfg["clinicalMode"] is False
    assert "retiredKey" not in cfg


def test_apply_saved_preferences_coerces_masks_to_int():
    cfg = config_store.compiled_config()
    config_store.apply_saved_preferences(cfg, {"leftMask": 195.0})
    assert cfg["leftMask"] == 195 and isinstance(cfg["leftMask"], int)


# ── persistable diff (what _save_app_config writes) ─────────────────────

def test_persistable_diff_saves_changed_and_deletes_restored():
    baseline = config_store.compiled_config()
    current = dict(baseline)
    current["bfiMax"] = 5.0                     # preference changed → save
    current["engineeringMode"] = True           # session → never
    current["tecTripTempC"] = 99                # constant → never
    current["altCameraSettingsDirty"] = True    # state changed → save
    to_save, to_delete = config_store.persistable_diff(current, baseline)
    assert to_save == {"bfiMax": 5.0, "altCameraSettingsDirty": True}
    assert "engineeringMode" not in to_save and "tecTripTempC" not in to_save
    assert "leftMask" in to_delete             # unchanged preference → row cleared
    assert "engineeringMode" not in to_delete and "tecTripTempC" not in to_delete
    assert set(to_save) | set(to_delete) == set(compiled.PERSISTED_KEYS)


def test_refused_keys_are_the_constants():
    assert config_store.refused_keys(["tecTripTempC", "bfiMax", "engineeringMode"]) == ["tecTripTempC"]
    assert config_store.refused_keys(["nope"]) == []


def test_config_dir_env_var_is_ignored(monkeypatch, tmp_path):
    """The retired OPENWATER_CONFIG_DIR override cannot redirect anything:
    there is no file to redirect. The compiled value is the value."""
    monkeypatch.setenv("OPENWATER_CONFIG_DIR", str(tmp_path))
    (tmp_path / "app_config.json").write_text(json.dumps({"bfiMax": 99.0}), encoding="utf-8")
    assert config_store.compiled_config()["bfiMax"] == 10.0


def test_no_legacy_import_surface():
    """A pre-#546 app_config.local.json is ignored outright: there is no
    importer to smuggle values through (decision 2026-09-16)."""
    assert not hasattr(config_store, "import_legacy_overrides")
    assert not hasattr(config_store, "remove_legacy_overrides")
    from utils import app_paths
    assert not hasattr(app_paths, "local_config_path")
