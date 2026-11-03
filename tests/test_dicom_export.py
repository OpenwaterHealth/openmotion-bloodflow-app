"""DICOM waveform export (dicom_export.py) and its History-export wiring.

The exporter writes a Hemodynamic Waveform object beside the CSV when the
Research-only ``exportDicom`` preference is on. These tests build a small
scans.db with the SDK's ScanDatabase, export it, and read the file back
with pydicom.
"""

import os
from unittest.mock import MagicMock

import numpy as np
import pytest

pydicom = pytest.importorskip("pydicom")

import dicom_export  # noqa: E402
from omotion.ScanDatabase import ScanDatabase  # noqa: E402

pytestmark = pytest.mark.unit

RATE = 40.0


def _make_session(db_path, *, n_frames=200, first_fid=100, drop_fids=(),
                  cams=range(8), sides=(0, 1), subject="SUBJ-7"):
    """One session whose BFI = side*10 + cam + frame/1000 and BVI = -BFI/2,
    so every sample is predictable. ``drop_fids`` leaves frames out."""
    db = ScanDatabase(db_path=db_path)
    sid = db.create_session(
        session_label="20261103_120000_" + subject, session_start=1.762e9,
        session_notes="free text", session_meta={"subject_id": subject})
    rows = []
    for k in range(n_frames):
        fid = first_fid + k
        if fid in drop_fids:
            continue
        for side in sides:
            for cam in cams:
                bfi = side * 10 + cam + k / 1000.0
                rows.append(dict(session_id=sid, cam_id=cam, side=side,
                                 frame_id=fid, timestamp_s=k / RATE,
                                 bfi=bfi, bvi=-bfi / 2))
    db.insert_session_data_rows(rows)
    db.close()
    return sid


def _export(tmp_path, sid, db_path):
    out = str(tmp_path / "scan.dcm")
    with ScanDatabase(db_path=db_path) as db:
        dicom_export.export_session_dicom(db, sid, out,
                                          software_version="1.2.3")
    return pydicom.dcmread(out)


def _values(ds, group):
    """Physical values for one multiplex group, NaN at padding."""
    g = ds.WaveformSequence[group]
    raw = np.frombuffer(g.WaveformData, dtype="<i2").reshape(
        g.NumberOfWaveformSamples, g.NumberOfWaveformChannels)
    sens = np.array([float(c.ChannelSensitivity)
                     for c in g.ChannelDefinitionSequence])
    vals = raw * sens
    vals[raw == dicom_export.PADDING_VALUE] = np.nan
    return vals


def test_roundtrip_layout_and_values(tmp_path):
    db_path = str(tmp_path / "scans.db")
    sid = _make_session(db_path)
    ds = _export(tmp_path, sid, db_path)

    assert ds.SOPClassUID == dicom_export.HEMODYNAMIC_WAVEFORM_SOP_CLASS
    assert ds.file_meta.MediaStorageSOPClassUID == ds.SOPClassUID
    assert ds.Modality == "HD"
    assert ds.PatientID == "SUBJ-7"
    assert ds.PatientName == ""
    assert ds.SoftwareVersions == "1.2.3"
    assert len(ds.WaveformSequence) == 4
    labels = [g.MultiplexGroupLabel for g in ds.WaveformSequence]
    assert labels == ["BFI left", "BFI right", "BVI left", "BVI right"]

    k = np.arange(200)[:, None] / 1000.0
    cams = np.arange(8)[None, :]
    expected = {0: cams + k, 1: 10 + cams + k,
                2: -(cams + k) / 2, 3: -(10 + cams + k) / 2}
    for gi, exp in expected.items():
        g = ds.WaveformSequence[gi]
        assert g.NumberOfWaveformChannels == 8
        assert g.NumberOfWaveformSamples == 200
        assert float(g.SamplingFrequency) == pytest.approx(RATE)
        got = _values(ds, gi)
        # Quantization error is at most half a sensitivity step.
        steps = np.array([float(c.ChannelSensitivity)
                          for c in g.ChannelDefinitionSequence])
        assert np.all(np.abs(got - exp) <= steps / 2 + 1e-12)


def test_pydicom_waveform_array_agrees(tmp_path):
    """pydicom's own decoder (which applies the channel sensitivity)
    reads the same values."""
    db_path = str(tmp_path / "scans.db")
    sid = _make_session(db_path, n_frames=20)
    ds = _export(tmp_path, sid, db_path)
    arr = ds.waveform_array(1)
    assert arr.shape == (20, 8)
    assert arr[5, 3] == pytest.approx(10 + 3 + 0.005, abs=1e-3)


