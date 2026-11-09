"""Lab Streaming Layer (LSL) output for a running scan.

``LslStreamSink`` is a scan pipeline sink that publishes two LSL streams
while a scan runs:

``OpenMotion-Live``
    The realtime values the live plot shows: per frame, dark-corrected
    against the predicted dark baseline. Rows go out as the frames arrive
    (40 Hz).

``OpenMotion-Corrected``
    The final values the scan database stores and History → Export CSV
    writes: each frame dark-subtracted against the dark frames on either
    side of it. They exist only once the next dark frame has closed the
    interval, so rows go out in bursts of about 600 frames every 15 s.

Both streams use the corrected CSV's column layout, taken from the SDK's
own header lists (``omotion.pipeline.sinks``) rather than restated here, so
a stream and the CSV cannot drift apart. One channel per CSV column, same
names, same order:

* Research layout (82 channels): ``frame_id``, ``timestamp_s``,
  ``bfi_l1`` … ``bfi_r8``, ``bvi_*``, ``mean_*``, ``contrast_*``, ``temp_*``.
* Clinical (reduced) layout (6 channels): ``frame_id``, ``timestamp_s``,
  ``bfi_left``, ``bfi_right``, ``bvi_left``, ``bvi_right``.

The export-only ``quality_*`` columns are text, which a numeric LSL stream
cannot carry, so the streams match the 82-column per-scan CSV
(``docs/csv-formats.md``) rather than the 98-column export.

Samples are ``double64`` so ``frame_id`` stays exact. An empty CSV cell is
``NaN``. Each sample's LSL timestamp is the frame's ``timestamp_s`` mapped
onto the LSL clock (``local_clock()``), anchored on the first frame either
stream sees, so the live and corrected rows for one frame carry the same
LSL time even though the corrected one is sent ~15 s later.

The sink never raises into the scan: a missing liblsl or a failing push is
logged and the sink goes quiet for the rest of the scan.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Callable, Optional

from omotion.pipeline.sinks import _NORMAL_HEADERS, _REDUCED_HEADERS

logger = logging.getLogger("openmotion.bloodflow-app.lsl")

LIVE_STREAM_NAME = "OpenMotion-Live"
CORRECTED_STREAM_NAME = "OpenMotion-Corrected"
STREAM_TYPE = "BloodFlow"
NOMINAL_SRATE_HZ = 40.0

# A row waits for every scanned camera before it is sent. When a camera
# drops a frame that row would never complete, so a row this many frames
# behind the newest one goes out with NaN in the missing cells.
# Live: ~1 s. Corrected rows arrive one dark interval (600 frames) per side
# at a time, so wait for two intervals before giving up on a side.
LIVE_MAX_LAG_FRAMES = 40
CORRECTED_MAX_LAG_FRAMES = 1200

_SIDES = ("left", "right")
_SIDE_CHAR = {"left": "l", "right": "r"}

_UNITS = {
    "frame_id": "count",
    "timestamp_s": "s",
    "bfi": "index",
    "bvi": "index",
    "mean": "DN",
    "contrast": "unitless",
    "temp": "celsius",
}


def stream_columns(reduced_mode: bool) -> list[str]:
    """The channel labels, in order: the corrected CSV header itself."""
    return list(_REDUCED_HEADERS if reduced_mode else _NORMAL_HEADERS)


def channel_metadata(label: str) -> dict[str, str]:
    """``label`` / ``unit`` / ``type`` (and ``side`` / ``camera`` where the
    column names one) for a CSV column name."""
    if label in ("frame_id", "timestamp_s"):
        return {"label": label, "unit": _UNITS[label], "type": label}
    metric, _, where = label.partition("_")
    meta = {"label": label, "unit": _UNITS[metric], "type": metric.upper()}
    if where in _SIDES:  # reduced layout: bfi_left
        meta["side"] = where
    else:  # research layout: bfi_l3
        meta["side"] = "left" if where[0] == "l" else "right"
        meta["camera"] = where[1:]
    return meta


def lsl_available() -> tuple[bool, str]:
    """Whether pylsl and its bundled liblsl load. ``(False, reason)`` when not."""
    try:
        import pylsl  # noqa: F401  (loads liblsl on import)
    except Exception as exc:  # ImportError, or RuntimeError for a missing liblsl
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def _num(v) -> float:
    if v is None:
        return math.nan
    try:
        return float(v)
    except (TypeError, ValueError):
        return math.nan


class _RowAssembler:
    """Collects per-camera cells into CSV-shaped rows keyed by frame number
    and releases them in frame order, the way ``CsvSink`` builds its file,
    except that an overdue row is released incomplete instead of holding
    every later row back."""

    def __init__(self, columns: list[str], expected: dict[str, set[int]],
                 reduced: bool, max_lag: int) -> None:
        self._index = {c: i for i, c in enumerate(columns)}
        self._n = len(columns)
        self._expected = expected
        self._reduced = reduced
        self._max_lag = max_lag
        self._acc: dict[int, dict] = {}
        self._next: Optional[int] = None

    def put(self, frame_id: int, t: float, column: str, value) -> None:
        if self._next is not None and frame_id < self._next:
            return  # that row has already been sent
        entry = self._acc.get(frame_id)
        if entry is None:
            entry = {"t": float(t), "row": [math.nan] * self._n, "seen": set()}
            self._acc[frame_id] = entry
        else:
            # Same rule as the CSV: the earliest timestamp among the cameras.
            entry["t"] = min(entry["t"], float(t))
        entry["row"][self._index[column]] = _num(value)

    def mark(self, frame_id: int, side: str, cam_id: int) -> None:
        """Record that ``side``/``cam_id`` delivered ``frame_id`` (its cells may
        still be NaN: a covered camera delivers frames with no value)."""
        entry = self._acc.get(frame_id)
        if entry is not None:
            entry["seen"].add((side, cam_id))

    def _complete(self, entry: dict) -> bool:
        seen = entry["seen"]
        for side in _SIDES:
            want = self._expected.get(side) or set()
            if self._reduced:
                if want and (side, -1) not in seen:
                    return False
            elif any((side, c) not in seen for c in want):
                return False
        return True

    def drain(self, flush_all: bool = False) -> list[tuple[float, list[float]]]:
        out: list[tuple[float, list[float]]] = []
        acc = self._acc
        if not acc:
            return out
        if self._next is None:
            self._next = min(acc)
        newest = max(acc)
        while acc:
            fid = self._next
            overdue = flush_all or newest - fid >= self._max_lag
            entry = acc.get(fid)
            if entry is not None and (overdue or self._complete(entry)):
                del acc[fid]
                row = entry["row"]
                row[0] = float(fid)
                row[1] = entry["t"]
                out.append((entry["t"], row))
                self._next = fid + 1
            elif entry is None and (
                    overdue or self._complete(acc[min(k for k in acc if k > fid)])):
                # No camera delivered this frame, and either it is overdue or
                # every scanned camera has already delivered a later one
                # (cameras deliver in order, so it is not coming). The CSV has
                # no row for it either: skip to the next frame that has one.
                self._next = min(k for k in acc if k > fid)
            else:
                break
        return out


class LslStreamSink:
    """Pipeline sink publishing the live and corrected LSL streams."""

    channels = {"live", "live_side", "final"}

    def __init__(self, *, source_prefix: str = "openmotion-bloodflow",
                 app_version: str = "",
                 outlet_factory: Optional[Callable[..., Any]] = None,
                 clock: Optional[Callable[[], float]] = None) -> None:
        # outlet_factory(name, columns, source_id, desc) -> outlet with
        # push_chunk(rows, timestamps); clock() -> LSL time. Both default to
        # pylsl and exist so the unit tests need no network.
        self._source_prefix = source_prefix
        self._app_version = app_version
        self._outlet_factory = outlet_factory
        self._clock = clock
        self._live = None
        self._corrected = None
        self._live_rows: Optional[_RowAssembler] = None
        self._corrected_rows: Optional[_RowAssembler] = None
        self._reduced = False
        self._offset: Optional[float] = None
        self._failed = False

    # ── Sink protocol ────────────────────────────────────────────────────

    def on_scan_start(self, meta) -> None:
        self._failed = False
        self._offset = None
        self._reduced = bool(meta.reduced_mode)
        columns = stream_columns(self._reduced)
        expected = {
            "left": {c for c in range(8) if meta.left_camera_mask & (1 << c)},
            "right": {c for c in range(8) if meta.right_camera_mask & (1 << c)},
        }
        self._live_rows = _RowAssembler(
            columns, expected, self._reduced, LIVE_MAX_LAG_FRAMES)
        self._corrected_rows = _RowAssembler(
            columns, expected, self._reduced, CORRECTED_MAX_LAG_FRAMES)
        try:
            factory = self._outlet_factory or _pylsl_outlet
            if self._clock is None:
                import pylsl
                self._clock = pylsl.local_clock
            layout = "clinical" if self._reduced else "research"
            desc = {
                "manufacturer": "Openwater",
                "app_version": self._app_version,
                "layout": layout,
                "scan_id": meta.scan_id,
                "left_camera_mask": f"0x{meta.left_camera_mask:02X}",
                "right_camera_mask": f"0x{meta.right_camera_mask:02X}",
            }
            self._live = factory(
                LIVE_STREAM_NAME, columns,
                f"{self._source_prefix}-live-{layout}",
                dict(desc, values="live"))
            self._corrected = factory(
                CORRECTED_STREAM_NAME, columns,
                f"{self._source_prefix}-corrected-{layout}",
                dict(desc, values="corrected"))
            logger.info(
                "LSL output on: streams %r and %r, %d channels (%s layout)",
                LIVE_STREAM_NAME, CORRECTED_STREAM_NAME, len(columns), layout)
        except Exception:
            logger.exception("LSL output could not start; this scan streams nothing")
            self._failed = True
            self._live = self._corrected = None

    def consume(self, channel: str, payload: Any) -> None:
        if self._failed:
            return
        try:
            if channel == "live" and not self._reduced:
                self._consume_live(payload)
                self._push(self._live, self._live_rows.drain())
            elif channel == "live_side" and self._reduced:
                self._consume_live_side(payload)
                self._push(self._live, self._live_rows.drain())
            elif channel == "final":
                self._consume_final(payload)
                self._push(self._corrected, self._corrected_rows.drain())
        except Exception:
            logger.exception("LSL output failed on %r; stopping it for this scan",
                             channel)
            self._failed = True

    def on_complete(self) -> None:
        try:
            if not self._failed:
                if self._live_rows is not None:
                    self._push(self._live, self._live_rows.drain(flush_all=True))
                if self._corrected_rows is not None:
                    self._push(self._corrected,
                               self._corrected_rows.drain(flush_all=True))
        except Exception:
            logger.exception("LSL output failed flushing the last rows")
        finally:
            # Dropping the outlets withdraws the streams. An LSL inlet keeps
            # the stream's source_id and picks the next scan's stream up again.
            self._live = self._corrected = None

    # ── Payloads ─────────────────────────────────────────────────────────

    def _consume_live(self, batch) -> None:
        """Research layout from a 'live' FrameBatch (the plot's values)."""
        if batch.bfi_live is None or batch.abs_frame_ids is None:
            return
        rows = self._live_rows
        side_ids = getattr(batch, "side_ids", None)
        cam_ids = getattr(batch, "cam_ids", None)
        mean = getattr(batch, "mean_dc_rt", None)
        contrast = getattr(batch, "contrast_sn_rt", None)
        temp = getattr(batch, "temperature_c", None)
        for i in range(batch.bfi_live.shape[0]):
            ft = str(batch.frame_type[i]) if batch.frame_type is not None else "light"
            # The CSV never has warm-up rows; stale frames are rejected data.
            if ft in ("warmup", "stale"):
                continue
            if side_ids is None or cam_ids is None:
                continue
            s, c = int(side_ids[i]), int(cam_ids[i])
            if not (0 <= s < 2 and 0 <= c < 8):
                continue
            side = _SIDES[s]
            fid = int(batch.abs_frame_ids[i])
            t = float(batch.timestamp_s[i])
            col = f"{_SIDE_CHAR[side]}{c + 1}"
            for metric, arr in (("bfi", batch.bfi_live), ("bvi", batch.bvi_live),
                                ("mean", mean), ("contrast", contrast),
                                ("temp", temp)):
                rows.put(fid, t, f"{metric}_{col}",
                         arr[i, s, c] if arr is not None else None)
            rows.mark(fid, side, c)

    def _consume_live_side(self, sample) -> None:
        """Clinical layout from a 'live_side' SideAverageSample."""
        s = int(sample.side)
        if not 0 <= s < 2:
            return
        side = _SIDES[s]
        fid = int(sample.frame_id)
        rows = self._live_rows
        rows.put(fid, sample.t, f"bfi_{side}", sample.bfi)
        rows.put(fid, sample.t, f"bvi_{side}", sample.bvi)
        rows.mark(fid, side, -1)

    def _consume_final(self, interval) -> None:
        """Either layout from a 'final' corrected interval, read the way
        CsvSink reads it: per-camera frames for the research layout, the
        cam_id = -1 side averages for the clinical one."""
        rows = self._corrected_rows
        for frame in interval.frames:
            cam_id = int(getattr(frame, "cam_id", -99))
            side = frame.side
            if side not in _SIDES:
                continue
            fid = int(frame.abs_frame_id)
            t = float(frame.t)
            if self._reduced:
                if cam_id != -1:
                    continue
                rows.put(fid, t, f"bfi_{side}", getattr(frame, "bfi", None))
                rows.put(fid, t, f"bvi_{side}", getattr(frame, "bvi", None))
            else:
                if not 0 <= cam_id < 8:
                    continue
                col = f"{_SIDE_CHAR[side]}{cam_id + 1}"
                for metric, attr in (("bfi", "bfi"), ("bvi", "bvi"),
                                     ("mean", "mean"), ("contrast", "contrast"),
                                     ("temp", "temp_c")):
                    rows.put(fid, t, f"{metric}_{col}", getattr(frame, attr, None))
            rows.mark(fid, side, cam_id)

    # ── Output ───────────────────────────────────────────────────────────

    def _push(self, outlet, rows: list[tuple[float, list[float]]]) -> None:
        if outlet is None or not rows:
            return
        if self._offset is None:
            self._offset = self._clock() - rows[0][0]
        stamps = [self._offset + t for t, _ in rows]
        outlet.push_chunk([r for _, r in rows], stamps)


def _pylsl_outlet(name: str, columns: list[str], source_id: str,
                  desc: dict[str, str]):
    """A pylsl StreamOutlet carrying the CSV columns as labelled channels."""
    import pylsl

    info = pylsl.StreamInfo(name, STREAM_TYPE, len(columns), NOMINAL_SRATE_HZ,
                            pylsl.cf_double64, source_id)
    d = info.desc()
    for key, value in desc.items():
        d.append_child_value(key, str(value))
    chans = d.append_child("channels")
    for label in columns:
        ch = chans.append_child("channel")
        for key, value in channel_metadata(label).items():
            ch.append_child_value(key, value)
    return pylsl.StreamOutlet(info)
