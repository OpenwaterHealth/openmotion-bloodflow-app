"""Unit tests for the boot-time default scan preset — issue #314.

When the app boots and no device has been detected, the scan page loads
a single named default scan configuration (camera masks + duration) so
it comes up in a defined ready state. Nothing is replayed and Start
stays gated on device readiness — the preset only shapes the page's
initial scan settings.

The preset lives in ONE place — ``config/default_scan.json`` — so the
placeholder contents can be swapped for the real ones later with a
one-file edit. ``motion_config.load_default_scan_preset`` loads and
validates it (per-key fallback on bad values, whole-file fallback on a
missing/corrupt file) and the connector exposes it to QML as
``MotionInterface.defaultScanPreset``.

Unit-marked: no app launch, no hardware.
"""

import json
from pathlib import Path

import pytest

from motion_config import DEFAULT_SCAN_PRESET, load_default_scan_preset
from motion_connector import MotionConnector

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── Loader: file resolution + fallbacks ──────────────────────────────────────


def test_missing_file_falls_back_to_builtin(tmp_path):
    preset = load_default_scan_preset(str(tmp_path))
    assert preset == DEFAULT_SCAN_PRESET
    assert preset is not DEFAULT_SCAN_PRESET  # caller gets a copy


def test_valid_file_values_are_adopted(tmp_path):
    (tmp_path / "default_scan.json").write_text(
        json.dumps({
            "name": "Bench Preset",
            "leftMask": 0x66,
            "rightMask": 0xC3,
            "freeRun": True,
            "durationSec": 300,
        }),
        encoding="utf-8",
    )
    preset = load_default_scan_preset(str(tmp_path))
    assert preset["name"] == "Bench Preset"
    assert preset["leftMask"] == 0x66
    assert preset["rightMask"] == 0xC3
    assert preset["freeRun"] is True
    assert preset["durationSec"] == 300


def test_corrupt_json_falls_back_to_builtin(tmp_path):
    (tmp_path / "default_scan.json").write_text(
        "{not valid json", encoding="utf-8"
    )
    assert load_default_scan_preset(str(tmp_path)) == DEFAULT_SCAN_PRESET


def test_non_object_json_falls_back_to_builtin(tmp_path):
    (tmp_path / "default_scan.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert load_default_scan_preset(str(tmp_path)) == DEFAULT_SCAN_PRESET


def test_partial_file_merges_builtin_for_missing_keys(tmp_path):
    (tmp_path / "default_scan.json").write_text(
        json.dumps({"leftMask": 0x0F}), encoding="utf-8"
    )
    preset = load_default_scan_preset(str(tmp_path))
    assert preset["leftMask"] == 0x0F
    assert preset["rightMask"] == DEFAULT_SCAN_PRESET["rightMask"]
    assert preset["freeRun"] == DEFAULT_SCAN_PRESET["freeRun"]
    assert preset["durationSec"] == DEFAULT_SCAN_PRESET["durationSec"]
    assert preset["name"] == DEFAULT_SCAN_PRESET["name"]


@pytest.mark.parametrize(
    "bad",
    [
        {"leftMask": 999},          # out of 0-255 range
        {"leftMask": -1},
        {"leftMask": "0xFF"},       # strings are not coerced
        {"leftMask": True},         # bool is not a mask
        {"rightMask": 4096},
        {"freeRun": "yes"},         # non-bool
        {"durationSec": 0},         # zero-length scan is invalid
        {"durationSec": -5},
        {"durationSec": True},
        {"name": 42},               # non-string
    ],
    ids=lambda bad: json.dumps(bad),
)
def test_invalid_values_fall_back_per_key(tmp_path, bad):
    (tmp_path / "default_scan.json").write_text(
        json.dumps(bad), encoding="utf-8"
    )
    preset = load_default_scan_preset(str(tmp_path))
    key = next(iter(bad))
    assert preset[key] == DEFAULT_SCAN_PRESET[key]


def test_invalid_key_does_not_poison_valid_siblings(tmp_path):
    (tmp_path / "default_scan.json").write_text(
        json.dumps({"leftMask": 999, "rightMask": 0x5A}), encoding="utf-8"
    )
    preset = load_default_scan_preset(str(tmp_path))
    assert preset["leftMask"] == DEFAULT_SCAN_PRESET["leftMask"]
    assert preset["rightMask"] == 0x5A


def test_default_config_dir_resolves_via_resource_path():
    """config_dir='config' (the connector default) must resolve through
    resource_path so the preset is found inside a PyInstaller bundle."""
    preset = load_default_scan_preset("config")
    assert preset == load_default_scan_preset(str(REPO_ROOT / "config"))


# ── Shipped file sanity ──────────────────────────────────────────────────────


def test_shipped_preset_file_is_valid():
    """The repo's config/default_scan.json must parse and hold sane values
    — it ships in the PyInstaller bundle (openwater.spec bundles config/)."""
    shipped = REPO_ROOT / "config" / "default_scan.json"
    assert shipped.exists(), "config/default_scan.json missing from repo"
    with open(shipped, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data["name"], str) and data["name"]
    assert isinstance(data["leftMask"], int) and 0 <= data["leftMask"] <= 0xFF
    assert isinstance(data["rightMask"], int) and 0 <= data["rightMask"] <= 0xFF
    assert isinstance(data["freeRun"], bool)
    assert isinstance(data["durationSec"], int) and data["durationSec"] > 0


def test_shipped_preset_matches_builtin_fallback():
    """Shipped file and in-code fallback must agree, so a missing/corrupt
    file in the field degrades to the same preset the build shipped with."""
    shipped = REPO_ROOT / "config" / "default_scan.json"
    with open(shipped, "r", encoding="utf-8") as f:
        assert json.load(f) == DEFAULT_SCAN_PRESET


def test_shipped_preset_has_no_bom():
    raw = (REPO_ROOT / "config" / "default_scan.json").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), (
        "config/default_scan.json has a UTF-8 BOM — plain json.load in a "
        "strict decoder path would reject it"
    )


