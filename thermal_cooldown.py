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

    Lock rule: ARMED by a scan ending; while armed, locked while any finite
    camera temp exceeds ``cooldownStartTempC``; released (and disarmed) once
    every finite temp is at or below ``cooldownStartTempC -
    cooldownHysteresisC``. While armed with no finite temps at all, falls
    back to a timer from scan end; on timer expiry, fails OPEN and disarms
    (a telemetry fault must not wedge a clinical operator — the firmware
    latch remains the hardware backstop).

    Why armed-only (#330 characterization, 2026-07-06): the die-temp
    conversion does not run on a powered-but-never-streamed camera — such
    reads are ±50 °C garbage that would randomly lock/unlock the gate.
    Post-scan reads (configured + recently streaming) were verified live
    for ≥45 min. So temperatures are only meaningful — and only consulted —
    between a scan ending and the gate releasing.
    """

    def __init__(self, cfg: dict):
        self.enabled = bool(cfg.get("cooldownEnabled", True))
        self.start_temp_c = float(cfg.get("cooldownStartTempC", 60.0))
        self.hysteresis_c = float(cfg.get("cooldownHysteresisC", 2.0))
        self.timer_fallback_sec = float(cfg.get("cooldownTimerFallbackSec", 600))
        self.tau_sec = float(cfg.get("cooldownTauSec", 510))
        self.ambient_c = float(cfg.get("cooldownAmbientC", 29.0))

        self.armed = False                     # scan ended; temps trustworthy
        self.locked = False
        self.reason = ""                       # "temp" | "timer" | ""
        self.hottest_c = float("nan")
        self._scan_end: float | None = None    # monotonic; arms the timer
        self._override = False
        self._timer_expiry_logged = False

    # -- inputs --------------------------------------------------------

    def on_scan_ended(self, now: float) -> None:
        """Arm the gate (temps are trustworthy from here until release)
        and cancel any engineering override."""
        self.armed = True
        self._scan_end = now
        self._override = False
        self._timer_expiry_logged = False

    def override(self) -> None:
        """Engineering escape hatch — open until the next scan ends."""
        self._override = True

    def apply(self, temps: dict, now: float) -> bool:
        """Fold a temp snapshot into the gate. True if visible state changed."""
        before = (self.locked, self.reason, self._display_hottest())

        if not self.armed:
            # Pre-first-scan reads are garbage (see class docstring) and
            # there is nothing to cool down from — stay open, store nothing.
            self.hottest_c = float("nan")
            self.locked, self.reason = False, ""
            return (self.locked, self.reason,
                    self._display_hottest()) != before

        finite = [t for t in temps.values() if math.isfinite(t)]
        self.hottest_c = max(finite) if finite else float("nan")

        if not self.enabled or self._override:
            self.locked, self.reason = False, ""
        elif finite:
            if self.hottest_c <= self.start_temp_c - self.hysteresis_c:
                # Cool enough to start: release AND disarm — polling stops
                # until the next scan re-arms, so late garbage can never
                # re-lock an idle system.
                self.locked, self.reason = False, ""
                self.armed = False
            elif self.locked:
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
            self.armed = False
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


class CooldownGate(QObject):
    """Qt shell around CooldownPolicy: a daemon thread polls die temps
    while the pipeline is idle; snapshots cross to the GUI thread via a
    queued signal so all policy mutation is single-threaded there."""

    stateChanged = pyqtSignal()
    _snapshotReady = pyqtSignal(object)   # worker thread -> GUI thread

    def __init__(self, cfg: dict, interface_getter, is_idle_fn,
                 parent=None, clock=time.monotonic):
        super().__init__(parent)
        self.policy = CooldownPolicy(cfg)
        self._interface_getter = interface_getter
        self._is_idle_fn = is_idle_fn
        self._clock = clock
        self._poll_interval = max(
            1.0, float(cfg.get("cooldownPollIntervalSec", 5.0)))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshotReady.connect(self._on_snapshot)

    def start(self) -> None:
        if not self.policy.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._poll_loop, name="cooldown-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- worker thread (never raises out — mirrors _run_sensor_init) ----

    def _poll_loop(self) -> None:
        while not self._stop.wait(self._poll_interval):
            try:
                self._tick_once()
            except Exception:
                logger.exception("cooldown poll tick failed")

    def _tick_once(self) -> None:
        if not self.policy.armed:
            return                       # nothing to gate; pre-scan reads
                                         # are garbage anyway (#330 E0)
        if not self._is_idle_fn():
            return                       # scan/config/init owns the bus
        temps = read_camera_temps(self._interface_getter())
        self._snapshotReady.emit(temps)

    # -- GUI thread ------------------------------------------------------

    def _on_snapshot(self, temps) -> None:
        if self.policy.apply(temps, self._clock()):
            self.stateChanged.emit()

    def on_scan_ended(self) -> None:
        self.policy.on_scan_ended(self._clock())

    def request_override(self) -> None:
        self.policy.override()
        # Reflect immediately instead of waiting out a poll interval.
        if self.policy.apply({}, self._clock()):
            self.stateChanged.emit()
