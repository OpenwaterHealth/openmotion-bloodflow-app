"""
Regression test for issue #228 — "Remove switch for BVI low pass fillter".

The BVI low-pass filter was implemented inside the legacy plot components
(EmbeddedRealtimePlot.qml / ReducedPlotView.qml, added in 37f7dc9) and was
deleted along with them in the Phase 5 legacy-plot cleanup (4a2d844,
June 2026) — the current PlotViewer never had an LPF. The Settings switch
outlived the filter as dead UI, silently persisting
``bviLowPassEnabled`` / ``bviLowPassCutoffHz`` keys that nothing read.

These tests pin the cleanup: no QML references the keys (the switch and
its load/save glue are gone) and the orphaned keys stay out of the
main.py defaults and the shipped config. If the filter is ever
reinstated, it needs a fresh implementation in the live plot path — at
which point these pins should be replaced with behavioral tests.

Unit-marked: pure source/config inspection, no app launch, no Qt.
"""

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_no_qml_references_bvi_low_pass():
    offenders = [
        str(p.relative_to(REPO_ROOT))
        for p in REPO_ROOT.rglob("*.qml")
        if "bviLowPass" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        "bviLowPass* referenced in QML — the dead Settings switch (or its "
        f"load/save glue) is back: {offenders}"
    )


def test_main_defaults_carry_no_bvi_low_pass_keys():
    src = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    assert "bviLowPass" not in src, (
        "main.py still declares bviLowPass* defaults — the keys are "
        "orphaned (nothing applies the filter) and must stay removed"
    )


def test_shipped_config_carries_no_bvi_low_pass_keys():
    with open(REPO_ROOT / "config" / "app_config.json",
              encoding="utf-8") as f:
        cfg = json.load(f)
    stray = [k for k in cfg if k.startswith("bviLowPass")]
    assert stray == [], (
        f"shipped config/app_config.json still carries orphaned keys: "
        f"{stray}"
    )
