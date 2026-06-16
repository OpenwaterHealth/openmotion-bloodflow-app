# Critical-Error Codes + Modal — Design

Date: 2026-06-15
Branch: `feature/critical-error-codes-modal` (off `next-next`)

## Goal

Surface critical/showstopper conditions (e.g. the boot-time I2C check failing) to
the clinician as a dismissible modal carrying a stable **error code**, and publish
the authoritative list of codes in the docs.

Today these conditions are only written to the app log; nothing blocks or alerts
the user in the UI. The app already has a toast path (`MotionInterface.notify`)
and a generic `errorOccurred(str)` signal, but neither is coded, blocking, or
self-documenting.

## Scope

In scope (categories chosen by product owner):

- **E-1xx** Startup / initialization failures (I2C check, connect, camera power).
- **E-2xx** Laser-safety showstoppers.
- **E-3xx** Scan / capture aborts.

Out of scope: calibration failures, contact-quality soft warnings (these keep
their existing toast/inline handling).

## Architecture

`motion_connector.py` is ~4031 lines; CLAUDE.md warns against growing it. So:

- **New module `error_codes.py`** — frozen registry of codes. Each entry:
  `code`, `category`, `title`, `message`, `suggested_action`. Plus a
  `CriticalError` dataclass and `lookup(code) -> CriticalError`. Pure, no Qt,
  unit-testable.
- **Connector additions (thin):**
  - `criticalErrorRaised = pyqtSignal(str, str, str, str)  # code, title, message, detail`
  - `_raise_critical(self, code: str, detail: str = "")` — looks up the registry,
    logs, and emits the signal. Emitted via `Qt.QueuedConnection` semantics so
    worker-thread call sites (USB I/O, scanner) are safe.
  - `@pyqtSlot(str) sendBugReport(code)` — see Bug report.
- Existing failure sites add a `self._raise_critical(...)` call. The failure
  paths' own behavior is unchanged; this is an additive surfacing layer.

## Catalog (authoritative list → `docs/ERROR_CODES.md`)

| Code | Condition | Source site |
|---|---|---|
| E-101 | Sensor I2C self-check: expected device(s) missing at boot (mux / IMU / camera / FPGA) | `_check_sensor_i2c_health` (reads `MotionSensor.i2c_health`) |
| E-102 | I2C health unreadable — firmware returned no snapshot | `_check_sensor_i2c_health` (`i2c_health is None`) |
| E-103 | Console init failed — laser-power params didn't apply at connect | `_on_handle_state_changed_impl` console branch |
| E-104 | Console not detected within startup timeout | connection watchdog — **warning toast, not modal** |
| E-105 | Camera power-on failed during init | `_run_sensor_init` |
| E-106 | Fewer sensors than required within startup timeout | connection watchdog — **warning toast, not modal** |
| E-201 | Laser safety monitor unresponsive beyond transient window | safety_known streak block (issue #119) |
| E-202 | Laser safety trip during scan | `_on_safety_trip_during_capture` |
| E-301 | Capture aborted before laser fired (precondition fail) | `startCapture` abort (with reason) |
| E-302 | SDK refused to spawn a new scan | `startCapture` (no reason) |

**Reshaped from the original proposal so every documented code has a real
trigger.** E-103 was repurposed from "sensor not detected" to "console init
failed", which has a clean site. E-203 was merged into E-201; E-303 was dropped
(capture is fail-soft — `captureFinished` always emits success).

**Connection watchdog (added):** because the SDK exposes only
CONNECTED/DISCONNECTED (it retries enumeration internally, with no
"failed attempt" event), E-104/E-106 are driven by a one-shot timer armed at
startup. After `connectionTimeoutSec` (default 30 s) it checks the cached
connection state. Config: `connectionTimeoutSec` / `requireConsole` /
`minSensors`. Post-startup disconnects remain the job of the existing
connection-status UI.

**Severity (updated):** E-104/E-106 are **non-blocking warnings**, not the
critical modal — a missing system at startup is a "plug it in" situation. They
surface as a single yellow toast (bottom-right, `tag="connection-watchdog"`,
sticky); when both are missing it collapses to `"System not found"`. So they are
**not** in the `error_codes.py` modal registry — `_check_connection_watchdog`
calls `notify(..., "warning", ...)` directly and logs the codes for support. The
modal registry holds 8 codes (E-101/102/103/105, E-201/202, E-301/302).

## Modal — `components/CriticalErrorModal.qml`

- Style matches `PasswordPromptModal`. Hosted top-level in `main.qml` so it
  overlays every page. Connected to `MotionInterface.criticalErrorRaised`.
- Dismissible: backdrop click, Dismiss button, Esc.
- Content: red header with **code badge** (`E-101`), title, message,
  suggested-action line, collapsible detail block.
- **Queue**: if several fire, show one at a time; show a "N more" affordance.
- Buttons: **Copy details**, **Send Bug Report to Openwater**, **Dismiss**.

## Bug report (hybrid — works today, upgrades cleanly)

No Openwater backend / mail relay exists today, and embedding SMTP credentials in
a clinical desktop app is undesirable. So:

- **Default (zero infra):** `sendBugReport(code)` builds a report (code, message,
  timestamp, app version, device IDs), reveals the current session log in
  Explorer (`explorer /select,<logpath>`), copies the prefilled report to the
  clipboard, and opens the default mail client via
  `mailto:support@openwater.health?subject=...&body=...`. The user attaches the
  highlighted log and sends. (Desktop apps cannot silently attach a file to an
  arbitrary mail client — this is the honest limitation.)
- **Upgrade (opt-in, true one-click):** if an optional `bug_report_smtp` block is
  present and complete in `app_config.json`, send directly via `smtplib` on a
  background thread with the log **attached automatically**; emit a result toast.
  Absent by default → fallback above.

Config additions:
- `support_email` (default `"support@openwater.health"`).
- optional `bug_report_smtp: { host, port, username, password, use_tls, from_addr }`.

The connector must expose the **current session log path**. `main.py` sets up the
timestamped per-launch log; we surface its path to the connector for the report.

## Testing

- Unit (`@pytest.mark.unit`, mock the seam — no app launch, per repo rule):
  - registry completeness: every catalog code resolves; no dup codes.
  - `_raise_critical` emits the correct 4-tuple payload for a known code.
  - bug-report builder: produces expected report text; selects SMTP vs mailto
    fallback based on config presence.
- QML modal: visual screenshot check (PrintWindow flag 2) after layout — boot-log
  grep won't catch geometry bugs.

## Non-goals / YAGNI

- No retry/auto-recovery from the modal (dismiss only + bug report).
- No new backend service.
- No reworking of the existing toast/`errorOccurred` paths.
