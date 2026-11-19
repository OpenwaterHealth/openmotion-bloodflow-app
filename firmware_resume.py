"""Finishing an interrupted device firmware update (#736 / #737).

A bare-metal console or sensor erases its only boot image at the start of a
firmware update. If the write fails while the unit stays powered, the unit
waits in the STM32 ROM loader (USB 0483:DF11) and can be rewritten. A power
cycle starts the half-written image instead and bricks the unit. So the app
has to be able to finish the write, even after a restart:

* firmware downloads land in a durable per-release folder under the data
  root (:func:`cache_dir`), not in %TEMP%;
* a record of the update in flight (:func:`make_record`) is persisted before
  anything is written and cleared once the update finishes, so at startup
  the app knows what a waiting DFU device was being given;
* :func:`register_cached_release` hands the SDK the provenance that
  ``download_firmware()`` registered in the process that downloaded it.

Research builds only: clinical builds never update firmware (#386).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from omotion.firmware_update import (
    FirmwareKind,
    LatestInfo,
    candidate_assets,
    download_firmware,
    register_download,
)

logger = logging.getLogger("openmotion.bloodflow-app.firmware-resume")

#: STATE config key (settings table) holding the record; None when no update
#: is in flight.
PENDING_KEY = "firmwareUpdatePending"

DEVICE_LABELS = {"console": "console", "left": "left sensor", "right": "right sensor"}

_TAG_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def cache_dir(data_root, kind: FirmwareKind, tag: str) -> Path:
    """``<data>/firmware/<kind>/<tag>``: one folder per release, so every
    file in it belongs to that release (the SDK's sibling lookup must not
    pick up a leftover from another version, sdk#218)."""
    return Path(data_root) / "firmware" / kind.value / _TAG_SAFE.sub("_", tag)


def make_record(device: str, kind: FirmwareKind, tag: str, image,
                *, dfu_serial: str | None = None, interrupted: bool = False) -> dict:
    """The persisted description of an update in flight.

    ``interrupted`` is set once the SDK reported that the unit is waiting in
    the ROM loader with incomplete firmware; ``image`` and ``dfu_serial`` are
    then the exact image and ROM-loader serial to retry with."""
    return {
        "device": device,
        "kind": kind.value,
        "tag": tag,
        "image": str(image),
        "dfu_serial": dfu_serial,
        "interrupted": bool(interrupted),
    }


def parse_record(value) -> dict | None:
    """``value`` if it is a well-formed record, else None (a hand-edited or
    half-written settings row must not drive a flash)."""
    if not isinstance(value, dict):
        return None
    if value.get("device") not in DEVICE_LABELS:
        return None
    try:
        FirmwareKind(value.get("kind"))
    except ValueError:
        return None
    if not isinstance(value.get("image"), str) or not isinstance(value.get("tag"), str):
        return None
    serial = value.get("dfu_serial")
    if serial is not None and not isinstance(serial, str):
        return None
    return value


def describe(record: dict | None) -> str:
    return DEVICE_LABELS.get((record or {}).get("device"), "device")


def image_for_resume(record: dict, data_root) -> Path:
    """The image to finish ``record``'s update with, provenance registered.

    Only the record's file *name* is used, and only if it is a firmware
    asset the app downloads for that product: the file comes from the app's
    own cache folder for that release, never from wherever the record
    points. If the folder was cleaned out, the same release is downloaded
    again by tag. Raises RuntimeError when there is nothing safe to flash.
    """
    kind = FirmwareKind(record["kind"])
    tag = record["tag"]
    folder = cache_dir(data_root, kind, tag)
    name = Path(record["image"]).name
    if not candidate_assets(kind, [name]):
        raise RuntimeError(f"{name} is not a {kind.value} firmware image this app "
                           "downloads; refusing to flash it")
    image = folder / name
    if not image.is_file():
        logger.warning("Firmware cache for %s %s is gone; downloading it again",
                       kind.value, tag)
        download_firmware(LatestInfo(kind=kind, tag=tag, asset_name=image.name), folder)
        if not image.is_file():
            raise RuntimeError(f"could not download {image.name} from {kind.value} "
                               f"release {tag} again")
    # download_firmware() registered the release's files with the SDK in the
    # process that downloaded them; a restart lost that. Register everything
    # this release folder holds that the SDK could flash, so update() picks
    # the right image for the boot mode the unit turns out to be in.
    names = [p.name for p in folder.iterdir() if p.is_file()]
    for name in set(candidate_assets(kind, names)) | {image.name}:
        register_download(folder / name, kind, tag)
    return image


def waiting_dfu_serials() -> list[str | None] | None:
    """Serial of every STM32 DFU device on USB (0483:DF11), None for one
    without a readable serial. None if USB could not be enumerated."""
    try:
        from omotion.dfu import STM32DFU

        return [d.get("serial") for d in STM32DFU.list_devices()]
    except Exception:
        logger.warning("Could not enumerate DFU devices", exc_info=True)
        return None
