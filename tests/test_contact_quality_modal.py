from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_contact_quality_warnings_take_precedence_over_generic_failure():
    qml = Path(__file__).resolve().parents[1] / "pages" / "BloodFlow.qml"
    text = qml.read_text(encoding="utf-8")
    handler_start = text.index("function onContactQualityCheckFinished")
    handler_end = text.index("// Live-scan warnings", handler_start)
    handler = text[handler_start:handler_end]

    warning_branch = handler.index("warnings.length")
    generic_failure_branch = handler.index("if (!ok)")

    assert warning_branch < generic_failure_branch
