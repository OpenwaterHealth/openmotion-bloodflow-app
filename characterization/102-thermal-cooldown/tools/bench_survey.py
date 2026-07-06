"""Bench-log survey for the #102 characterization runbook.

Connects, then prints firmware versions, hardware IDs, camera chip IDs and
current die temps for both modules — the attribution record the runbook
requires before any experiment. Read-only; safe on an idle bench.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.environ.get(
    "OWCAM_TOOLS", os.path.dirname(os.path.abspath(__file__))))

import owcam  # noqa: E402


def _safe(fn, default="?"):
    try:
        v = fn()
        return v if v not in (None, "") else default
    except Exception as e:
        return f"? ({type(e).__name__})"


def main() -> int:
    power_on = "--power-on" in sys.argv
    iface = owcam.connect()
    try:
        if power_on:
            import time
            for side in ("left", "right"):
                sensor = owcam.sensor_for(iface, side)
                ok = sensor.enable_camera_power(0xFF)
                print(f"{side}: enable_camera_power(0xFF) -> {ok}")
            time.sleep(1.0)
        print("== console ==")
        print("  fw:", _safe(lambda: iface.console.get_version()))
        print("  hw_id:", _safe(lambda: iface.console.get_hardware_id()))
        for side in ("left", "right"):
            sensor = owcam.sensor_for(iface, side)
            print(f"== {side} sensor ==")
            print("  fw:", _safe(lambda: sensor.get_version()))
            print("  hw_id:", _safe(lambda: sensor.get_hardware_id()))
        info = owcam.survey(iface, cams=list(range(8)))
        for side in ("left", "right"):
            ids = info.chip_ids[side]
            temps = info.temps[side]
            print(f"== {side} cameras ==")
            for cam in range(8):
                print(f"  cam{cam}: chip_id=0x{ids[cam]:04X}  "
                      f"die_temp={temps[cam]:.1f} C")
    finally:
        try:
            iface.stop()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
