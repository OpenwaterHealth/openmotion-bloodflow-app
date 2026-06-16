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
| E-101 | Sensor I2C bus check failed — expected device(s) missing at boot (mux / IMU / camera / FPGA) | `MotionSensor.is_i2c_healthy()` false after connect |
| E-102 | I2C health unreadable — firmware returned no snapshot | `MotionSensor.i2c_health is None` |
| E-103 | Sensor failed to connect / not detected (on a failed connect attempt) | connection handler |
| E-104 | Console failed to connect / not detected (on a failed connect attempt) | connection handler |
| E-105 | Camera power-on failed during init | `_run_sensor_init` |
| E-201 | Laser safety chip unresponsive at startup | safety read at connect |
| E-202 | Laser safety trip during scan | `safetyTripDuringCaptureRequested` |
| E-203 | Laser safety state unknown beyond transient threshold | safety_known streak (issue #119) |
| E-301 | Capture aborted before laser fired (precondition fail) | `startCapture` abort |
| E-302 | SDK refused to spawn a new scan | `startCapture` |
| E-303 | Capture / pipeline failed | `captureFinished(ok=False)` |

E-103/E-104 fire only on a *failed connect attempt*, not on routine
unplug/disconnect (those keep the existing connection-status UI).

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
