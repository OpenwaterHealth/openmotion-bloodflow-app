"""PulseSink + PulseDemoController (pulse_view.py).

Unit-marked: no app launch, no hardware. The conftest autouse fixtures
short-circuit on the unit marker and provide a bare QCoreApplication for the
QObject/QTimer machinery.
"""

import numpy as np
import pytest

from omotion.pulse.types import PulseAnalysis, PulseFeatures
from pulse_view import PulseSink, PulseDemoController

pytestmark = pytest.mark.unit


def _fake_analysis(side="left", beats=3):
    bins = 60
    return PulseAnalysis(
        side=side,
        phase=np.linspace(0.0, 1.0, bins, endpoint=False),
        template=np.zeros(bins), env_min=np.zeros(bins), env_max=np.zeros(bins),
        env_p25=np.zeros(bins), env_p75=np.zeros(bins),
        live_phase=np.zeros(0), live_value=np.zeros(0),
        features=PulseFeatures(hr_bpm=72.0), beat_count=beats,
    )


# ── PulseSink ──────────────────────────────────────────────────────────────

def test_pulse_sink_subscribes_to_pulse_channel():
    assert PulseSink.channels == {"pulse"}


def test_pulse_sink_forwards_pulse_payload():
    seen = []
    sink = PulseSink(emit_fn=seen.append)
    pa = _fake_analysis(side="right")
    sink.consume("pulse", pa)
    assert len(seen) == 1
    assert seen[0].side == "right"


def test_pulse_sink_ignores_other_channels():
    seen = []
    sink = PulseSink(emit_fn=seen.append)
    sink.consume("live", object())
    assert seen == []


def test_pulse_sink_has_lifecycle_hooks():
    sink = PulseSink(emit_fn=lambda x: None)
    sink.on_scan_start(object())     # must not raise
    sink.on_complete()               # must not raise


# ── PulseDemoController ──────────────────────────────────────────────────────

def test_demo_emits_snapshots_for_both_sides():
    seen = []
    ctrl = PulseDemoController(emit_fn=seen.append, step=2)
    ctrl.start(preset="normal", bpm=72.0)
    for _ in range(400):             # ~800 samples pushed
        ctrl._tick()
    ctrl.stop()
    sides = {s.side for s in seen}
    assert sides == {"left", "right"}


def test_demo_recovers_preset_heart_rate():
    seen = []
    ctrl = PulseDemoController(emit_fn=seen.append, step=2)
    ctrl.start(preset="normal", bpm=90.0)
    for _ in range(500):
        ctrl._tick()
    ctrl.stop()
    last_left = [s for s in seen if s.side == "left"][-1]
    assert 84.0 <= last_left.features.hr_bpm <= 96.0


def test_demo_running_flag_tracks_start_stop():
    ctrl = PulseDemoController(emit_fn=lambda x: None)
    assert ctrl.running is False
    ctrl.start(preset="normal")
    assert ctrl.running is True
    ctrl.stop()
    assert ctrl.running is False


def test_demo_stop_halts_emission():
    seen = []
    ctrl = PulseDemoController(emit_fn=seen.append, step=2)
    ctrl.start(preset="normal")
    for _ in range(50):
        ctrl._tick()
    ctrl.stop()
    n = len(seen)
    for _ in range(50):
        ctrl._tick()                 # ticks after stop are no-ops
    assert len(seen) == n
