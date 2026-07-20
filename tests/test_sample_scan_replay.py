"""Sample-scan-in-replay when no device is connected at boot (#314).

Design (a): the bundled sample scan is parsed from a CSV into an in-memory
``PastScanSource`` (``preloaded_buffers``, ``scan_db=None``) and bound to the
viewer via ``_set_current_scan_source`` — the same replay path History uses,
but DB-free so the user's real ``scans.db`` is never touched.

Covers:
  - ``load_csv_scan_buffers`` column mapping / timestamps / per-cam series,
    against both a synthetic CSV and the shipped ``resources/sample_scan.csv``.
  - the replay-source interface those buffers feed (points_for_window, masks).
  - the connector's watchdog-driven offer gating (research-only, no-device-only).
  - the fail-soft missing/empty-file path.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from data_sources import (
    PastScanSource, buffers_are_empty, load_csv_scan_buffers,
)
from motion_connector import MotionConnector

pytestmark = pytest.mark.unit


REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_SAMPLE = REPO_ROOT / "resources" / "sample_scan.csv"

_PER_CAM_METRICS = ("bfi", "bvi", "mean", "contrast")


def _per_cam_header() -> list[str]:
    """The per-cam wide export header (frame_id, timestamp_s, then
    metric_side+cam for bfi/bvi/mean/contrast). Mirrors the shipped
    export format that ``_load_corrected_csv_into`` detects."""
    cols = ["frame_id", "timestamp_s"]
    for metric in _PER_CAM_METRICS:
        for prefix in ("l", "r"):
            for cam in range(1, 9):
                cols.append(f"{metric}_{prefix}{cam}")
    return cols


def _write_synthetic_csv(path: Path, n_frames: int = 5) -> None:
    """Write a small per-cam CSV. Each cell is a deterministic function of
    (frame, metric, side, cam) so the loader's column→(side,cam,metric)
    mapping can be asserted exactly."""
    header = _per_cam_header()
    lines = [",".join(header)]
    for f in range(n_frames):
        frame_id = 10 + f
        t = round((frame_id - 1) * 0.025, 6)
        row = [str(frame_id), str(t)]
        for metric in _PER_CAM_METRICS:
            m_base = _PER_CAM_METRICS.index(metric) * 100
            for side_idx, prefix in enumerate(("l", "r")):
                for cam in range(1, 9):
                    row.append(str(m_base + side_idx * 10 + cam + f * 0.001))
        lines.append(",".join(row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── load_csv_scan_buffers ────────────────────────────────────────────


def test_load_csv_maps_columns_to_side_cam_metric(tmp_path):
    csv_path = tmp_path / "sample.csv"
    _write_synthetic_csv(csv_path, n_frames=3)

    buffers = load_csv_scan_buffers(str(csv_path))

    # 4 metrics × 2 sides × 8 cams = 64 buffers, all cam_id 0..7.
    assert len(buffers) == 4 * 2 * 8
    assert all(0 <= key[1] < 8 for key in buffers)
    assert {k[2] for k in buffers} == set(_PER_CAM_METRICS)

    # Column l1 → (left, cam_id 0); r8 → (right, cam_id 7). Verify a
    # couple of exact values (frame 0 → 10 + side*10 + cam for bfi).
    bfi_l1 = buffers[("left", 0, "bfi")]
    assert bfi_l1.n == 3
    assert round(float(bfi_l1.v[0]), 3) == round(0 + 0 * 10 + 1, 3)  # 1.0
    bfi_r8 = buffers[("right", 7, "bfi")]
    assert round(float(bfi_r8.v[0]), 3) == round(0 + 10 + 8, 3)      # 18.0
    contrast_l1 = buffers[("left", 0, "contrast")]
    assert round(float(contrast_l1.v[0]), 3) == round(300 + 1, 3)    # 301.0


def test_load_csv_preserves_timestamps_and_frame_ids(tmp_path):
    csv_path = tmp_path / "sample.csv"
    _write_synthetic_csv(csv_path, n_frames=4)

    buffers = load_csv_scan_buffers(str(csv_path))
    buf = buffers[("left", 0, "bfi")]
    # frame_ids start at 10; timestamps at (frame_id-1)*0.025.
    assert list(buf.frame_id[: buf.n]) == [10, 11, 12, 13]
    assert round(float(buf.t[0]), 6) == round(9 * 0.025, 6)
    # Monotonic non-decreasing (window_indices' precondition).
    ts = list(buf.t[: buf.n])
    assert ts == sorted(ts)


def test_load_csv_missing_file_is_fail_soft(tmp_path):
    """A missing file yields an empty dict, never raises."""
    buffers = load_csv_scan_buffers(str(tmp_path / "does_not_exist.csv"))
    assert buffers_are_empty(buffers)


def test_load_csv_unrecognized_columns_yields_empty(tmp_path):
    """A file whose header has none of the expected columns loads nothing
    (best-effort), rather than raising."""
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    buffers = load_csv_scan_buffers(str(bad))
    assert buffers_are_empty(buffers)


def test_shipped_sample_csv_loads_all_16_cameras():
    """The committed resources/sample_scan.csv must parse into a full
    per-cam layout (both sensors, 8 cams each, all four metrics)."""
    assert SHIPPED_SAMPLE.exists(), f"missing sample: {SHIPPED_SAMPLE}"
    buffers = load_csv_scan_buffers(str(SHIPPED_SAMPLE))
    assert not buffers_are_empty(buffers)
    cams_left = {k[1] for k in buffers if k[0] == "left" and 0 <= k[1] < 8}
    cams_right = {k[1] for k in buffers if k[0] == "right" and 0 <= k[1] < 8}
    assert cams_left == set(range(8))
    assert cams_right == set(range(8))
    for metric in _PER_CAM_METRICS:
        assert ("left", 0, metric) in buffers


# ── replay-source interface fed by the CSV buffers ───────────────────


def test_past_scan_source_from_csv_serves_replay_points(tmp_path):
    csv_path = tmp_path / "sample.csv"
    _write_synthetic_csv(csv_path, n_frames=6)
    buffers = load_csv_scan_buffers(str(csv_path))

    src = PastScanSource(
        scan_db=None, session_id=-1, preloaded_buffers=buffers,
        user_label="Sample scan",
    )
    try:
        assert src.live is False
        # Per-cam sample → grid masks both 0xFF, per-camera (clinical=0).
        assert src.leftMask == 0xFF
        assert src.rightMask == 0xFF
        assert src.clinicalMode == 0
        assert src.userLabel == "Sample scan"
        # points_for_window returns [t, v] pairs over the range. Use a
        # realistic window (the sample spans ~0.225..0.35 s) with an ample
        # point budget so no decimation collapses the trace.
        pts = src.points_for_window(
            "left", 0, "bfi", 0.0, 1.0, 1000)
        assert len(pts) == 6
        assert all(len(p) == 2 for p in pts)
        # value_at near the first sample resolves to a finite number.
        v = src.value_at("left", 0, "bfi", float(pts[0][0]))
        assert v == pytest.approx(pts[0][1], rel=1e-3)
    finally:
        src.release()


# ── connector gating + fail-soft ─────────────────────────────────────


def _connector(tmp_path, *, console, left, right, scan_db_path=None,
               app_config=None):
    iface = MagicMock()
    iface.is_device_connected.return_value = (console, left, right)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = scan_db_path
    cfg = {"engineeringMode": False}
    if app_config:
        cfg.update(app_config)
    return MotionConnector(
        interface=iface, app_config=cfg,
        data_dir=str(tmp_path), config_dir="config",
    )


def test_load_sample_scan_binds_replay_source(tmp_path):
    """The slot binds the shipped sample as a non-live (replay)
    PastScanSource. No device check here — the caller (the watchdog offer,
    via the dialog) has already decided."""
    c = _connector(tmp_path, console=False, left=False, right=False)
    assert c.currentScanSource is None

    c.loadSampleScan()

    src = c.currentScanSource
    assert isinstance(src, PastScanSource)
    assert src.live is False
    assert src.session_id == -1
    assert src.userLabel == "Sample scan"
    assert sum(b.n for b in src.buffers.values()) > 0


def test_sample_not_reloaded_when_a_source_is_already_bound(tmp_path):
    """Idempotent: with a source already bound the call is a no-op, so it
    can't clobber a live scan the user has since started."""
    c = _connector(tmp_path, console=False, left=False, right=False)
    c.loadSampleScan()
    first = c.currentScanSource
    assert first is not None
    c.loadSampleScan()
    assert c.currentScanSource is first


