"""
Issue #228 — config-only BVI low-pass filter; Settings switch removed.

History: the original 1-pole BVI LPF lived inside the legacy plot
components (EmbeddedRealtimePlot/ReducedPlotView, 37f7dc9) and died with
them in the Phase 5 cleanup (4a2d844) — the Settings switch outlived it
as dead UI. #228 removes the switch and reintroduces the filter in the
current display path, governed ONLY by the ``bviLowPassCutoffHz`` config
key (no enabled bool, no UI):

  - key missing or invalid  → default 20.0 Hz
  - key <= 0                → filter off (documented escape hatch)

The filter runs at LiveScanSource ingest (display-side only): live
PlotViewer traces and the clinical side averages (cam_id -1) are
smoothed; scans.db, CSVs, and replay stay raw. Discretization matches
the legacy QML filter: alpha = dt / (RC + dt), RC = 1/(2*pi*fc),
dt = 1/40 s (nominal live sample rate).

Unit-marked: no app launch, no hardware.
"""

import json
import math
from pathlib import Path

import pytest

from data_sources import (
    BVI_LPF_DEFAULT_CUTOFF_HZ,
    LiveScanSource,
    bvi_lpf_alpha,
    resolve_bvi_lpf_cutoff,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── Settings switch stays gone ──────────────────────────────────────────


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


def test_enabled_bool_stays_gone():
    """The filter is governed by the cutoff number alone — no bool key."""
    assert "bviLowPassEnabled" not in (
        (REPO_ROOT / "main.py").read_text(encoding="utf-8"))
    with open(REPO_ROOT / "config" / "app_config.json",
              encoding="utf-8") as f:
        assert "bviLowPassEnabled" not in json.load(f)


def test_cutoff_key_ships_at_20():
    with open(REPO_ROOT / "config" / "app_config.json",
              encoding="utf-8") as f:
        assert json.load(f)["bviLowPassCutoffHz"] == 20.0
    assert '"bviLowPassCutoffHz": 20.0' in (
        (REPO_ROOT / "main.py").read_text(encoding="utf-8"))


# ── Config-value resolution contract ────────────────────────────────────


def test_resolve_missing_or_invalid_defaults_to_20():
    assert BVI_LPF_DEFAULT_CUTOFF_HZ == 20.0
    for bad in (None, "not-a-number", object(), True, False, float("nan")):
        assert resolve_bvi_lpf_cutoff(bad) == 20.0, repr(bad)


def test_resolve_positive_number_passes_through():
    assert resolve_bvi_lpf_cutoff(12.5) == 12.5
    assert resolve_bvi_lpf_cutoff(20) == 20.0
    assert resolve_bvi_lpf_cutoff("5") == 5.0  # numeric strings tolerated


def test_resolve_nonpositive_disables():
    assert resolve_bvi_lpf_cutoff(0) == 0.0
    assert resolve_bvi_lpf_cutoff(-3.2) == 0.0
    assert resolve_bvi_lpf_cutoff("0") == 0.0


# ── Discretization (alpha) contract ─────────────────────────────────────


def test_alpha_formula_at_default_cutoff():
    """alpha = dt/(RC+dt), RC = 1/(2*pi*fc), dt = 1/40 s — the same
    discretization the legacy QML filter used. At the 20 Hz default this
    is mild smoothing (~0.759), since 20 Hz sits at Nyquist for the
    ~40 fps BVI stream."""
    dt = 1.0 / 40.0
    rc = 1.0 / (2.0 * math.pi * 20.0)
    assert bvi_lpf_alpha(20.0) == pytest.approx(dt / (rc + dt))
    assert bvi_lpf_alpha(20.0) == pytest.approx(0.7585, abs=1e-3)


def test_alpha_nonpositive_cutoff_is_passthrough():
    assert bvi_lpf_alpha(0.0) == 1.0
    assert bvi_lpf_alpha(-1.0) == 1.0


# ── Filter behavior at LiveScanSource ingest ────────────────────────────

ALPHA_20 = bvi_lpf_alpha(20.0)


def _bvi_values(src, side="left", cam_id=0):
    buf = src.buffers[(side, cam_id, "bvi")]
    return [float(v) for v in buf.v[:buf.n]]


def _append(src, side, cam_id, frame_id, t, bvi, bfi=1.0, **kw):
    src.append_uncorrected(side=side, cam_id=cam_id, frame_id=frame_id,
                           t=t, bfi=bfi, bvi=bvi, **kw)


def test_default_construction_stores_bvi_raw():
    """The constructor default is OFF — direct constructions (tests,
    future callers) keep raw-storage semantics. The 20 Hz production
    default is applied by the connector via resolve_bvi_lpf_cutoff."""
    src = LiveScanSource(plot_t0=0.0)
    _append(src, "left", 0, 1, 0.000, bvi=2.0)
    _append(src, "left", 0, 2, 0.025, bvi=10.0)
    assert _bvi_values(src) == pytest.approx([2.0, 10.0])


def test_filter_seeds_then_smooths():
    src = LiveScanSource(plot_t0=0.0, bvi_lpf_cutoff_hz=20.0)
    _append(src, "left", 0, 1, 0.000, bvi=2.0)
    _append(src, "left", 0, 2, 0.025, bvi=10.0)
    _append(src, "left", 0, 3, 0.050, bvi=4.0)
    y1 = 2.0 + ALPHA_20 * (10.0 - 2.0)
    y2 = y1 + ALPHA_20 * (4.0 - y1)
    assert _bvi_values(src) == pytest.approx([2.0, y1, y2], rel=1e-5)


def test_filter_touches_only_bvi():
    src = LiveScanSource(plot_t0=0.0, bvi_lpf_cutoff_hz=20.0)
    for i, (bfi, mean, contrast) in enumerate(
            [(4.0, 100.0, 0.30), (8.0, 200.0, 0.60)]):
        _append(src, "left", 0, i, i * 0.025, bvi=3.0,
                bfi=bfi, mean=mean, contrast=contrast)
    for metric, expect in (("bfi", [4.0, 8.0]), ("mean", [100.0, 200.0]),
                           ("contrast", [0.30, 0.60])):
        buf = src.buffers[("left", 0, metric)]
        assert [float(v) for v in buf.v[:buf.n]] == pytest.approx(
            expect, rel=1e-6), metric


def test_filter_state_is_per_side_and_camera():
    """Streams filter independently — including the clinical side-average
    channel (cam_id -1), which must not share state with any camera."""
    src = LiveScanSource(plot_t0=0.0, bvi_lpf_cutoff_hz=20.0)
    for frame, (a, b, c) in enumerate([(2.0, 100.0, 0.5), (10.0, 60.0, 0.9)]):
        _append(src, "left", 0, frame, frame * 0.025, bvi=a)
        _append(src, "right", 3, frame, frame * 0.025, bvi=b)
        _append(src, "left", -1, frame, frame * 0.025, bvi=c)
    assert _bvi_values(src, "left", 0)[1] == pytest.approx(
        2.0 + ALPHA_20 * (10.0 - 2.0), rel=1e-5)
    assert _bvi_values(src, "right", 3)[1] == pytest.approx(
        100.0 + ALPHA_20 * (60.0 - 100.0), rel=1e-5)
    assert _bvi_values(src, "left", -1)[1] == pytest.approx(
        0.5 + ALPHA_20 * (0.9 - 0.5), rel=1e-5)


def test_nan_passes_through_without_poisoning_state():
    """NaN BVI (covered-sensor side averages) is stored as-is — the
    renderer skips it — and the filter resumes from the last finite
    output rather than latching NaN forever."""
    src = LiveScanSource(plot_t0=0.0, bvi_lpf_cutoff_hz=20.0)
    _append(src, "left", -1, 1, 0.000, bvi=2.0)
    _append(src, "left", -1, 2, 0.025, bvi=float("nan"))
    _append(src, "left", -1, 3, 0.050, bvi=4.0)
    vals = _bvi_values(src, "left", -1)
    assert vals[0] == pytest.approx(2.0)
    assert math.isnan(vals[1])
    assert vals[2] == pytest.approx(2.0 + ALPHA_20 * (4.0 - 2.0), rel=1e-5)


def test_leading_nan_then_first_finite_seeds():
    src = LiveScanSource(plot_t0=0.0, bvi_lpf_cutoff_hz=20.0)
    _append(src, "left", 0, 1, 0.000, bvi=float("nan"))
    _append(src, "left", 0, 2, 0.025, bvi=5.0)
    vals = _bvi_values(src)
    assert math.isnan(vals[0])
    assert vals[1] == pytest.approx(5.0)


def test_nonpositive_cutoff_disables_filter():
    src = LiveScanSource(plot_t0=0.0, bvi_lpf_cutoff_hz=0.0)
    _append(src, "left", 0, 1, 0.000, bvi=2.0)
    _append(src, "left", 0, 2, 0.025, bvi=10.0)
    assert _bvi_values(src) == pytest.approx([2.0, 10.0])
