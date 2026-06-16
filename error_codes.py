"""Critical-error code catalog for the BloodFlow app.

Single source of truth for the showstopper conditions surfaced to the user
through the critical-error modal. Pure data — no Qt, no hardware — so it stays
unit-testable and importable from anywhere.

Codes are grouped by subsystem:

    E-1xx  startup / initialization
    E-2xx  laser safety
    E-3xx  scan / capture

The human-readable catalog lives in ``docs/ERROR_CODES.md`` and is generated
from this registry; keep the two in sync.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CriticalError:
    """One showstopper condition.

    Attributes:
        code: stable identifier, e.g. ``"E-101"``.
        category: ``"startup"`` | ``"safety"`` | ``"scan"``.
        title: short headline shown in the modal.
        message: one or two sentences describing what happened.
        suggested_action: what the operator should try.
    """

    code: str
    category: str
    title: str
    message: str
    suggested_action: str


def _e(code, category, title, message, suggested_action) -> CriticalError:
    return CriticalError(code, category, title, message, suggested_action)


# --- E-1xx: startup / initialization ----------------------------------------
_STARTUP = [
    _e(
        "E-101", "startup",
        "Sensor self-check failed",
        "The sensor reported that one or more internal devices (I2C mux, IMU, "
        "a camera, or an FPGA) did not respond during its power-on self-check. "
        "The system cannot scan reliably in this state.",
        "Power-cycle the sensor and reconnect. If it persists, the sensor "
        "hardware needs service — send a bug report.",
    ),
    _e(
        "E-102", "startup",
        "Sensor self-check unreadable",
        "The sensor connected but did not return its power-on self-check "
        "result, so its internal device health is unknown.",
        "Power-cycle the sensor and reconnect. If it persists, update the "
        "sensor firmware or send a bug report.",
    ),
    _e(
        "E-103", "startup",
        "Console initialization failed",
        "The console connected but its laser-power configuration could not be "
        "applied. The laser may not operate correctly until this is resolved.",
        "Power-cycle the console and reconnect. If it persists, send a bug "
        "report — the console firmware or config may need attention.",
    ),
    _e(
        "E-105", "startup",
        "Camera power-on failed",
        "The sensor could not power on its cameras during initialization, so "
        "camera identities could not be read.",
        "Power-cycle the sensor and reconnect. If only some cameras are "
        "affected, the camera board may need service.",
    ),
]

# --- E-2xx: laser safety -----------------------------------------------------
_SAFETY = [
    _e(
        "E-201", "safety",
        "Laser safety monitor unresponsive",
        "The laser-safety monitor stopped reporting a known-good state for "
        "longer than the allowed transient window, so laser safety cannot be "
        "confirmed. The laser was shut off as a precaution.",
        "Power-cycle the system and reconnect. Do not scan until this clears "
        "— send a bug report if it persists; the safety I2C link may be "
        "failing.",
    ),
    _e(
        "E-202", "safety",
        "Laser safety trip",
        "The laser-safety monitor tripped during a scan and the laser was shut "
        "off. The scan was stopped.",
        "Remove any obstruction, let the system settle, and start a new scan. "
        "If it trips repeatedly, stop and send a bug report.",
    ),
]

# --- E-3xx: scan / capture ---------------------------------------------------
_SCAN = [
    _e(
        "E-301", "scan",
        "Scan aborted before start",
        "A scan was requested but a precondition check failed, so the scan was "
        "aborted before the laser fired.",
        "Resolve the reported precondition (connection, safety, or "
        "configuration) and start the scan again.",
    ),
    _e(
        "E-302", "scan",
        "Scan could not start",
        "The system refused to start a new scan, usually because a previous "
        "scan is still finishing.",
        "Wait a few seconds for the previous scan to finish, then try again. "
        "Reconnect if the system stays busy.",
    ),
]


ERROR_CODES: dict[str, CriticalError] = {
    err.code: err for err in (*_STARTUP, *_SAFETY, *_SCAN)
}


def lookup(code: str) -> CriticalError:
    """Return the catalog entry for ``code``.

    Never raises: an unknown code yields a generic entry that preserves the
    code, so the modal can always show *something* even for a code that
    predates this build.
    """
    err = ERROR_CODES.get(code)
    if err is not None:
        return err
    return CriticalError(
        code=code,
        category="startup",
        title="Critical error",
        message="A critical error occurred.",
        suggested_action="Send a bug report with the session log attached.",
    )
