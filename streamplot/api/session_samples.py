"""
session_samples.py - Raw sample data types and CSV reader.
"""

import os
import re
from datetime import datetime
from typing import Optional
import numpy as np
import pandas as pd
import numpy.typing as npt
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Data type
# ---------------------------------------------------------------------------

class Sample(NamedTuple):
    """One raw histogram frame from one camera."""
    side:     str                      # 'left' or 'right'
    cam_id:   np.uint32
    frame_id: np.uint32
    timestamp: np.float32
    hist:     npt.NDArray[np.uint32]   # 1024 bins
    temp:     np.float32
    summ:     np.uint64
    tcm:      np.float32 = np.float32(0.0)
    tcl:      np.float32 = np.float32(0.0)
    pdc:      np.float32 = np.float32(0.0)


# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------

# Expected filename pattern:
#   scan_{session_id}_{YYYYMMDD}_{HHMMSS}_{side}_mask{hexMask}.csv
_CSV_RE = re.compile(
    r"scan_(?P<session_id>[A-Za-z0-9]+)"
    r"_(?P<date>\d{8})"
    r"_(?P<time>\d{6})"
    r"_(?P<side>left|right)"
    r"_mask(?P<mask>[0-9A-Fa-f]+)"
    r"\.csv$",
    re.IGNORECASE,
)

#   scan_{session_id}_{YYYYMMDD}_{HHMMSS}_notes.txt
_NOTES_RE = re.compile(
    r"scan_(?P<session_id>[A-Za-z0-9]+)"
    r"_(?P<date>\d{8})"
    r"_(?P<time>\d{6})"
    r"_notes\.txt$",
    re.IGNORECASE,
)


def parse_session_csv_filename(filename):
    """
    Parse a scan CSV filename.
    Returns dict with keys: session_id, datetime, side, mask  or  None.
    """
    m = _CSV_RE.search(os.path.basename(filename))
    if m is None:
        return None
    dt = datetime.strptime(m.group("date") + m.group("time"), "%Y%m%d%H%M%S")
    return {
        "session_id": m.group("session_id"),
        "datetime":   dt,
        "side":       m.group("side").lower(),
        "mask":       int(m.group("mask"), 16),
    }


def find_session_files(directory, session_id):
    """
    Locate scan files for session_id in directory.
    Returns dict with keys: left, right, notes (each a path or None).
    """
    result = {"left": None, "right": None, "notes": None}
    try:
        entries = os.listdir(directory)
    except OSError:
        return result
    for fname in entries:
        m = _CSV_RE.search(fname)
        if m and m.group("session_id") == session_id:
            result[m.group("side").lower()] = os.path.join(directory, fname)
            continue
        mn = _NOTES_RE.search(fname)
        if mn and mn.group("session_id") == session_id:
            result["notes"] = os.path.join(directory, fname)
    return result


# ---------------------------------------------------------------------------
# CSV reader
# ---------------------------------------------------------------------------

class SessionSamples:
    """
    Reads a raw histogram CSV file into memory and vends Sample objects.

    Column layout:
        col 0       : cam_id
        col 1       : frame_id
        col 2       : timestamp_s
        col 3..1026 : hist[0..1023]
        col 1027    : temperature
        col 1028    : sum
        col 1029    : tcm  (optional)
        col 1030    : tcl  (optional)
        col 1031    : pdc  (optional)
    """

    def __init__(self):
        self.rows  = None
        self.ncams = None
        self.side  = "left"

    def read_csv(self, csv_path, side="left"):
        """Load CSV. Returns number of rows loaded."""
        self.side = side.lower()
        self.rows = np.array(pd.read_csv(csv_path, dtype=np.float32))
        camera_inds = np.unique(self.rows[:, 0])
        self.ncams  = len(camera_inds)
        return self.rows.shape[0]

    def size(self):
        return 0 if self.rows is None else self.rows.shape[0]

    def get(self, i):
        row   = self.rows[i]
        ncols = row.shape[0]
        tcm = np.float32(row[1029]) if ncols > 1029 else np.float32(0.0)
        tcl = np.float32(row[1030]) if ncols > 1030 else np.float32(0.0)
        pdc = np.float32(row[1031]) if ncols > 1031 else np.float32(0.0)
        return Sample(
            side     = self.side,
            cam_id   = np.uint32(row[0]),
            frame_id = np.uint32(row[1]),
            timestamp= np.float32(row[2]),
            hist     = row[3:1027].astype(np.uint32),
            temp     = np.float32(row[1027]),
            summ     = np.uint64(row[1028]),
            tcm      = tcm,
            tcl      = tcl,
            pdc      = pdc,
        )


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: session_samples.py <csv_path> [left|right]")
        sys.exit(0)
    csv_path = sys.argv[1]
    side = sys.argv[2] if len(sys.argv) > 2 else "left"
    sd = SessionSamples()
    n = sd.read_csv(csv_path, side=side)
    print(f"Loaded {n} rows, {sd.ncams} cameras, side={side}")
    s = sd.get(0)
    print(f"First sample: cam_id={s.cam_id} frame_id={s.frame_id} ts={s.timestamp:.3f}")
