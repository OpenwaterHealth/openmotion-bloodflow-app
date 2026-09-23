"""The camera-pattern presets live in three hand-kept copies (#451).

ScanSettingsModal.qml has the scan-time dropdown (a ListModel of masks plus
a parallel ``applyPatternToSensor`` table of per-camera booleans),
SettingsModal.qml has the default-mask dropdown, and motion_connector.py
maps saved masks back to names for History. When Far moved to 0xC3
(rows 3 and 4) Near was left at 0x5A, which selects rows 1 and 3. This
test keeps every copy in agreement and pins both presets to their rows.

Row numbering follows the sensor diagram (SensorView.qml) counted from the
laser: row 1 = mask bits 3/4, row 2 = 2/5, row 3 = 1/6, row 4 = 0/7.
"""

import re
from pathlib import Path

import pytest

from motion_connector import _CONFIG_NAMES

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_SETTINGS = REPO_ROOT / "components" / "ScanSettingsModal.qml"
SETTINGS = REPO_ROOT / "components" / "SettingsModal.qml"

ROW_INDICES = {1: (3, 4), 2: (2, 5), 3: (1, 6), 4: (0, 7)}


def _rows_mask(*rows):
    return sum(1 << i for r in rows for i in ROW_INDICES[r])


def _list_model(path):
    """Ordered (name, mask) pairs of the file's pattern ListModel, minus
    the mask-less Custom entry."""
    text = path.read_text(encoding="utf-8")
    pairs = re.findall(
        r'ListElement \{ name: "([^"]+)";\s*maskHex: "(0x[0-9A-Fa-f]+)" \}', text)
    assert pairs, f"no pattern ListModel found in {path.name}"
    return [(name, int(mask, 16)) for name, mask in pairs]


def _apply_table():
    """case index -> mask from applyPatternToSensor's boolean arrays,
    using maskFromArray's mapping (array slot i is mask bit 7 - i)."""
    text = SCAN_SETTINGS.read_text(encoding="utf-8")
    table = {}
    for idx, arr in re.findall(r"case (\d+): pattern = \[([^\]]*)\]", text):
        bits = [b.strip() == "true" for b in arr.split(",")]
        assert len(bits) == 8
        table[int(idx)] = sum(1 << (7 - i) for i, on in enumerate(bits) if on)
    return table


def test_near_is_rows_1_and_2_far_is_rows_3_and_4():
    patterns = dict(_list_model(SCAN_SETTINGS))
    assert patterns["Near"] == _rows_mask(1, 2) == 0x3C
    assert patterns["Far"] == _rows_mask(3, 4) == 0xC3


def test_settings_dropdown_matches_scan_settings_dropdown():
    assert _list_model(SETTINGS) == _list_model(SCAN_SETTINGS)


def test_apply_table_matches_list_model():
    table = _apply_table()
    for idx, (name, mask) in enumerate(_list_model(SCAN_SETTINGS)):
        assert table.get(idx) == mask, f"{name}: case {idx} disagrees with maskHex"


def test_history_names_match_list_model():
    assert {mask: name for name, mask in _list_model(SCAN_SETTINGS)} == _CONFIG_NAMES