def test_dropped_frames_and_masked_cameras_are_padding(tmp_path):
    db_path = str(tmp_path / "scans.db")
    sid = _make_session(db_path, n_frames=50, first_fid=0,
                        drop_fids={10, 11}, cams=range(0, 4), sides=(0,))
    ds = _export(tmp_path, sid, db_path)

    left = _values(ds, 0)
    assert left.shape == (50, 8)             # time kept across the gap
    assert np.isnan(left[10:12, :4]).all()   # dropped frames
    assert np.isfinite(left[12, :4]).all()
    assert np.isnan(left[:, 4:]).all()       # cameras not in the mask
    assert np.isnan(_values(ds, 1)).all()    # right side absent


def test_unknown_session_and_legacy_rows_raise(tmp_path):
    db_path = str(tmp_path / "scans.db")
    db = ScanDatabase(db_path=db_path)
    sid = db.create_session(session_label="old", session_start=1.0,
                            session_notes=None, session_meta={})
    db.insert_session_data(sid, 0, 0, 0.0, bfi=1.0, bvi=1.0)  # frame_id -1
    with pytest.raises(ValueError):
        dicom_export.read_session(db, 9999)
    with pytest.raises(RuntimeError):
        dicom_export.read_session(db, sid)
    db.close()


@pytest.mark.parametrize("peak, step", [
    (4.7, 2e-4), (7.18, 5e-4), (32.0, 1e-3), (0.01, 5e-7), (0.0, 1e-3),
])
def test_channel_sensitivity_fits_int16(peak, step):
    vals = np.array([0.0, peak, -peak / 2, np.nan])
    assert dicom_export.channel_sensitivity(vals) == pytest.approx(step)
    if peak:
        q = dicom_export.quantize(vals, step)
        assert q[1] <= dicom_export.INT16_MAX
        assert q[3] == dicom_export.PADDING_VALUE


# ── Connector wiring ───────────────────────────────────────────────────────

def _connector(tmp_path, db_path, **cfg):
    from motion_connector import MotionConnector
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = db_path
    app_config = {"engineeringMode": False}
    app_config.update(cfg)
    return MotionConnector(interface=iface, app_config=app_config,
                           data_dir=str(tmp_path), config_dir="config")


@pytest.mark.parametrize("cfg, expect_dcm", [
    ({"exportDicom": True}, True),
    ({"exportDicom": False}, False),
    ({"exportDicom": True, "clinicalMode": True}, False),
])
def test_history_export_writes_dcm_only_when_enabled(tmp_path, cfg,
                                                     expect_dcm):
    db_path = str(tmp_path / "scans.db")
    sid = _make_session(db_path, n_frames=10)
    c = _connector(tmp_path, db_path, **cfg)

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    res = c._export_scans_sync([sid], str(out_dir))
    assert res == {"exported": 1, "skipped": 0}
    label = "20261103_120000_SUBJ-7"
    assert (out_dir / f"{label}_export.csv").exists()
    assert (out_dir / f"{label}_export.dcm").exists() is expect_dcm

    single = str(tmp_path / "single.csv")
    assert c._export_scan_csv_sync(sid, single) is True
    assert os.path.exists(str(tmp_path / "single.dcm")) is expect_dcm


def test_dicom_failure_keeps_csv_export(tmp_path, monkeypatch):
    db_path = str(tmp_path / "scans.db")
    sid = _make_session(db_path, n_frames=10)
    c = _connector(tmp_path, db_path, exportDicom=True)

    def boom(*a, **k):
        raise RuntimeError("nope")
    monkeypatch.setattr(dicom_export, "export_session_dicom", boom)
    toasts = []
    c.notificationRequested.connect(toasts.append)

    out = str(tmp_path / "x.csv")
    assert c._export_scan_csv_sync(sid, out) is True
    assert os.path.exists(out)
    assert not os.path.exists(str(tmp_path / "x.dcm"))
    assert any("DICOM" in t["text"] for t in toasts)


# ── Packaging: Research-only, kept out of the clinical bundle ──────────────

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def test_clinical_builds_leave_out_dicom_export_and_pydicom():
    spec = _read("openwater.spec")
    assert '_excludes += ["dicom_export", "pydicom"]' in spec
    nuitka = _read(os.path.join("scripts", "build_nuitka.ps1"))
    assert '"--nofollow-import-to=dicom_export"' in nuitka
    assert '"--nofollow-import-to=pydicom"' in nuitka
    # Research needs pydicom's urls.json at import time.
    assert "pydicom.data:urls.json" in nuitka


def test_connector_imports_dicom_export_lazily():
    """A top-level import would make the clinical build (which leaves the
    module out) fail at startup."""
    src = _read("motion_connector.py")
    assert "\nimport dicom_export" not in src
    assert "\nfrom dicom_export" not in src
