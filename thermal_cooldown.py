"""Thermal cooldown gate for scan-start lockout (issue #102).

Cameras thermally latch (camera-PCB regulator) at ~113-117 degC die temp
with no in-scan recovery, and 10-minute 8-camera scan survival is dominated
by the START temperature (spec:
docs/superpowers/specs/2026-07-05-camera-cooldown-design.md; data:
Projects/investigations/102_thermal_cooldown/). This module owns the
app-side gate: while the pipeline is idle it polls each camera's die
temperature directly from the sensor TPM registers and locks out Start
until every camera is at or below the configured start ceiling.

Split for testability (repo unit-test style — no Qt app required):
- q88_to_celsius / read_camera_temps: pure helpers over the SDK seam.
- CooldownPolicy: pure-Python gate state machine (no Qt, no clock reads).
- CooldownGate: thin QObject shell — daemon poll thread + Qt signals.
"""
from __future__ import annotations

import logging
import math
import threading
import time

from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger("openmotion.app.cooldown")

# OX02C1B behind the TCA9548A mux; TPM die-temp register pair (Q8.8 signed).
# Same path the camera-drift campaign validated as live even when the
# firmware's streamed temps freeze (sensor-fw#73).
_CAM_I2C_ADDR = 0x36
_TPM_TEMP_REG = 0x4D2A


def q88_to_celsius(data) -> float:
    """Decode the 2-byte Q8.8 signed TPM reading to degrees C."""
    raw = (data[0] << 8) | data[1]
    if raw >= 0x8000:
        raw -= 0x10000
    return raw / 256.0


def read_camera_temps(interface) -> dict[tuple[str, int], float]:
    """One idle snapshot of every connected camera's die temp.

    A latched camera is I2C-unreachable and powered-off rails don't answer —
    both are expected states, recorded as NaN (debug log only, never raises).
    """
    temps: dict[tuple[str, int], float] = {}
    if interface is None:
        return temps
    for side in ("left", "right"):
        sensor = getattr(interface, side, None)
        try:
            if sensor is None or not sensor.is_connected():
                continue
        except Exception:
            continue
        for cam in range(8):
            temp = float("nan")
            try:
                data = sensor.i2c_read_register(
                    _CAM_I2C_ADDR, _TPM_TEMP_REG, read_len=2,
                    reg_addr_size=2, mux_channel=cam)
                if data:
                    temp = q88_to_celsius(data)
            except Exception:
                logger.debug("temp read failed %s cam%d", side, cam,
                             exc_info=True)
            temps[(side, cam)] = temp
    return temps
