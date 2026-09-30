"""Regression tests for the Scan History duration column (#625).

History must show the same whole seconds as the session-notes
"duration:" line, which formats the same actual_duration_sec by
truncating (int()), so 59.6 s reads 0:59 in both places, never 0:60.
"""

import json
import re
from pathlib import Path

import pytest
from PyQt6.QtQml import QJSEngine

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
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
        (59.6, "0:59"),
        (59.999, "0:59"),
        (60, "1:00"),
        (119.7, "1:59"),
        (3600.5, "60:00"),
        (-1, "—"),
        (None, "—"),
    ],
)
def test_format_duration_truncates_like_session_notes(
    format_duration, seconds, expected
):
    assert format_duration(seconds) == expected
