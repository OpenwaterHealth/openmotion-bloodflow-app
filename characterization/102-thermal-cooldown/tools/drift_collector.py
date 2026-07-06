"""Pipeline sink that reduces raw histogram batches to per-frame statistics.

Subscribes to the "raw" channel of the omotion scan pipeline. For every
frame/camera it records:
    side, cam_id, frame_id, timestamp_s, frame_type,
    mean_dn, std_dn, row_sum, die_temp_c, pdc, tcm, tcl

Memory: a 20-min 16-cam scan at 40 Hz ≈ 768k row tuples (a few hundred MB
with Python object overhead). Multi-hour scans would exceed RAM, so an
optional spill_path streams rows to disk every spill_every rows; save()
then just flushes the remainder and renames the spill file into place.
"""
from __future__ import annotations

import csv
import logging
import os

import numpy as np

logger = logging.getLogger("campaign.collector")

BIN_IDX = np.arange(1024, dtype=np.float64)
BIN_IDX2 = BIN_IDX ** 2

CSV_HEADER = [
    "side", "cam_id", "frame_id", "timestamp_s", "type",
    "mean_dn", "std_dn", "row_sum", "die_temp_c",
    "pdc", "tcm", "tcl",
]


def _scalar(arr, i):
    try:
        v = arr[i]
        return float(v) if v is not None else float("nan")
    except Exception:
        return float("nan")


class DriftCollector:
    """Raw-channel sink accumulating per-frame stats in memory."""

    channels = {"raw"}

    def __init__(self, spill_path: str | None = None,
                 spill_every: int = 200_000) -> None:
        self.rows: list[tuple] = []
        self.meta = None
        self._spill_path = spill_path
        self._spill_every = spill_every
        self._spilled = 0

    def on_scan_start(self, meta) -> None:
        self.rows.clear()
        self.meta = meta
        self._spilled = 0
        if self._spill_path and os.path.exists(self._spill_path):
            os.remove(self._spill_path)

    def _flush_spill(self) -> None:
        new_file = not os.path.exists(self._spill_path)
        with open(self._spill_path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new_file:
                w.writerow(CSV_HEADER)
            w.writerows(self.rows)
        self._spilled += len(self.rows)
        self.rows.clear()

    def consume(self, channel: str, batch) -> None:
        if channel != "raw":
            return
        try:
            self._consume_raw(batch)
        except Exception:
            logger.exception("DriftCollector consume failed")

    _SIDE_NAMES = ("left", "right")

    def _consume_raw(self, batch) -> None:
        for i, side_idx, cam_id, frame_type in batch.iter_rows(exclude={"stale"}):
            if side_idx not in (0, 1):
                continue
            frame_id = int(batch.frame_ids[i])
            ts = float(batch.timestamp_s[i])
            pdc = _scalar(batch.pdc, i) if batch.pdc is not None else float("nan")
            tcm = _scalar(batch.tcm, i) if batch.tcm is not None else float("nan")
            tcl = _scalar(batch.tcl, i) if batch.tcl is not None else float("nan")
            side_name = self._SIDE_NAMES[side_idx]
            histo = np.asarray(
                batch.raw_histograms[i, side_idx, cam_id, :], dtype=np.float64
            )
            total = float(histo.sum())
            if total <= 0:
                mean = std = float("nan")
            else:
                mean = float(histo @ BIN_IDX) / total
                ex2 = float(histo @ BIN_IDX2) / total
                std = float(np.sqrt(max(ex2 - mean * mean, 0.0)))
            temp = (
                float(batch.temperature_c[i, side_idx, cam_id])
                if batch.temperature_c is not None else float("nan")
            )
            self.rows.append((
                side_name, cam_id, frame_id, ts, frame_type,
                mean, std, total, temp, pdc, tcm, tcl,
            ))
        if self._spill_path and len(self.rows) >= self._spill_every:
            self._flush_spill()

    def on_complete(self) -> None:
        logger.info("DriftCollector: %d rows collected (%d spilled)",
                    len(self.rows) + self._spilled, self._spilled)

    def save(self, path: str) -> int:
        if self._spill_path:
            self._flush_spill()
            total = self._spilled
            os.replace(self._spill_path, path)
            return total
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(CSV_HEADER)
            w.writerows(self.rows)
        return len(self.rows)
