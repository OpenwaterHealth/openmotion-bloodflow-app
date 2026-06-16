# Audit Log — Design

**Date:** 2026-06-15
**Status:** Approved (design); pending spec review
**Branch base:** `next-next`

## Goal

Add a machine-readable, append-only audit log to the bloodflow app's
SQLite database (`scans.db`). Entries are intended primarily for
**auditors**: minimal, structured, and exportable. The log is reached
through a password-gated **Logs** button in the Settings panel, which
opens a modal that lists entries and can export them as CSV.

## Decisions (locked)

- **Password gate:** reuse the existing developer password
  (`_DEVELOPER_PASSWORD` / `MotionInterface.checkDeveloperPassword`,
  motion_connector.py:71) — same mechanism as the Calibrate gate.
- **Entry shape:** minimal fixed core columns + a JSON `details` blob
  (typed-event + JSON details).
- **Table location:** app-side, in the same `scans.db` file. No SDK
  (`openmotion-sdk`) edits.

## Architecture

A new standalone module **`audit_log.py`** with an `AuditLog` class.

- Owns its own `sqlite3` connection to `scan_db_path`
  (`check_same_thread=False`), lazily creates the `logs` table, and
  exposes a small API: `log(event_type, details=None)`, `query(limit)`,
  `export_csv(dest_path)`, `close()`.
- The connector (`MotionConnector`) holds a single `AuditLog` instance,
  constructed in `__init__` from
  `getattr(self._interface, "scan_db_path", None)`. Call sites are thin
  one-liners (`self._audit.log("scan_started", {...})`).
- Rationale: keeps the already-oversized `motion_connector.py` (4031
  lines) from growing materially, mirrors the `motion_config.py`
  extraction precedent, and makes the logger unit-testable against a
  tmp DB with no app launch / no hardware.

### Concurrency

Audit events fire from multiple threads — the SDK connection-monitor
thread (`_on_handle_state_changed_impl`), scan-worker threads
(scan end), and the Qt UI thread (settings, view, delete). Therefore:

- `sqlite3.connect(path, check_same_thread=False)`.
- A `threading.Lock` guarding every write.
- `PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=5000` — coexists with
  the SDK's `ScanDatabase`, which also opens WAL connections to the same
  file on demand.

### Fail-soft

`AuditLog` must never crash or block the app. If `scan_db_path` is
`None` (e.g. headless/unit context) or the connection/insert raises,
the instance degrades to a no-op and logs a single warning via the
standard `logging` logger. A failed audit write never propagates.

## Schema

Created by `AuditLog._init_schema()` (idempotent, `IF NOT EXISTS`):

```sql
CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY,
    ts_epoch    REAL NOT NULL,   -- time.time()
    ts_iso      TEXT NOT NULL,   -- ISO-8601 local time, sortable
    event_type  TEXT NOT NULL,   -- one of the event-type constants below
    details     TEXT             -- compact JSON object; NULL when no payload
);

CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts_epoch);
```

`details` is `json.dumps(obj, separators=(",", ":"), sort_keys=True)` —
compact and deterministic. Stored as `NULL` when there is no payload.

## Event types & instrumentation points

Event-type strings are module-level constants in `audit_log.py`.

| event_type | Fired at (file:line anchor) | `details` |
|---|---|---|
| `system_startup` | `MotionConnector.__init__` | `app_version`, `sdk_version`, `data_dir` |
| `system_info` | `__init__`, immediately after `system_startup` | `hostname`, `platform`, `system`, `arch`, `processor`, `python`, `ram_gb` (host info, mirrors SDK `log_system_info`) |
| `system_shutdown` | `MotionConnector.shutdown()` (called from `main.py` `handle_exit`, :294) | `clean: true` |
| `device_connected` | `_on_handle_state_changed_impl` (motion_connector.py:1137) | `device` (`console`/`left`/`right`), `reason` |
| `device_disconnected` | `_on_handle_state_changed_impl` | `device`, `reason` |
| `device_stats` | on connect, once IDs are readable (same handler) | `device`, `hardware_id`, `firmware_version` |
| `scan_started` | `startCapture` (motion_connector.py:2183) | `label`, `left_mask`, `right_mask`, `session_id` (`null` if not yet assigned — `ScanDBSink` may create the session row after this point; `scan_ended` carries the authoritative id) |
| `scan_ended` | `stopCapture` (motion_connector.py:1320) / scan-complete | `session_id`, `duration_s`, `outcome` |
| `calibration_started` | `runCalibration` (motion_connector.py:3645, status→running at :3721) | `target` (`both`/`left`/`right`) |
| `calibration_ended` | `_on_calibration_complete` (motion_connector.py:3935) | `target`, `outcome` (`passed`/`failed`/`aborted`), `reason` |
| `settings_changed` | `setConfig` (:1785) and `saveConfigs` (:1793) | changed keys only, as `{key: {old, new}}` (diff computed before the update is applied) |
| `scan_viewed` | `loadPastScan` (motion_connector.py:1923) | `session_id`, `label` |
| `scan_deleted` | `deleteScans` (motion_connector.py:1703) | `session_ids`, `count` |
| `audit_log_viewed` | LogsModal opened (connector slot called from QML on open) | `entry_count` |
| `audit_log_exported` | `exportAuditLogCsv` (connector slot) | `dest`, `row_count` |

