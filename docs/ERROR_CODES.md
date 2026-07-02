# Critical Error Codes

When the BloodFlow app hits a showstopper condition it raises a **critical-error
modal** carrying a stable code (e.g. `E-101`). The modal is dismissible and
offers **Copy details** and **Send Bug Report to Openwater**.

Codes are the single source of truth in [`error_codes.py`](../error_codes.py);
this document is generated from that registry. They are grouped by subsystem:

- **E-1xx** — startup / initialization
- **E-2xx** — laser safety
- **E-3xx** — scan / capture

When reporting a problem, quote the code — it tells support exactly which check
failed.

Not every coded condition is critical: the **startup connection watchdog**
(E-104 / E-106) is a non-blocking *warning* shown as a yellow toast, not the
modal — see [Startup warnings](#startup-warnings-connection-watchdog) below.

## E-1xx — Startup / initialization

### E-101 — Sensor self-check failed
One or more of the sensor's internal I2C devices (the I2C mux, the IMU, a
camera, or an FPGA) did not respond during the firmware's power-on self-check.
The system cannot scan reliably in this state.

**What to do:** Power-cycle the sensor and reconnect. If it persists, the sensor
hardware needs service — send a bug report.

### E-102 — Sensor self-check unreadable
The sensor connected but did not return its power-on self-check result, so its
internal device health is unknown (typically older firmware, or the status
command was rejected).

**What to do:** Power-cycle the sensor and reconnect. If it persists, update the
sensor firmware or send a bug report.

### E-103 — Console initialization failed
The console connected but its laser-power configuration could not be applied.
The laser may not operate correctly until this is resolved.

**What to do:** Power-cycle the console and reconnect. If it persists, send a bug
report — the console firmware or config may need attention.

### E-105 — Camera power-on failed
The sensor could not power on its cameras during initialization, so camera
identities could not be read.

**What to do:** Power-cycle the sensor and reconnect. If only some cameras are
affected, the camera board may need service.

## E-2xx — Laser safety

### E-201 — Laser safety monitor unresponsive
The laser-safety monitor stopped reporting a known-good state for longer than
the allowed transient window, so laser safety cannot be confirmed. The laser was
shut off as a precaution.

**What to do:** Power-cycle the system and reconnect. Do not scan until this
clears — send a bug report if it persists; the safety I2C link may be failing.

### E-202 — Laser safety trip
The laser-safety monitor tripped during a scan and the laser was shut off. The
scan was stopped.

**What to do:** Remove any obstruction, let the system settle, and start a new
scan. If it trips repeatedly, stop and send a bug report.

## E-3xx — Scan / capture

### E-301 — Scan aborted before start
A scan was requested but a precondition check failed, so the scan was aborted
before the laser fired.

**What to do:** Resolve the reported precondition (connection, safety, or
configuration) and start the scan again.

### E-302 — Scan could not start
The system refused to start a new scan, usually because a previous scan is still
finishing.

**What to do:** Wait a few seconds for the previous scan to finish, then try
again. Reconnect if the system stays busy.

### E-303 — Camera data lost during scan
Every camera stopped delivering data and acquisition could not continue, so the
scan was stopped. Data captured before the loss was saved. Fired by the scan
data-stall watchdog when no camera has delivered a frame for
`scanDataStallTimeoutSec` (default 3 s) while the trigger is ON.

**What to do:** Check the sensor cables and power, then reconnect and start a
new scan. If it happens repeatedly, send a bug report.

## Startup warnings (connection watchdog)

A one-shot check armed at app launch flags expected devices that never showed
up. Unlike the codes above it is **non-blocking**: it shows a **yellow warning
toast** in the bottom-right, not the critical modal, because the fix is usually
just "plug it in and reconnect". If the expected devices haven't enumerated
within `connectionTimeoutSec` (default 30 s) after launch:

- **E-104 — Console not detected** → `"Console not detected. Check the console
  USB cable and power, then reconnect."`
- **E-106 — Sensor not detected** → `"Sensor not detected. Check the sensor USB
  cable and power, then reconnect."`
- **Both missing** → a single consolidated toast: `"System not found. Check that
  the console and sensor are connected and powered on."`

The codes E-104/E-106 still appear in the app log for support traceability.

Tunable in [`config/app_config.json`](../config/app_config.json):

- `connectionTimeoutSec` (default `30`) — grace period before the check runs;
  `0` disables the watchdog.
- `requireConsole` (default `true`) — warn (E-104) if no console connected.
- `minSensors` (default `1`) — warn (E-106) if fewer than this many sensors
  connected.

Disconnects that happen *after* startup are handled by the normal connection
status UI, not by this watchdog.

## Sending a bug report

The **Send Bug Report to Openwater** button packages the current session log plus
the error context for support.

- By default it opens your mail client with a pre-filled message to
  `support@openwater.health`, copies the report to your clipboard, and reveals
  the session log file so you can attach it before sending.
- If an SMTP relay is configured (`bug_report_smtp` in
  [`config/app_config.json`](../config/app_config.json)), the report is sent
  automatically with the log attached — no manual step.

The support address is configurable via the `support_email` key in
`config/app_config.json`.
