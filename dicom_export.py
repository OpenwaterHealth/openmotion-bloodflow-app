"""DICOM export of a recorded scan (Research builds, opt-in).

History → Export writes ``<label>_export.csv``; with the ``exportDicom``
preference on, the connector also writes ``<label>_export.dcm`` beside it
through :func:`export_session_dicom`.

The object is a **Hemodynamic Waveform** (SOP class
1.2.840.10008.5.1.4.1.1.9.2.1, Modality ``HD``): four multiplex groups of
eight channels each, one group per (metric, side), so BFI and BVI for all
16 cameras fit in one instance:

    group 1  BFI left   cameras 1-8
    group 2  BFI right  cameras 1-8
    group 3  BVI left   cameras 1-8
    group 4  BVI right  cameras 1-8

Samples are signed 16-bit (``SS``). Each channel gets its own Channel
Sensitivity (a 1-2-5 step chosen so the channel's largest |value| fits in
int16), so ``stored * sensitivity`` gives the value back to about 1 part in
16,000 of that channel's range. A frame with no sample for a channel (a
camera masked off, a per-camera dropout, a NaN) is written as the Waveform
Padding Value, never as a fabricated number. The sample index is
``frame_id - first_frame_id``, so dropped frames stay in time.

``mean``, ``contrast``, ``temp`` and ``quality`` stay in the CSV: the
hemodynamic object holds at most 4 groups x 8 channels. Scan notes are not
written (the CSV export does not carry them either).

Patient identity: the app only records the operator-typed subject ID, which
becomes Patient ID. Patient Name, Birth Date and Sex are Type 2 and are
written empty. UIDs come from pydicom's ``generate_uid()`` (``2.25.`` root),
so exporting the same scan twice gives two different instances.

Kept free of Qt and of the SDK so it can be unit-tested on plain sqlite:
:func:`read_session` takes any object with the ``ScanDatabase``
``_connection()`` method (the SDK's handles SQLCipher on clinical builds).
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import numpy as np

logger = logging.getLogger("openmotion.bloodflow-app.dicom")

HEMODYNAMIC_WAVEFORM_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.9.2.1"

# Private coding scheme for the channel sources: DICOM has no standard
# codes for speckle BFI/BVI. "99" prefix = private scheme per PS3.16.
CODING_SCHEME = "99OWH"

METRICS = ("bfi", "bvi")
SIDES = ("l", "r")
CAMERAS_PER_SIDE = 8
NOMINAL_SAMPLE_RATE_HZ = 40.0

INT16_MAX = 32767
PADDING_VALUE = -32768  # reserved: never produced by quantization

_SIDE_NAME = {"l": "left", "r": "right"}


@dataclass
class SessionWaveforms:
    """Everything the writer needs, already pulled out of scans.db."""
    session_id: int
    session_label: str
    session_start: Optional[float]          # epoch seconds
    meta: dict = field(default_factory=dict)
    sample_rate_hz: float = NOMINAL_SAMPLE_RATE_HZ
    # (metric, side) -> float array (n_samples, 8), NaN where missing
    channels: dict = field(default_factory=dict)

    @property
    def n_samples(self) -> int:
        for arr in self.channels.values():
            return int(arr.shape[0])
        return 0


# ── Reading ────────────────────────────────────────────────────────────────

def _parse_meta(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _estimate_rate(frame_ids: np.ndarray, times: np.ndarray) -> float:
    """Frames per second from the span of frame ids over the span of time.
    Falls back to the nominal 40 Hz for a scan too short to measure."""
    if frame_ids.size < 2:
        return NOMINAL_SAMPLE_RATE_HZ
    df = float(frame_ids[-1] - frame_ids[0])
    dt = float(times[-1] - times[0])
    if df <= 0 or dt <= 0:
        return NOMINAL_SAMPLE_RATE_HZ
    rate = df / dt
    if not (1.0 <= rate <= 400.0):
        logger.warning("dicom export: implausible frame rate %.3f Hz; "
                       "using nominal %.0f Hz", rate, NOMINAL_SAMPLE_RATE_HZ)
        return NOMINAL_SAMPLE_RATE_HZ
    return round(rate, 3)


def read_session(db, session_id: int) -> SessionWaveforms:
    """Load one session's per-camera BFI/BVI from a ScanDatabase.

    Raises ValueError for an unknown session and RuntimeError for one with
    no playable data (no rows, or rows recorded before frame ids existed),
    mirroring the SDK's ``materialize_corrected_csv``."""
    conn = db._connection()
    row = conn.execute(
        "SELECT session_label, session_start, session_meta "
        "FROM sessions WHERE id = ?", (int(session_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"session_id={session_id} not found")
    label, start, meta_raw = row[0], row[1], row[2]

    rows = conn.execute(
        "SELECT frame_id, timestamp_s, side, cam_id, bfi, bvi "
        "FROM session_data WHERE session_id = ? "
        "ORDER BY frame_id ASC, side ASC, cam_id ASC",
        (int(session_id),),
    ).fetchall()
    if not rows:
        raise RuntimeError(f"session_id={session_id} has no data")
    data = np.array(
        [[r[0], r[1], r[2], r[3],
          np.nan if r[4] is None else r[4],
          np.nan if r[5] is None else r[5]] for r in rows],
        dtype=float,
    )
    fids = data[:, 0].astype(np.int64)
    if (fids < 0).all():
        raise RuntimeError(
            f"session_id={session_id} predates frame ids; cannot export")
    keep = fids >= 0
    data, fids = data[keep], fids[keep]

    first = int(fids.min())
    idx = fids - first
    n = int(idx.max()) + 1

    # One timestamp per frame (the min over cameras, like the CSV) for the
    # rate estimate.
    uniq_fids, first_pos = np.unique(fids, return_index=True)
    frame_ts = np.minimum.reduceat(data[:, 1], first_pos)
    rate = _estimate_rate(uniq_fids, frame_ts)

    channels = {}
    for mi, metric in enumerate(METRICS):
        for si, side in enumerate(SIDES):
            arr = np.full((n, CAMERAS_PER_SIDE), np.nan)
            sel = data[:, 2].astype(int) == si
            cams = data[sel, 3].astype(int)
            ok = (cams >= 0) & (cams < CAMERAS_PER_SIDE)
            arr[idx[sel][ok], cams[ok]] = data[sel, 4 + mi][ok]
            channels[(metric, side)] = arr

    return SessionWaveforms(
        session_id=int(session_id),
        session_label=label or "",
        session_start=float(start) if start is not None else None,
        meta=_parse_meta(meta_raw),
        sample_rate_hz=rate,
        channels=channels,
    )


# ── Quantization ───────────────────────────────────────────────────────────

def channel_sensitivity(values: np.ndarray) -> float:
    """Smallest 1-2-5 x 10^k step whose int16 range covers max|values|."""
    finite = values[np.isfinite(values)]
    peak = float(np.max(np.abs(finite))) if finite.size else 0.0
    if peak <= 0.0:
        return 1e-3
    need = peak / INT16_MAX
    exp = math.floor(math.log10(need))
    for k in (exp, exp + 1):
        for m in (1, 2, 5):
            step = m * 10.0 ** k
            if step >= need:
                # Round away float noise (2e-4 not 0.00020000000000000001).
                return float(f"{step:.1e}")
    return float(f"{10.0 ** (exp + 1):.1e}")


def quantize(values: np.ndarray, sensitivity: float) -> np.ndarray:
    q = np.round(values / sensitivity)
    q = np.clip(q, -INT16_MAX, INT16_MAX)
    q[~np.isfinite(values)] = PADDING_VALUE
    return q.astype("<i2")


def _ds(value: float) -> str:
    """A DICOM DS string (max 16 chars)."""
    s = f"{value:.10g}"
    return s if len(s) <= 16 else f"{value:.6e}"


# ── Writing ────────────────────────────────────────────────────────────────

def _code(value: str, meaning: str, scheme: str = CODING_SCHEME):
    from pydicom.dataset import Dataset
    c = Dataset()
    c.CodeValue = value
    c.CodingSchemeDesignator = scheme
    c.CodeMeaning = meaning
    return c


def build_dataset(sw: SessionWaveforms, *, software_version: str = "",
                  now: Optional[datetime] = None):
    """Build the Hemodynamic Waveform dataset for one session."""
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    if sw.n_samples == 0:
        raise RuntimeError("nothing to export: session has no samples")

    now = now or datetime.now()
    acq = (datetime.fromtimestamp(sw.session_start)
           if sw.session_start else now)

    sop_uid = generate_uid()
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = HEMODYNAMIC_WAVEFORM_SOP_CLASS
    fm.MediaStorageSOPInstanceUID = sop_uid
    fm.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = Dataset()
    ds.file_meta = fm
    ds.SpecificCharacterSet = "ISO_IR 192"  # UTF-8 subject IDs

    # SOP Common
    ds.SOPClassUID = HEMODYNAMIC_WAVEFORM_SOP_CLASS
    ds.SOPInstanceUID = sop_uid
    ds.InstanceCreationDate = now.strftime("%Y%m%d")
    ds.InstanceCreationTime = now.strftime("%H%M%S")

    # Patient (Type 2 fields present, empty when unknown)
    ds.PatientName = ""
    ds.PatientID = str(sw.meta.get("subject_id") or "")[:64]
    ds.PatientBirthDate = ""
    ds.PatientSex = ""

    # General Study
    ds.StudyInstanceUID = generate_uid()
    ds.StudyDate = acq.strftime("%Y%m%d")
    ds.StudyTime = acq.strftime("%H%M%S")
    ds.ReferringPhysicianName = ""
    ds.StudyID = ""
    ds.AccessionNumber = ""
    ds.StudyDescription = "Open-Motion blood flow scan"

    # General Series
    ds.Modality = "HD"
    ds.SeriesInstanceUID = generate_uid()
    ds.SeriesNumber = 1
    ds.SeriesDescription = (sw.session_label or "Open-Motion scan")[:64]

    # General Equipment
    ds.Manufacturer = "Openwater"
    ds.ManufacturerModelName = "Open-Motion"
    if software_version:
        ds.SoftwareVersions = str(software_version)[:64]

    # Waveform Identification
    ds.InstanceNumber = 1
    ds.ContentDate = acq.strftime("%Y%m%d")
    ds.ContentTime = acq.strftime("%H%M%S")
    ds.AcquisitionDateTime = acq.strftime("%Y%m%d%H%M%S.%f")

    # Acquisition Context (Type 2 sequence; nothing to say)
    ds.AcquisitionContextSequence = []

    groups = []
    n = sw.n_samples
    for metric in METRICS:
        for side in SIDES:
            values = sw.channels[(metric, side)]
            g = Dataset()
            g.MultiplexGroupTimeOffset = "0"
            g.WaveformOriginality = "ORIGINAL"
            g.NumberOfWaveformChannels = CAMERAS_PER_SIDE
            g.NumberOfWaveformSamples = n
            g.SamplingFrequency = _ds(sw.sample_rate_hz)
            g.MultiplexGroupLabel = f"{metric.upper()} {_SIDE_NAME[side]}"
            g.WaveformBitsAllocated = 16
            g.WaveformSampleInterpretation = "SS"
            g.WaveformPaddingValue = PADDING_VALUE.to_bytes(
                2, "little", signed=True)

            stored = np.empty((n, CAMERAS_PER_SIDE), dtype="<i2")
            chdefs = []
            for c in range(CAMERAS_PER_SIDE):
                col = values[:, c]
                sens = channel_sensitivity(col)
                stored[:, c] = quantize(col, sens)
                label = f"{metric}_{side}{c + 1}"
                cd = Dataset()
                cd.WaveformChannelNumber = c + 1
                cd.ChannelLabel = label
                cd.ChannelSourceSequence = [_code(
                    label.upper(),
                    f"{metric.upper()} {_SIDE_NAME[side]} camera {c + 1}")]
                cd.ChannelSensitivity = _ds(sens)
                cd.ChannelSensitivityUnitsSequence = [
                    _code("[arb'U]", "arbitrary unit", "UCUM")]
                cd.ChannelSensitivityCorrectionFactor = "1"
                cd.ChannelBaseline = "0"
                cd.ChannelSampleSkew = "0"
                cd.WaveformBitsStored = 16
                chdefs.append(cd)
            g.ChannelDefinitionSequence = chdefs
            # Sample-major, channels interleaved.
            g.WaveformData = stored.tobytes()
            groups.append(g)
    ds.WaveformSequence = groups
    return ds


def export_session_dicom(db, session_id: int, output_path: str, *,
                         software_version: str = "") -> str:
    """Read ``session_id`` from ``db`` and write it to ``output_path``."""
    sw = read_session(db, session_id)
    ds = build_dataset(sw, software_version=software_version)
    ds.save_as(output_path, enforce_file_format=True)
    logger.info("dicom export: sid=%d, %d samples @ %s Hz -> %s",
                sw.session_id, sw.n_samples, sw.sample_rate_hz, output_path)
    return output_path
