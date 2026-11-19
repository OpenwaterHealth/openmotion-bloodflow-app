# Critical Error Codes

When the BloodFlow app hits a showstopper condition it raises a **critical-error
modal** carrying a stable code (e.g. `E-101`). The modal is dismissible and
offers **Copy details** and **Contact Support**.

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
hardware needs service — contact support.

### E-102 — Sensor self-check unreadable
The sensor connected but did not return its power-on self-check result, so its
internal device health is unknown (typically older firmware, or the status
command was rejected).

**What to do:** Power-cycle the sensor and reconnect. If it persists, update the
sensor firmware or contact support.

### E-103 — Console initialization failed
The console connected but its laser-power configuration could not be applied.
The laser may not operate correctly until this is resolved.

**What to do:** Power-cycle the console and reconnect. If it persists, contact
support — the console firmware or config may need attention.

### E-105 — Camera power-on failed
The sensor could not power on its cameras during initialization, so camera
identities could not be read.

**What to do:** Power-cycle the sensor and reconnect. If only some cameras are
affected, the camera board may need service.

### E-107 — Storage almost full
The drive that stores scan data has less than 1 GB free. Scans cannot be started
until space is freed. Checked once at app launch, against `minFreeDiskMb`
(default 1024 MB); the modal detail shows the free space and the data folder. A
free-space query that fails raises nothing.

**What to do:** Free up space on the data drive (or export and remove old
scans) before starting a scan — no restart needed, Start re-checks. Contact
support if the drive should not be full.

### E-108 — Scan storage inaccessible
The scan database in the data folder is not encrypted. This build stores
patient data only in encrypted form, so it will not open that database and
scans cannot be started. Checked at every launch of a clinical build, before
anything opens `scans.db`; the modal detail shows the database path. A
plaintext `scans.db` can be left in the data folder by a Research install or by
a version before 1.5.0, and the app never converts it. The app stays open, but
it never reads or writes that file: settings last for the session only, and
pressing Start is refused with E-301.

**What to do:** Do not delete or move the data folder: it may hold patient
data. Contact support with the session log attached.

## E-2xx — Laser safety

### E-201 — Laser safety monitor unresponsive
The laser-safety monitor stopped reporting a known-good state for longer than
the allowed transient window, so laser safety cannot be confirmed. The laser was
shut off as a precaution.

**What to do:** Power-cycle the system and reconnect. Do not scan until this
clears — contact support if it persists; the safety I2C link may be failing.

### E-202 — Laser safety trip
The laser-safety monitor tripped during a scan and the laser was shut off. The
scan was stopped.

**What to do:** Remove any obstruction, let the system settle, and start a new
scan. If it trips repeatedly, stop and contact support.

### E-203 — Laser safety trip
The laser-safety monitor tripped while no scan was running and the laser was
shut off. The safety interlock stays latched until the console is power-cycled.
Raised for a trip at any time outside a scan (idle, the preflight signal-quality
check, test/calibrate); a trip during a scan raises E-202 instead. The
persistent laser-safety toast stays up until the monitor reports clear.

**What to do:** Power-cycle the console and reconnect before starting a scan. If
it trips again, stop and contact support.

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
new scan. If it happens repeatedly, contact support.

### E-304 — Device disconnected during scan
A console or sensor was disconnected while the scan was running, so the scan was
stopped. Some data loss may have occurred — data captured before the
disconnection is not guaranteed to have been saved. Fired when a device
participating in the running scan — the console, or a sensor whose camera mask
was non-zero at scan start — drops off USB during the trigger-on capture phase. Distinct from E-303: a physical unplug rarely trips the data-stall
watchdog (the SDK tears the scan down first), so this is the coded modal for a
device being removed mid-scan. Unplugging an idle / masked-out sensor, or any
device while not scanning, does not raise this.

**What to do:** Check the USB cables and power, reconnect the system, and start a
new scan. If it keeps happening, contact support.

### E-305 — Not enough storage to start scan
The drive that stores scan data has less than 1 GB free, so the scan was not
started and the laser did not fire. Checked when **Start** is pressed — before
the clinical pre-scan contact check — and again when the capture itself starts,
against `minFreeDiskMb` (default 1024 MB). A free-space query that fails does
not block the scan.

**What to do:** Free up space on the data drive (or export and remove old
scans), then start the scan again. Contact support if the drive should not be
full.

### E-306 — Scan stopped: storage almost full
The drive that stores scan data dropped below 100 MB free during the scan, so
the scan was stopped before the drive filled up. Data captured so far was saved.
Checked every 5 s while a scan runs, against `scanStopFreeDiskMb` (default
100 MB). The scan is stopped gracefully, like pressing Stop. The remaining space
lets the scan database finish writing. The modal replaces the "Storage is
running low" warning toast (see [Storage warnings](#storage-warnings-during-a-scan)),
and its detail shows the free space, the data folder and the scan time elapsed.

**What to do:** Free up space on the data drive (or export and remove old
scans) before starting another scan. Contact support if the drive should not be
full.

## Startup warnings (connection watchdog)

A one-shot check armed at app launch flags expected devices that never showed
up. Unlike the codes above it is **non-blocking**: it shows a **yellow warning
toast** in the bottom-right, not the critical modal, because the fix is usually
just "plug it in and reconnect". If the expected devices haven't enumerated
within `connectionTimeoutSec` (default 12 s) after launch:

- **E-104 — Console not detected** → `"Console not detected. Check the console
  USB cable and power, then reconnect."`
- **E-106 — Sensor not detected** → `"Sensor not detected. Check the sensor USB
  cable and power, then reconnect."`
- **Both missing** → a single consolidated toast: `"System not found. Check that
  the console and sensor are connected and powered on."`

The codes E-104/E-106 still appear in the app log for support traceability.

A device that is still connecting (mid-handshake) when the timeout expires is
not reported yet: the check runs once more 10 s later and reports whatever is
still not connected then. The toast comes down by itself as soon as the devices
it warned about connect; otherwise it auto-dismisses after 10 s.

Tunable in [`config/app_config.json`](../config/app_config.json):

- `connectionTimeoutSec` (default `12`) — grace period before the check runs;
  `0` disables the watchdog.
- `requireConsole` (default `true`) — warn (E-104) if no console connected.
- `minSensors` (default `1`) — warn (E-106) if fewer than this many sensors
  connected.

Disconnects that happen *after* startup are handled by the normal connection
status UI, not by this watchdog.

## Storage warnings (during a scan)

While a scan runs the app re-checks free space on the data drive every 5 s.

- **Below 1 GB free** (`minFreeDiskMb`) → a **yellow warning toast**, shown once
  per scan and kept until dismissed: `"Storage is running low: <n> MB free on
  the data drive. The scan will stop automatically at 100 MB."` The scan keeps
  running.
- **Below 100 MB free** (`scanStopFreeDiskMb`) → the scan is stopped and the
  [E-306](#e-306--scan-stopped-storage-almost-full) critical modal replaces the
  toast. E-306 is also the audit log's `scan_ended` abort code.

Either threshold set to `0` or below disables its checks.

## Contacting support

The **Contact Support** button packages the current session log plus the error
context for support.

- By default it opens your mail client with a pre-filled message to
  `support@openwater.health`, copies the report to your clipboard, and reveals
  the session log file so you can attach it before sending.
- If an SMTP relay is configured (`bug_report_smtp` in
  [`config/app_config.json`](../config/app_config.json)), the report is sent
  automatically with the log attached — no manual step.

The support address is configurable via the `support_email` key in
`config/app_config.json`.
