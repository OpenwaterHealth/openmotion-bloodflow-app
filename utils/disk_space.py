"""Free-disk-space checks for the data drive (issue #506).

Pure helpers — no Qt, no hardware — so the connector's storage checks stay
unit-testable. The connector applies two thresholds (config/app_config.py):

  - ``minFreeDiskMb`` (1 GB): under it the app raises a critical error at
    startup (E-107) and when Start is pressed (E-305), and a running scan
    gets a one-time warning toast;
  - ``scanStopFreeDiskMb`` (100 MB): under it a running scan is stopped
    gracefully with a critical error (E-306), leaving room for scans.db to
    finalize instead of failing on a full disk.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Optional

logger = logging.getLogger("openmotion.bloodflow-app.disk-space")

MB = 1024 * 1024


def free_bytes(path: str) -> Optional[int]:
    """Free bytes on the volume holding ``path``, or None if unreadable.

    ``path`` need not exist yet: the nearest existing ancestor is asked
    instead (the data root is created lazily). None means "unknown" —
    callers must not block on a failed query.
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


def below(free: Optional[int], limit_mb: float) -> bool:
    """True when known free space is under ``limit_mb``. An unknown
    ``free`` (None) or a non-positive limit (check disabled) is never
    below."""
    if free is None or limit_mb <= 0:
        return False
    return free < limit_mb * MB
