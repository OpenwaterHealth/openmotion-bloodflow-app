"""Source-text guards on the pre-scan contact-quality result handler.

QML is not instantiated here — these read pages/BloodFlow.qml as text, which
is the cheapest thing that still catches the two regressions this handler has
actually suffered.

History worth keeping: the original guard asserted only that the warnings
branch appeared at a lower string offset than ``if (!ok)``. That expressed
"show the specific warnings, not a generic error" (8b7892c), but the way it
was written was also satisfied by the warnings branch returning
unconditionally — which is exactly what made ``ok`` unreachable and left the
preflight verdict discarded for the whole reliability campaign (#390). The
assertion and its intent had drifted apart, and the defect lived in the gap.
Both properties are now pinned explicitly.
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[1]


def _handler() -> str:
    """The handler body with // comments stripped.

    Stripping matters: these assertions look for identifiers, and a comment
    explaining why a call is *not* made would otherwise read as the call
    itself.
    """
    text = (_REPO / "pages" / "BloodFlow.qml").read_text(encoding="utf-8")
    start = text.index("function onContactQualityCheckFinished")
    end = text.index("// Live-scan warnings", start)
    return "\n".join(
        line.split("//")[0] for line in text[start:end].splitlines()
    )


def test_contact_quality_warnings_take_precedence_over_generic_failure():
    """A failing check with per-camera detail must show that detail, not
    replace it with "Quick check failed" (8b7892c)."""
    handler = _handler()

    warning_branch = handler.index("warnings.length")
    generic_failure_branch = handler.index("if (!ok)")

    assert warning_branch < generic_failure_branch
    assert "showError" not in handler[warning_branch:generic_failure_branch], (
        "the warnings branch falls through into the generic error, which "
        "would replace the per-camera detail with 'Quick check failed'"
    )


def test_preflight_verdict_is_not_discarded():
    """``ok`` is the SDK's pass/fail verdict. It reached QML and was dropped
    on every path — the whole of #390. It must be recorded on the modal."""
    handler = _handler()

    assert "checkPassed = ok" in handler, (
        "the contact-quality verdict is computed, emitted, and then thrown "
        "away — nothing downstream can tell a passing check from a failing "
        "one (#390)"
    )


def test_modal_exposes_the_verdict():
    """The property the handler writes has to exist, and default to the
    safe value for a modal that has not run a check yet."""
    modal = (_REPO / "components" / "ContactQualityModal.qml").read_text(
        encoding="utf-8"
    )

    assert "property bool checkPassed" in modal
