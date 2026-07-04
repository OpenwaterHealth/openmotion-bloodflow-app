"""PulseSink + PulseDemoController (pulse_view.py).

Unit-marked: no app launch, no hardware. The conftest autouse fixtures
short-circuit on the unit marker and provide a bare QCoreApplication for the
QObject/QTimer machinery.
"""

import numpy as np
import pytest

from omotion.pulse.types import PulseAnalysis, PulseFeatures
from omotion.pulse.synth import synth_bfi
from pulse_view import PulseSink, PulseDemoController, load_bfi_results_csv

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


def test_load_bfi_results_csv(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text(
        "camera,side,time_s,BFI,BVI\n"
        "0,left,0.0,4.5,7.0\n"
        "1,left,0.0,5.5,7.5\n"
        "0,right,0.0,4.0,8.0\n"
        "0,left,0.025,4.6,7.1\n")
    left, right = load_bfi_results_csv(str(p))
    assert left.shape[0] == 2                 # two distinct timepoints, left
    assert right.shape[0] == 1
    assert abs(float(left[0]) - 5.0) < 1e-9   # per-side mean of cams: (4.5+5.5)/2


def test_demo_replay_streams_provided_data():
    seen = []
    ctrl = PulseDemoController(emit_fn=seen.append, step=4)
    _, lv = synth_bfi(duration_s=16.0, fs=40.0, bpm=72.0, seed=1)
    _, rv = synth_bfi(duration_s=16.0, fs=40.0, bpm=100.0, seed=2)
    ctrl.start_replay(lv, rv)
    assert ctrl.running is True
    for _ in range(240):
        ctrl._tick()
    ctrl.stop()
    assert {s.side for s in seen} == {"left", "right"}
    left = [s for s in seen if s.side == "left"][-1]
    assert 66.0 <= left.features.hr_bpm <= 78.0   # recovered the 72 bpm replay
    assert left.features.reliable is True          # replayed a real pulse


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