# ── Connector exposure ───────────────────────────────────────────────────────


def test_connector_exposes_default_scan_preset():
    """QML reads MotionInterface.defaultScanPreset; the property must
    return the dict loaded at __init__ into _default_scan_preset."""
    conn = MotionConnector.__new__(MotionConnector)
    conn._default_scan_preset = {"leftMask": 0x42}
    assert conn.defaultScanPreset == {"leftMask": 0x42}


def test_connector_init_loads_preset_from_config_dir():
    """__init__ must populate _default_scan_preset via
    load_default_scan_preset(config_dir) — pinned at source level because
    constructing a full MotionConnector wires hardware/telemetry."""
    src = (REPO_ROOT / "motion_connector.py").read_text(encoding="utf-8")
    assert "load_default_scan_preset" in src
    assert (
        "self._default_scan_preset = load_default_scan_preset(config_dir)"
        in src
    )


# ── QML wiring pins (text-based, like test_contact_quality_modal) ────────────


def _bloodflow_oncompleted_block() -> str:
    qml = REPO_ROOT / "pages" / "BloodFlow.qml"
    text = qml.read_text(encoding="utf-8")
    start = text.index("Component.onCompleted:")
    end = text.index("Component.onDestruction:", start)
    return text[start:end]


def test_bloodflow_applies_preset_at_boot():
    block = _bloodflow_oncompleted_block()
    assert "MotionInterface.defaultScanPreset" in block


def test_preset_applies_before_clinical_forcing():
    """Clinical mode's forced FDA configuration must win: the preset
    application has to come BEFORE the ``if (clinicalMode)`` block so the
    clinical branch overwrites it, leaving clinical boot state unchanged."""
    block = _bloodflow_oncompleted_block()
    preset_at = block.index("MotionInterface.defaultScanPreset")
    clinical_at = block.index("if (clinicalMode)")
    assert preset_at < clinical_at


def test_preset_does_not_mark_masks_initial_applied():
    """The preset must NOT set _leftMaskInitialApplied /
    _rightMaskInitialApplied — device connect must still adopt the config
    default masks exactly as before (issue #127 logic)."""
    block = _bloodflow_oncompleted_block()
    assert "_leftMaskInitialApplied" not in block
    assert "_rightMaskInitialApplied" not in block


def test_scan_settings_open_seeds_duration_from_page():
    """Opening Scan Settings must seed the modal's duration state from the
    page's current freeRun/durationSec — otherwise open+close silently
    resets a preset duration to the modal's hardcoded 1:00:00 default."""
    qml = (REPO_ROOT / "pages" / "BloodFlow.qml").read_text(encoding="utf-8")
    handler_start = qml.index("onScanSettingsClicked:")
    handler_end = qml.index("onNotesClicked:", handler_start)
    handler = qml[handler_start:handler_end]
    assert "setInitialDuration" in handler

    modal = (REPO_ROOT / "components" / "ScanSettingsModal.qml").read_text(
        encoding="utf-8"
    )
    assert "function setInitialDuration" in modal
