"""Unit tests for _load_app_config resolution — issues #154, #473, #546.

Since #546 the shipped values are compiled in (``config/app_config.py``):
there is no file to be missing, corrupt or carry unknown keys, so those
resolution paths are gone. What remains to pin: the compiled values the
clinical flow depends on, the dev-only launch overrides, and the tombstones
for flags that were removed and must never come back.
"""

from pathlib import Path

import pytest

import main as app_main
from config import app_config as compiled

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_load_returns_the_compiled_values():
    cfg = app_main._load_app_config()
    assert cfg["leftMask"] == 0x66
    assert cfg["engineeringMode"] is False
    assert cfg["clinicalMode"] is compiled.CLINICAL_MODE
    # portableMode is derived, never None once loaded
    assert cfg["portableMode"] in (True, False)
    assert set(cfg) == set(compiled.APP_CONFIG)


def test_dev_overrides_are_applied_and_recorded():
    cfg = app_main._load_app_config(overrides={"engineeringMode": True, "nope": 1})
    assert cfg["engineeringMode"] is True
    assert "nope" not in cfg
    assert app_main._DEV_CONFIG_KEYS == {"engineeringMode"}
    # the baseline the connector diffs against carries the dev value too, so
    # a forced preference is never persisted as if the operator chose it
    assert app_main._APP_CONFIG_BASELINE["engineeringMode"] is True


def test_dev_flags_do_not_leak_between_loads():
    app_main._load_app_config(overrides={"engineeringMode": True})
    cfg = app_main._load_app_config()
    assert cfg["engineeringMode"] is False
    assert app_main._DEV_CONFIG_KEYS == set()


def test_auto_configure_flag_is_gone():
    """Tombstone for issue #154: the startup auto-flash flag must not
    come back — not in the compiled config, not in any source file."""
    assert "autoConfigureOnStartup" not in compiled.APP_CONFIG

    offenders = []
    for pattern in ("*.py", "*.qml"):
        for path in REPO_ROOT.rglob(pattern):
            rel = path.relative_to(REPO_ROOT)
            if rel == Path("tests") / "test_app_config_defaults.py":
                continue
            skip_dirs = (
                ".venv", "venv", ".git", ".claude",
                "build", "dist", "__pycache__",
            )
            if any(part in skip_dirs for part in rel.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "autoConfigureOnStartup" in text:
                offenders.append(str(rel))
    assert offenders == []


def test_tec_trip_temp_is_compiled_nonzero():
    """A shipped 0 would disable the firmware over-temp trip entirely."""
    assert compiled.APP_CONFIG["tecTripTempC"] == 40
    assert app_main._load_app_config()["tecTripTempC"] == 40


def test_connection_timeout_is_twelve_seconds():
    """The startup connection watchdog fires 12 s after launch. It owns the
    E-104/E-106 toast and (research builds) the sample-dataset offer."""
    assert compiled.APP_CONFIG["connectionTimeoutSec"] == 12


def test_beta_updates_default_off_and_old_key_retired():
    assert compiled.APP_CONFIG["downloadBetaUpdates"] is False
    assert "downloadBetaFirmware" not in compiled.APP_CONFIG


def test_ft_thresholds_are_the_sdk_factory_values():
    """The compiled ft_* thresholds must be the SDK factory acceptance
    values — never zeros. A zero min-mean/min-contrast can never fail
    (both quantities are non-negative), which disabled the SDK's
    calibration pre-write gate outright (#473)."""
    from omotion import factory_calibration_thresholds

    f = factory_calibration_thresholds()
    cfg = compiled.APP_CONFIG
    assert cfg["ft_min_mean_per_camera"] == list(f.min_mean_per_camera)
    assert cfg["ft_min_contrast_per_camera"] == list(f.min_contrast_per_camera)
    assert cfg["ft_min_bfi_per_camera"] == list(f.min_bfi_per_camera)
    assert cfg["ft_max_bfi_per_camera"] == list(f.max_bfi_per_camera)
    assert cfg["ft_min_bvi_per_camera"] == list(f.min_bvi_per_camera)
    assert cfg["ft_max_bvi_per_camera"] == list(f.max_bvi_per_camera)
    assert cfg["ft_max_dark_per_camera"] == list(f.max_dark_per_camera)
    assert all(v > 0 for v in cfg["ft_min_mean_per_camera"])
    assert all(v > 0 for v in cfg["ft_min_contrast_per_camera"])


def test_no_config_json_ships():
    """The whole point of #546: nothing editable next to the executable."""
    assert not (REPO_ROOT / "config" / "app_config.json").exists()
    assert not (REPO_ROOT / "config" / "tec_params.json").exists()
    assert (REPO_ROOT / "config" / "app_config.py").exists()
