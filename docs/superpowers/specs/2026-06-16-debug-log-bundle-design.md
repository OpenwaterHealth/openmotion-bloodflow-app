# Debug Log Bundle — Design

**Date:** 2026-06-16
**Status:** Approved (design); pending spec review
**Branch:** `claude/dreamy-borg-0c01f8` (builds on the audit-log feature)

## Goal

Add a **"Send Debug Logs to Openwater"** button to the audit-log viewer
(`LogsModal`). It zips the last 48 hours of app logs (plus minimal
diagnostic context), reveals the zip in the file explorer, and tells the
user to email it to **support@openwater.cc**. No mail client is launched
and nothing is auto-sent (a `mailto:` link cannot attach a file, and
true one-click send would require new SMTP/HTTP infrastructure that is
out of scope).

## Decisions (locked)

- **Behavior:** zip → reveal in file explorer → toast with the support
  address. No mail-client launch, no auto-send.
- **Bundle contents:** `app-logs/*.log` modified in the last 48h, plus
  `app_config.json` and a generated `system_info.txt`. No `scans.db`,
  no scan CSVs, no patient/subject data.
- **Recipient shown in the toast:** `support@openwater.cc`.

## Architecture

A new standalone module **`debug_bundle.py`** with a pure function, plus
a thin connector slot and one QML button.

- `build_debug_bundle(data_dir, dest_dir, now_epoch, window_hours=48)
  -> dict` — walks `<data_dir>/app-logs/*.log`, keeps files whose mtime
  is `>= now_epoch - window_hours*3600`, adds `<data_dir>/app_config.json`
  (if present) and a generated `system_info.txt`, writes a zip into
  `dest_dir`, and returns metadata
  `{"path", "file_count", "log_count", "bytes"}`. `now_epoch` is a
  parameter so tests are deterministic (the connector passes
  `time.time()`).
- The connector slot `prepareDebugLogBundle()` calls the builder, reveals
  the file, shows a toast, and records an audit event.
- Rationale: file-gathering/zipping logic is unit-testable against a tmp
  directory with no app launch; the 4031-line `motion_connector.py`
  stays lean (one thin slot). Mirrors the `audit_log.py` precedent.

### `system_info.txt`

Reuses `audit_log.gather_host_info()` and adds app + SDK version. Plain
key/value text lines, e.g.:

```
app_version: 1.2.0-dev.4
sdk_version: 1.5.8
generated: 2026-06-16T10:30:00-07:00
hostname: LAB-PC-01
platform: Windows-11-10.0.26200-SP0
...
```

