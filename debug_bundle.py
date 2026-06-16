"""Build a zip bundle of recent app logs for emailing to support.

Collects the last N hours of app log files plus app_config.json and a
generated system_info.txt into a single zip. Pure file logic -- no Qt, no
hardware -- so it is unit-testable against a temp directory.
"""

from __future__ import annotations

import datetime
import logging
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

from audit_log import gather_host_info

logger = logging.getLogger("openmotion.bloodflow-app.debug-bundle")

WINDOW_HOURS = 48


def _system_info_text(
    now_epoch: float, extra_info: Optional[Dict[str, Any]]
) -> str:
    """Render host info (+ caller-supplied extras) as sorted key: value
    lines, with a leading local-time 'generated' stamp."""
    info = gather_host_info()
    if extra_info:
        info.update(extra_info)
    generated = (
        datetime.datetime.fromtimestamp(now_epoch)
        .astimezone()
        .isoformat(timespec="seconds")
    )
    lines = [f"generated: {generated}"]
    lines += [f"{k}: {info[k]}" for k in sorted(info)]
    return "\n".join(lines) + "\n"


def build_debug_bundle(
    data_dir: str | Path,
    dest_dir: str | Path,
    now_epoch: float,
    *,
    window_hours: int = WINDOW_HOURS,
    config_path: str | Path | None = None,
    extra_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Zip recent app logs + config + system info into dest_dir.

    Includes <data_dir>/app-logs/*.log with mtime within window_hours,
    the app_config.json at config_path (default <data_dir>/app_config.json)
    if present, and a generated system_info.txt. Returns
    {"path", "file_count", "log_count", "bytes"} where:
      - log_count  = log files that matched the time window.
      - file_count = entries actually written into the zip (logs + config
        if present + system_info). May be < log_count + 1 if a matched log
        fails to add (skipped fail-soft).
      - bytes      = size of the written zip on disk.
    """
    data_dir = Path(data_dir)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    cutoff = now_epoch - window_hours * 3600
    log_dir = data_dir / "app-logs"
    recent_logs = []
    if log_dir.is_dir():
        for p in sorted(log_dir.glob("*.log")):
            try:
                if p.stat().st_mtime >= cutoff:
                    recent_logs.append(p)
            except OSError:
                logger.warning(
                    "debug_bundle: stat failed for %s", p, exc_info=True
                )

    if config_path is None:
        config_path = data_dir / "app_config.json"
    config_path = Path(config_path)

    stamp = datetime.datetime.fromtimestamp(now_epoch).strftime(
        "%Y%m%d_%H%M%S"
    )
    dest = dest_dir / f"debug-bundle-{stamp}.zip"

    file_count = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in recent_logs:
            try:
                zf.write(p, arcname=f"app-logs/{p.name}")
                file_count += 1
            except OSError:
                logger.warning(
                    "debug_bundle: could not add %s", p, exc_info=True
                )
        if config_path.is_file():
            try:
                zf.write(config_path, arcname=config_path.name)
                file_count += 1
            except OSError:
                logger.warning(
                    "debug_bundle: could not add config %s", config_path,
                    exc_info=True,
                )
        zf.writestr(
            "system_info.txt", _system_info_text(now_epoch, extra_info)
        )
        file_count += 1

    return {
        "path": str(dest),
        "file_count": file_count,
        "log_count": len(recent_logs),
        "bytes": dest.stat().st_size,
    }
