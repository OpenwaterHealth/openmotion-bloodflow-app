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
  - that every fail-soft exit is legible in the app log (#403) — the
    failure is silent by design, so the log is the only diagnostic.
"""

import logging
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


# ── unavailability is legible in the log (#403) ──────────────────────
#
# Failing to show the sample is fail-soft by design, so the ONLY symptom
# a user or a support bundle can see is an empty viewer — identical for a
# packaging fault, an IO fault and a bad dataset. These tests pin the log
# as the thing that tells them apart.

CONNECTOR_LOGGER = "openmotion.bloodflow-app.connector"


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING]


def test_missing_sample_file_logs_a_warning_naming_the_path(tmp_path, caplog):
    """File absent (the packaging fault) → WARNING naming the resolved
    path, so a frozen build shows where it looked."""
    missing = tmp_path / "nope.csv"
    c = _connector(tmp_path, console=False, left=False, right=False)

    with caplog.at_level(logging.INFO, logger=CONNECTOR_LOGGER):
        c._load_sample_scan(str(missing))

    warns = _warnings(caplog)
    assert any("sample scan unavailable" in m for m in warns), warns
    assert any(str(missing) in m for m in warns), warns


def test_zero_byte_sample_file_logs_a_distinct_warning(tmp_path, caplog):
    """A truncated/0-byte file is called out as such rather than being
    lumped in with 'parsed but empty'."""
    empty = tmp_path / "empty.csv"
    empty.write_bytes(b"")
    c = _connector(tmp_path, console=False, left=False, right=False)

    with caplog.at_level(logging.INFO, logger=CONNECTOR_LOGGER):
        c._load_sample_scan(str(empty))

    warns = _warnings(caplog)
    assert any("0 bytes" in m for m in warns), warns
    assert c.currentScanSource is None


def test_unparseable_sample_logs_parsed_but_empty(tmp_path, caplog):
    """Present and readable but the wrong shape (the bad-dataset fault) →
    a WARNING that says it parsed, distinct from the missing-file one."""
    bad = tmp_path / "bad.csv"
    bad.write_text("x,y\n1,2\n", encoding="utf-8")
    c = _connector(tmp_path, console=False, left=False, right=False)

    with caplog.at_level(logging.INFO, logger=CONNECTOR_LOGGER):
        c._load_sample_scan(str(bad))

    warns = _warnings(caplog)
    assert any("held no usable samples" in m for m in warns), warns
    # Must NOT be reported as a missing/unreadable file.
    assert not any("cannot read" in m for m in warns), warns


def test_successful_load_logs_no_warning(tmp_path, caplog):
    """The healthy path stays quiet at WARNING — otherwise the warnings
    above are noise a reader learns to skip."""
    c = _connector(tmp_path, console=False, left=False, right=False)

    with caplog.at_level(logging.INFO, logger=CONNECTOR_LOGGER):
        c.loadSampleScan()

    assert c.currentScanSource is not None
    assert _warnings(caplog) == []


def _sample_at(monkeypatch, path):
    """Point the connector's bundled-sample lookup at `path`."""
    monkeypatch.setattr(
        MotionConnector, "_sample_scan_path",
        staticmethod(lambda: Path(path)))


@pytest.mark.parametrize("kind", ["missing", "zero_byte", "wrong_header",
                                  "header_only", "undecodable"])
def test_unavailable_sample_suppresses_the_offer(
        tmp_path, caplog, monkeypatch, kind):
    """Never offer what accepting can't deliver.

    The dialog's only action is "Open sample dataset"; if the sample
    can't load, that button silently does nothing. Each unloadable shape
    must suppress the offer outright AND say why in the log."""
    path = tmp_path / "sample.csv"
    if kind == "missing":
        path = tmp_path / "not_bundled.csv"          # never created
    elif kind == "zero_byte":
        path.write_bytes(b"")
    elif kind == "wrong_header":
        path.write_text("x,y\n1,2\n", encoding="utf-8")
    elif kind == "header_only":
        path.write_text(",".join(_per_cam_header()) + "\n", encoding="utf-8")
    elif kind == "undecodable":
        # The loader opens utf-8, so a file that won't decode won't parse.
        path.write_bytes(b"\xff\xfe\x00bfi_l1\x00\xff\n\xff\xfe\n")
    _sample_at(monkeypatch, path)

    c = _connector(tmp_path, console=False, left=False, right=False,
                   app_config={"clinicalMode": False})
    offers = _offers(c)

    with caplog.at_level(logging.INFO, logger=CONNECTOR_LOGGER):
        c._check_connection_watchdog()

    assert offers == [], f"{kind}: offered an unloadable sample"
    warns = _warnings(caplog)
    assert any("sample scan unavailable" in m for m in warns), warns
    msgs = [r.getMessage() for r in caplog.records]
    assert any("offer suppressed — sample not available" in m
               for m in msgs), msgs


def test_suppression_warning_names_the_resolved_path(
        tmp_path, caplog, monkeypatch):
    """The path is the only clue to where a frozen build searched —
    resource_path returns a non-existent candidate on a miss."""
    missing = tmp_path / "not_bundled.csv"
    _sample_at(monkeypatch, missing)
    c = _connector(tmp_path, console=False, left=False, right=False,
                   app_config={"clinicalMode": False})

    with caplog.at_level(logging.INFO, logger=CONNECTOR_LOGGER):
        c._check_connection_watchdog()

    assert any(str(missing) in m for m in _warnings(caplog)), _warnings(caplog)


def test_probe_accepts_the_reduced_clinical_export_header(
        tmp_path, caplog, monkeypatch):
    """The probe must not reject the other valid export format, or a
    clinical-mode sample dataset would be wrongly suppressed."""
    path = tmp_path / "reduced.csv"
    path.write_text(
        "frame_id,timestamp_s,bfi_left,bfi_right,bvi_left,bvi_right\n"
        "1,0.0,1.0,2.0,3.0,4.0\n", encoding="utf-8")
    _sample_at(monkeypatch, path)
    c = _connector(tmp_path, console=False, left=False, right=False,
                   app_config={"clinicalMode": False})
    offers = _offers(c)

    with caplog.at_level(logging.INFO, logger=CONNECTOR_LOGGER):
        c._check_connection_watchdog()

    assert len(offers) == 1
    assert not any("sample scan unavailable" in m for m in _warnings(caplog))


def test_probe_does_not_parse_the_whole_file(tmp_path, monkeypatch):
    """The probe is structural, not a parse. A full parse of the shipped
    sample costs ~170 ms — paid on the GUI thread inside the watchdog
    callback, and again when the user accepts."""
    calls = []
    import motion_connector as mc
    monkeypatch.setattr(
        mc, "load_csv_scan_buffers",
        lambda p: calls.append(p) or {})
    c = _connector(tmp_path, console=False, left=False, right=False,
                   app_config={"clinicalMode": False})

    assert c._sample_scan_available() is True
    assert calls == []


def test_offer_logs_the_shipped_sample_as_available(tmp_path, caplog):
    """Healthy build: the probe records the path and size at INFO, which
    is what a support bundle is diffed against."""
    c = _connector(tmp_path, console=False, left=False, right=False,
                   app_config={"clinicalMode": False})

    with caplog.at_level(logging.INFO, logger=CONNECTOR_LOGGER):
        c._check_connection_watchdog()

    msgs = [r.getMessage() for r in caplog.records]
    assert any("sample scan available" in m for m in msgs), msgs
    # The watchdog's own E-104/E-106 warning is expected here; what must
    # not appear is an unavailability warning.
    assert not any("sample scan unavailable" in m
                   for m in _warnings(caplog)), _warnings(caplog)


def test_ignored_load_request_is_logged(tmp_path, caplog):
    """Clicking 'Open sample dataset' with a source already bound is a
    no-op; log it, or the click looks like it did nothing at all."""
    c = _connector(tmp_path, console=False, left=False, right=False)
    c.loadSampleScan()
    assert c.currentScanSource is not None

    with caplog.at_level(logging.INFO, logger=CONNECTOR_LOGGER):
        c.loadSampleScan()

    msgs = [r.getMessage() for r in caplog.records]
    assert any("request ignored" in m for m in msgs), msgs


@pytest.mark.parametrize(
    "app_config,console,preload,expected",
    [
        ({"clinicalMode": True}, False, False, "clinical build"),
        ({"clinicalMode": False}, True, False, "a device is connected"),
        ({"clinicalMode": False}, False, True, "already bound"),
    ],
)
def test_every_offer_suppression_logs_its_reason(
        tmp_path, caplog, app_config, console, preload, expected):
    """Each gate says why it declined to offer. A once-per-launch decision
    with three gates is undiagnosable if the returns are silent."""
    c = _connector(tmp_path, console=console, left=False, right=False,
                   app_config=app_config)
    if preload:
        c.loadSampleScan()
    offers = _offers(c)

    with caplog.at_level(logging.INFO, logger=CONNECTOR_LOGGER):
        c._check_connection_watchdog()

    assert offers == []
    msgs = [r.getMessage() for r in caplog.records]
    assert any("offer suppressed" in m and expected in m for m in msgs), msgs


# ── watchdog-driven offer gating ─────────────────────────────────────


def _offers(conn):
    """Capture sampleScanOfferRequested emissions."""
    out = []
    conn.sampleScanOfferRequested.connect(lambda: out.append(True))
    return out


def test_watchdog_offers_sample_in_research_with_no_device(tmp_path):
    """Research build, nothing connected → offer the sample dataset."""
    c = _connector(tmp_path, console=False, left=False, right=False,
                   app_config={"clinicalMode": False})
    offers = _offers(c)

    c._check_connection_watchdog()

    assert len(offers) == 1
    # The offer is only an offer — nothing is loaded until the user accepts.
    assert c.currentScanSource is None


def test_watchdog_never_offers_in_clinical_mode(tmp_path):
    """Clinical builds get no offer and no sample — ever. A clinical user
    must never see fabricated traces they didn't ask for."""
    c = _connector(tmp_path, console=False, left=False, right=False,
                   app_config={"clinicalMode": True})
    offers = _offers(c)

    c._check_connection_watchdog()

    assert offers == []
    assert c.currentScanSource is None


@pytest.mark.parametrize(
    "console,left,right",
    [(True, False, False), (False, True, False), (False, False, True)],
)
def test_watchdog_no_offer_when_any_device_is_connected(
        tmp_path, console, left, right):
    """Gated on the whole system, not just the console: a console-less rig
    with a live sensor attached must never be offered a sample."""
    c = _connector(tmp_path, console=console, left=left, right=right,
                   app_config={"clinicalMode": False})
    offers = _offers(c)

    c._check_connection_watchdog()

    assert offers == []


def test_watchdog_no_offer_when_a_source_is_already_bound(tmp_path):
    """Something already showing (e.g. a past scan the user opened from
    History during the 12 s window) is never clobbered by the offer."""
    c = _connector(tmp_path, console=False, left=False, right=False,
                   app_config={"clinicalMode": False})
    c.loadSampleScan()
    assert c.currentScanSource is not None
    offers = _offers(c)

    c._check_connection_watchdog()

    assert offers == []


def test_watchdog_still_warns_while_offering(tmp_path):
    """The offer is additive: the E-104/E-106 warning toast still fires."""
    c = _connector(tmp_path, console=False, left=False, right=False,
                   app_config={"clinicalMode": False})
    notifs = []
    c.notificationRequested.connect(lambda p: notifs.append(p))
    offers = _offers(c)

    c._check_connection_watchdog()

    assert len(offers) == 1
    assert len(notifs) == 1
    assert notifs[0]["type"] == "warning"
