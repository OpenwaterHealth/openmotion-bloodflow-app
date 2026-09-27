"""Regression tests for Scan History duration rounding."""

import json
import re
import sys
from pathlib import Path

import pytest
from PyQt6.QtQml import QJSEngine

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
HISTORY_QML = REPO_ROOT / "components" / "HistoryModal.qml"


def _format_duration_function(source: str) -> str:
    match = re.search(
        r"(?m)^[ \t]*function formatDuration\(sec\)\s*\{", source
    )
    assert match is not None, (
        "HistoryModal.qml must define formatDuration(sec)"
    )

    start = source.index("{", match.start())
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[match.start():index + 1]

    raise AssertionError("formatDuration(sec) has an unclosed function body")


@pytest.fixture(scope="module")
def format_duration():
    source = HISTORY_QML.read_text(encoding="utf-8")
    function = _format_duration_function(source)
    engine = QJSEngine()

    def render(seconds):
        result = engine.evaluate(
            f"{function}\nformatDuration({json.dumps(seconds)})"
        )
        assert not result.isError(), result.toString()
        return result.toString()

    return render


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0:00"),
        (59.4, "0:59"),
        (59.6, "1:00"),
        (119.7, "2:00"),
        (-1, "—"),
        (None, "—"),
    ],
)
def test_format_duration_rounds_total_seconds(
    format_duration, seconds, expected
):
    assert format_duration(seconds) == expected