Notes:
- `device_stats` reads `get_hardware_id()` and `get_version()` off the
  console/sensor handle (SDK: `MotionConsole.get_hardware_id` :556,
  `MotionSensor.get_hardware_id` :451, `*.get_version`). Best-effort:
  missing values logged as empty strings.
- `settings_changed` only records keys whose value actually changed, so
  a no-op save produces no entry (or an entry with an empty diff —
  implementation may skip empty diffs entirely).
- `audit_log_viewed` / `audit_log_exported` make audit-data access
  itself auditable, per the requirement to log viewing.

## UI

### Settings — Logs button

In `components/SettingsModal.qml`, add an **"Audit Log"** `SectionCard`
containing a **Logs** `ActionButton`. The card is **not** gated on
`developerMode` (auditors are not developers). On click it opens a
`PasswordPromptModal` (reused; title "Audit Log", description "Enter the
password to view the audit log."); `onAccepted` opens the LogsModal.

### LogsModal

New `components/LogsModal.qml`, modeled on `HistoryModal.qml`:

- Standard modal shell (backdrop, title bar with ✕, ESC-to-close,
  click-outside-to-close) consistent with existing modals.
- Exposes `readonly property string label: "Audit Log"`, `visible`, and
  `function open()` / `function close()` so `ModalManager` governs it.
- A scrollable, newest-first table with columns **Time** (`ts_iso`),
  **Event** (`event_type`), **Details** (the JSON string, elided/wrapped).
  Rows come from `MotionInterface.auditLogEntries(limit)`.
- `open()` calls a connector slot to (a) record the `audit_log_viewed`
  event and (b) load entries.
- An **Export CSV** button using `QtQuick.Dialogs` `FileDialog`
  (default folder = `MotionInterface.directory`), wired to
  `MotionInterface.exportAuditLogCsv(destPath)`.

Register `logsModal` in `BloodFlow.qml`'s `ModalManager { modals: [...] }`
list.

### Connector slots (QML-facing)

Added to `MotionConnector`:

- `@pyqtSlot(int, result="QVariantList") auditLogEntries(limit=500)` —
  pure read: returns rows as `{id, ts_iso, ts_epoch, event_type,
  details}` dicts, newest first. Does **not** log (so re-querying/
  refreshing the table never double-logs).
- `@pyqtSlot() recordAuditLogViewed()` — records the single
  `audit_log_viewed` event. Called exactly once from `LogsModal.open()`,
  separately from `auditLogEntries()`.
- `@pyqtSlot(str, result=str) exportAuditLogCsv(dest_path)` — writes the
  full log to CSV (columns: `ts_iso, ts_epoch, event_type, details`),
  records `audit_log_exported`, returns the written path (or "" on
  failure). Accepts a `file://` URL or plain path (normalize like the
  existing `FolderDialog` handlers in SettingsModal).

## CSV export format

Header row, then one row per entry, ordered oldest→newest (stable for
diffing):

```
ts_iso,ts_epoch,event_type,details
2026-06-15T09:14:02,1750000442.12,system_startup,"{""app_version"":""1.2.3"",...}"
```

`details` is the raw JSON string, quoted per `csv` module rules. UTF-8,
`newline=""` (matches the existing FT-CSV writer at
motion_connector.py:2593).

## Testing

`tests/test_audit_log.py`, all `@pytest.mark.unit` (no app launch, no
hardware — conftest autouse fixtures short-circuit on `unit`):

- Table is created on first use; `IF NOT EXISTS` is idempotent on reopen.
- `log()` inserts a row with epoch + ISO timestamps and round-trips the
  JSON `details`.
- `log()` with `details=None` stores SQL `NULL`.
- `query(limit)` returns newest-first and honors the limit.
- `export_csv()` writes the expected header and quoting; round-trips
  through `csv.DictReader`.
- `AuditLog(None)` (no path) is a silent no-op: `log()`/`query()`/
  `export_csv()` don't raise, `query()` returns `[]`.
- Concurrent writes from two threads don't raise and both land (lock).

Optional connector-level unit test: constructing `MotionConnector` with
a tmp `scan_db_path` produces `system_startup` + `system_info` rows.

The existing `tests/test_developer_password.py` already covers the
password check being reused here.

## Out of scope / YAGNI

- No log rotation / retention policy (append-only; auditors keep the
  file). Revisit only if table size becomes a real problem.
- No in-app filtering/search UI beyond newest-first listing (CSV export
  covers downstream analysis).
- No SDK changes.
- No new config flags.
