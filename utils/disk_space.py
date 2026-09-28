"""Free-disk-space guard for scan output (issue #506).

Pure helpers — no Qt, no hardware — so the connector's two checks stay
unit-testable:

  - before a scan starts, refuse when the data drive cannot hold the
    estimated scan output plus a reserve (E-305);
  - while a scan runs, stop it cleanly once free space falls to a floor
    (E-306), so the pipeline can still finalize scans.db instead of
    failing on a full disk.

The size estimate is deliberately conservative and approximate. The rates
were measured on real scans (2026-09): a ``session_data`` row is ~131 B,
one per camera per frame at 40 Hz (~5.3 KB/s per camera); a raw histogram
CSV runs ~0.12 MB/s per camera. The mid-scan floor is the real guarantee —
the estimate only keeps a scan that obviously cannot fit from starting.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Optional

logger = logging.getLogger("openmotion.bloodflow-app.disk-space")

MB = 1024 * 1024

# Per-camera write rates, rounded up from the measurements above.
DB_BYTES_PER_CAMERA_S = 8 * 1024
RAW_CSV_BYTES_PER_CAMERA_S = 160 * 1024


def free_bytes(path: str) -> Optional[int]:
    """Free bytes on the volume holding ``path``, or None if unreadable.

    ``path`` need not exist yet: the nearest existing ancestor is asked
    instead (the data root is created lazily). None means "unknown" —
    callers must not block a scan on a failed query.
    """
    probe = os.path.abspath(path)
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return int(shutil.disk_usage(probe).free)
    except OSError as exc:
        logger.warning("free-space query failed for %s: %s", probe, exc)
        return None


def estimate_scan_bytes(
    duration_s: float,
    n_cameras: int,
    raw_csv_max_s: Optional[float] = 0,
    auto_export: bool = False,
) -> int:
    """Rough upper estimate of what one scan writes to the data drive.

    ``raw_csv_max_s`` follows the pipeline's ``raw_save_max_duration_s``
    contract: 0 = no raw CSVs, None = raw for the whole scan, otherwise
    raw for at most that many seconds. ``auto_export`` adds the scan-end
    CSV export (#598), budgeted at the DB rate.
    """
    duration_s = max(0.0, float(duration_s))
    n_cameras = max(0, int(n_cameras))
    per_cam = DB_BYTES_PER_CAMERA_S * duration_s
    if auto_export:
        per_cam += DB_BYTES_PER_CAMERA_S * duration_s
    if raw_csv_max_s is None:
        raw_s = duration_s
    else:
        raw_s = min(duration_s, max(0.0, float(raw_csv_max_s)))
    per_cam += RAW_CSV_BYTES_PER_CAMERA_S * raw_s
    return int(per_cam * n_cameras)


def start_shortfall(
    free: Optional[int], estimate: int, reserve: int,
) -> Optional[int]:
    """Bytes missing to start a scan, or None when it may start.

    A scan needs ``estimate + reserve`` free. An unknown ``free`` (None)
    never blocks: the mid-scan floor still protects the drive.
    """
    if free is None:
        return None
    needed = int(estimate) + max(0, int(reserve))
    return needed - free if free < needed else None


def below_floor(free: Optional[int], floor: int) -> bool:
    """True when a running scan must stop: known free space at or under
    ``floor``. A non-positive floor disables the mid-scan stop."""
    if free is None or floor <= 0:
        return False
    return free <= floor