def test_sample_source_replaced_by_a_real_scan_source(tmp_path):
    """When a device connects and a real scan runs, the new source
    supersedes the sample (proving the sample never blocks/pollutes a
    real scan)."""
    c = _connector(tmp_path, console=False, left=False, right=False)
    c.loadSampleScan()
    sample = c.currentScanSource
    assert isinstance(sample, PastScanSource)

    from data_sources import LiveScanSource
    live = LiveScanSource(plot_t0=0.0, parent=c)
    c._live_scan_source = live
    c._set_current_scan_source(live)
    assert c.currentScanSource is live
    live.release()


def test_missing_sample_file_is_fail_soft(tmp_path):
    """A missing/unreadable sample must not crash boot — no source bound,
    no exception."""
    c = _connector(tmp_path, console=False, left=False, right=False)
    c._load_sample_scan(str(tmp_path / "nope.csv"))
    assert c.currentScanSource is None


def test_empty_sample_file_is_fail_soft(tmp_path):
    """A file with no recognized columns leaves the viewer empty."""
    bad = tmp_path / "empty.csv"
    bad.write_text("x,y\n1,2\n", encoding="utf-8")
    c = _connector(tmp_path, console=False, left=False, right=False)
    c._load_sample_scan(str(bad))
    assert c.currentScanSource is None