The builder owns host probing (`gather_host_info()`) and the `generated`
timestamp (derived from `now_epoch`). The connector supplies the app/SDK
versions via `extra_info` (it can't probe them from the pure module).
The builder merges `extra_info` over the host info and writes the
combined key/value lines to `system_info.txt`.

## Module API (`debug_bundle.py`)

```python
WINDOW_HOURS = 48

def build_debug_bundle(
    data_dir: str | Path,
    dest_dir: str | Path,
    now_epoch: float,
    *,
    window_hours: int = WINDOW_HOURS,
    config_path: str | Path | None = None,
    extra_info: dict | None = None,
) -> dict:
    """Zip recent app logs + config + system info into dest_dir.

    Returns {"path": str, "file_count": int, "log_count": int,
             "bytes": int}. Creates dest_dir if needed. The zip name is
    debug-bundle-<YYYYMMDD_HHMMSS>.zip derived from now_epoch (local).
    """
```

- `config_path`: path to `app_config.json`. Defaults to
  `<data_dir>/app_config.json` when `None`. The connector passes the
  real location (`resource_path("config", "app_config.json")`), since
  the config does **not** live under the data directory. Included at the
  zip root if the path exists; silently skipped if not.
- `extra_info`: version info (`app_version`, `sdk_version`) merged into
  `system_info.txt`. Not a path carrier.

Behavior details:
- Source logs: `<data_dir>/app-logs/*.log` with
  `mtime >= now_epoch - window_hours*3600`. Stored in the zip under
  `app-logs/<filename>`.
- `app_config.json`: include the file at `config_path` (default
  `<data_dir>/app_config.json`) at the zip root, if it exists.
- `system_info.txt`: written at the zip root from `gather_host_info()`
  merged with `extra_info` (app_version, sdk_version) and a `generated`
  local-ISO-8601 timestamp from `now_epoch`.
- Output filename: `debug-bundle-<YYYYMMDD_HHMMSS>.zip` (timestamp from
  `now_epoch`, local time).
- Returns metadata; never partially writes — builds into the named file
  and closes it. Best-effort per-file: a log that can't be read is
  skipped (logged), not fatal.

## Connector slot (`motion_connector.py`)

```python
@pyqtSlot(result=str)
def prepareDebugLogBundle(self) -> str:
    """Zip the last 48h of app logs (+ config + system info) into
    app-logs/debug-bundles/, reveal it in the file explorer, and toast
    the support address. Returns the zip path, or '' on failure."""
```

- `dest_dir = <self._directory>/app-logs/debug-bundles`.
- `extra_info = {"app_version": get_version(), "sdk_version":
  self._interface.get_sdk_version()}` (each guarded; "" on failure),
  and `config_path = resource_path("config", "app_config.json")`.
- On success: reveal the file (Windows: `subprocess.run(["explorer",
  "/select,", path])`; other OS: `QDesktopServices.openUrl` on the
  folder — best-effort, wrapped in try/except), then
  `self.notify("Debug logs saved to <path>. Please email this file to
  support@openwater.cc.", "success", 0, True, "debug-bundle")` (sticky
  so the path stays readable), and
  `self._audit.log("debug_bundle_created", {...})`.
- On failure: log the exception, `self.errorOccurred.emit(...)` and/or a
  warning toast, return "".

## UI (`components/LogsModal.qml`)

Add a button **"Send Debug Logs"** in the toolbar, before "Export CSV"
(same button styling as the existing toolbar buttons). `onClicked:
MotionInterface.prepareDebugLogBundle()`. The connector handles the
reveal + toast, so the button has no further logic. A short helper text
or tooltip is not required (the toast explains what happened).

## Audit integration

New event **`debug_bundle_created`**, `details:
{"dest", "file_count", "log_count", "bytes", "window_hours"}`. Added as
an event-type constant in `audit_log.py` (`EV_DEBUG_BUNDLE_CREATED`) for
consistency; it appears in the same Logs viewer.

## Error handling

Fail-soft throughout. Zipping failure → error toast, return "", no
crash. Reveal failure → still toast the path. Missing `app_config.json`
or zero recent logs → still produce a valid zip (with whatever exists +
`system_info.txt`); the metadata reflects the actual counts.

## Testing

`tests/test_debug_bundle.py` (`@pytest.mark.unit`):
- Create a tmp `data_dir/app-logs` with three `*.log` files; backdate one
  past 48h via `os.utime`. Add a tmp `app_config.json`. Call
  `build_debug_bundle(data_dir, dest, now_epoch=<fixed>)`.
- Assert the resulting zip (via `zipfile.ZipFile.namelist()`) contains
  the two recent logs under `app-logs/`, `app_config.json`, and
  `system_info.txt`; excludes the aged log.
- Assert returned metadata: `log_count == 2`, `file_count`,
  `bytes == os.path.getsize(path)`, `path` ends with the expected name.
- `system_info.txt` contains `app_version` / `sdk_version` keys from
  `extra_info`.
- Empty case: no recent logs → zip still contains `system_info.txt`
  (and config if present), `log_count == 0`, no exception.

Connector-level (`tests/test_audit_connector.py`, `@pytest.mark.unit`):
- `prepareDebugLogBundle()` returns a path that exists under
  `app-logs/debug-bundles/`, and a `debug_bundle_created` audit event is
  recorded. (Reveal/notify side effects are best-effort and not
  asserted; the slot must not raise when `explorer` is unavailable.)

## Out of scope / YAGNI

- No SMTP/HTTP auto-send; no mail-client launch.
- No retention/cleanup of old bundles (they're small; revisit only if
  this becomes a problem).
- No configurable window in the UI (fixed 48h; the constant is the one
  tuning point).
- No inclusion of `scans.db` or scan CSVs.
