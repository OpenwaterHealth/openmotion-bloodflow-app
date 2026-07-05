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


class CooldownPolicy:
    """Pure gate state machine. Callers pass ``now`` (monotonic seconds);
    no Qt, no threads, no clock reads — fully unit-testable.

    Lock rule: locked while any finite camera temp exceeds
    ``cooldownStartTempC``; released once every finite temp is at or below
    ``cooldownStartTempC - cooldownHysteresisC``. With no finite temps at
    all, falls back to a timer armed by scan end; with no armed timer,
    fails OPEN (a cool freshly-launched system must not lock, and a
    telemetry fault must not wedge a clinical operator — the firmware
    latch remains the hardware backstop).
    """

    def __init__(self, cfg: dict):
        self.enabled = bool(cfg.get("cooldownEnabled", True))
        self.start_temp_c = float(cfg.get("cooldownStartTempC", 45.0))
        self.hysteresis_c = float(cfg.get("cooldownHysteresisC", 2.0))
        self.timer_fallback_sec = float(cfg.get("cooldownTimerFallbackSec", 600))
        self.tau_sec = float(cfg.get("cooldownTauSec", 0))
        self.ambient_c = float(cfg.get("cooldownAmbientC", 25.0))

        self.locked = False
        self.reason = ""                       # "temp" | "timer" | ""
        self.hottest_c = float("nan")
        self._scan_end: float | None = None    # monotonic; arms the timer
        self._override = False
        self._timer_expiry_logged = False

    # -- inputs --------------------------------------------------------

    def on_scan_ended(self, now: float) -> None:
        """Arm the timer fallback and cancel any engineering override."""
        self._scan_end = now
        self._override = False
        self._timer_expiry_logged = False

    def override(self) -> None:
        """Engineering escape hatch — open until the next scan ends."""
        self._override = True

    def apply(self, temps: dict, now: float) -> bool:
        """Fold a temp snapshot into the gate. True if visible state changed."""
        finite = [t for t in temps.values() if math.isfinite(t)]
        before = (self.locked, self.reason, self._display_hottest())
        self.hottest_c = max(finite) if finite else float("nan")

        if not self.enabled or self._override:
            self.locked, self.reason = False, ""
        elif finite:
            if self.locked:
                if self.hottest_c <= self.start_temp_c - self.hysteresis_c:
                    self.locked, self.reason = False, ""
                else:
                    self.reason = "temp"
            elif self.hottest_c > self.start_temp_c:
                self.locked, self.reason = True, "temp"
        elif self._timer_active(now):
            self.locked, self.reason = True, "timer"
        else:
            if self.locked and self.reason == "timer" \
                    and not self._timer_expiry_logged:
                logger.warning(
                    "no camera temps readable and the post-scan timer "
                    "expired — unlocking (temperature unverified)")
                self._timer_expiry_logged = True
            self.locked, self.reason = False, ""
        return (self.locked, self.reason, self._display_hottest()) != before

    # -- outputs -------------------------------------------------------

    def eta_sec(self, now: float) -> int:
        """Seconds until expected release; 0 = released/imminent, -1 = unknown."""
        if not self.locked:
            return 0
        if self.reason == "timer":
            return max(0, int(self.timer_fallback_sec - (now - self._scan_end)))
        if self.tau_sec <= 0 or not math.isfinite(self.hottest_c):
            return -1
        target = self.start_temp_c - self.hysteresis_c
        if self.hottest_c <= target:
            return 0
        if target <= self.ambient_c:
            return -1
        return int(self.tau_sec * math.log(
            (self.hottest_c - self.ambient_c) / (target - self.ambient_c)))

    # -- internals -----------------------------------------------------

    def _timer_active(self, now: float) -> bool:
        if self.timer_fallback_sec <= 0 or self._scan_end is None:
            return False
        return (now - self._scan_end) < self.timer_fallback_sec

    def _display_hottest(self):
        """Rounded display value (None when unknown) so change detection
        doesn't fire on sub-0.1 deg noise or NaN != NaN."""
        return round(self.hottest_c, 1) if math.isfinite(self.hottest_c) else None
