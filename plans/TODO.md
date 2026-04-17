# Scan/SDK-request code-quality TODO

Tracked follow-ups from the scan-start / SDK-request code review.
Severity: **minor** unless marked otherwise.

## Pipeline / runner

- [ ] **m1 — Move `triggerConfig` into app config.** Currently both `ScanRunner`
      instances read `bloodFlow.defaultTriggerConfig`, which is a local
      constant with hardcoded defaults. It should live under
      `MOTIONInterface.appConfig.triggerConfig` so ops can tune it without
      a rebuild.

- [ ] **m3 — Replace magic duration estimate.**
      `motion_connector.runContactQualityCheck` emits
      `contactQualityCheckStarted.emit(4)` with a hardcoded 4 s estimate.
      Compute it from the actual `duration_s` arg plus a small configure
      overhead, or expose a helper on the SDK.

## Connector (`motion_connector.py`)

- [ ] **m4 — Swallowed exception in `runContactQualityCheck`.** The
      `try/except: pass` around `self._scan_workflow.running` check will
      silently proceed if access raises. Replace with a narrower check
      (e.g. direct attribute access after `is not None` guard) or let the
      SDK-layer guard be the single source of truth.

- [ ] **m5 — Inconsistent error surfacing.** Scan errors flow through
      `scanDialog.appendLog`; contact-quality errors go through
      `console.log` via `messageOut` and only reach the modal when
      `ok == false AND err != ""`. Normalize on a single logging / UI
      surface per pipeline stage.

- [ ] **m6 — Duplicate guards.** `startCapture`, `runContactQualityCheck`,
      and `startConfigureCameraSensors` each check `_capture_running` /
      `_config_running` with slightly different error messages, duplicating
      what the SDK returns via `start_scan` / `start_configure_camera_sensors`.
      Pick one layer as the authoritative guard and drop the other, or
      extract a `_ensure_idle()` helper that emits a single canonical
      message.
