# Developer-Mode Gate — Design

**Date:** 2026-05-28
**Target release:** bloodflow-app 1.1.3
**Branch:** `feature/developer-mode-gate` (based on `next`)

## Summary

Hide developer/engineering features behind a deliberate unlock. Today
`developerMode` defaults to `true` in `config/app_config.json`, so every
build ships with calibration, test, log-viewer, soft-reset, and raw-CSV
controls visible. This change ships **locked** (`developerMode: false`) and
adds a hidden entry point: double-clicking the Openwater logo opens a
password prompt; the correct password turns developer mode on (persisted).

Three user-facing changes:

1. **Logo double-click → password → developer mode.**
2. **Console-fan on/off switch** added to the Developer settings card, with
   its state shown when the Settings window opens.
3. **Calibrate / Test controls move into the Developer card**, and the
   standalone "Calibration" card is removed — so calibration is developer-only.

## Background (current state)

- `developerMode` is read reactively across the QML via
  `MOTIONInterface.appConfig.developerMode`. Flipping it through
  `setConfig("developerMode", value)` emits `appConfigChanged`, which
  re-evaluates every `visible:` binding — no restart needed to reveal/hide UI.
- The logo is rendered in `components/WindowMenu.qml` (the `Rectangle` at
  ~lines 57–114); `main.qml:77` passes `logoSource`. There is no click
  handler today. A full-width drag `MouseArea` sits behind the header.
- All calibration UI lives in `components/SettingsModal.qml`:
  - an **always-visible** "Calibration" `SectionCard` (~lines 773–909):
    Calibrate + Test buttons, a Both/Left/Right target `StyledCombo`, a status
    indicator light, a status `TextArea`, a 1 Hz `calibTimer`, and two
    `Connections` blocks (one resets the timer on state change; one raises
    `TestResultsWindow` on test-scan state change).
  - a **`developerMode`-gated** "Developer" `SectionCard` (~lines 712–771):
    console Soft Reset, Save-raw-CSV switch, raw-CSV duration.
- Console fan: the SDK exposes `console.set_fan_speed(0..100)` (duty cycle)
  and `console.get_fan_rpm(1..3)` (tach RPM). On console connect the connector
  forces `set_fan_speed(fan_speed=100)` (`motion_connector.py:1247`). There is
  **no** console-fan connector slot yet; the existing `setFanControl` /
  `getFanControlStatus` slots are for *sensor* fans, not the console.

## Decisions (confirmed with user)

- **Password storage:** hardcoded constant, compared in Python (not exposed
  to QML source).
- **Persistence:** unlocking persists via `setConfig("developerMode", true)`
  (survives restart) — matching how the flag already behaves.
- **Default:** `developerMode` defaults to `false` in `app_config.json`.
- **Fan semantics:** on → `set_fan_speed(100)`, off → `set_fan_speed(0)`.
  State is read from a connector-cached last-set value (no hardware
  round-trip), seeded to reflect the connect-time 100% default.
- **Exit path:** a "Disable developer mode" button in the Developer card
  (needed because the flag now persists).

## Feature 1 — Logo double-click → password → developer mode

### Components

- **`components/WindowMenu.qml`** — add `signal logoDoubleClicked()` and a
  `MouseArea` over just the logo `Rectangle` with `onDoubleClicked:
  windowMenu.logoDoubleClicked()`. The logo `MouseArea` sits above the
  header-drag `MouseArea`, so dragging the rest of the bar is unaffected.
  Keep the component dumb — it only emits; `main.qml` owns behavior.

- **`components/DeveloperUnlockModal.qml`** (new) — a small modal styled like
  the existing modals (backdrop + centered panel, Escape-to-close,
  click-outside-to-close). Contents: a title, a masked password `TextField`
  (`echoMode: TextInput.Password`), an inline error label, and
  Unlock / Cancel buttons. Exposes `open()` / `close()` and a
  `signal unlocked()`. Pressing Enter in the field submits.
  - On submit → `if (MOTIONInterface.checkDeveloperPassword(field.text))`:
    success → `MOTIONInterface.setConfig("developerMode", true)`, emit
    `unlocked()`, close, and show a confirmation toast via
    `MOTIONInterface.notify(...)`. Failure → show inline "Incorrect password",
    clear the field, leave state unchanged.

- **`main.qml`** — instantiate `DeveloperUnlockModal` and wire
  `headerMenu.onLogoDoubleClicked: developerUnlockModal.open()`. If already in
  developer mode, the prompt still opens (harmless); no special-casing.

- **`motion_connector.py`** — add
  `@pyqtSlot(str, result=bool) def checkDeveloperPassword(self, pw)` that
  compares against a module-level hardcoded constant
  (e.g. `_DEVELOPER_PASSWORD`). Comparison in Python keeps the literal out of
  shipped QML text. (Constant equality compare is sufficient; no hashing was
  requested.)

### Exit mechanism

- In the Developer `SectionCard` (`SettingsModal.qml`), add a "Disable
  developer mode" `ActionButton` → `MOTIONInterface.setConfig("developerMode",
  false)` plus a toast. Because the Developer card is itself gated by
  `developerMode`, the whole card (including this button) disappears the
  instant it's pressed — acceptable; the Settings modal stays open.

## Feature 2 — Console-fan switch in Developer settings

### Connector (`motion_connector.py`)

- Add `self._console_fan_on = True` in `__init__` (seeded `True` to match the
  connect-time `set_fan_speed(100)` default).
- At the connect-time fan set (`~line 1247`), set
  `self._console_fan_on = True` on success so the cache tracks reality.
- Add a notify signal `consoleFanChanged = pyqtSignal()`.
- Add `@pyqtProperty(bool, notify=consoleFanChanged) def consoleFanOn(self)`
  returning `self._console_fan_on`.
- Add `@pyqtSlot(bool, result=bool) def setConsoleFan(self, on)`:
  guard on `self._consoleConnected`; call
  `self._interface.console.set_fan_speed(100 if on else 0)`; on success update
  `self._console_fan_on` and emit `consoleFanChanged`; return success.
  Wrap in try/except (consistent with other console slots) so a mid-flight
  disconnect can't crash the Qt slot.

### UI (`SettingsModal.qml`, Developer card)

- A `FieldRow { label: "Console fans" }` containing a `PillSwitch`:
  `checked: MOTIONInterface.consoleFanOn`,
  `enabled: MOTIONInterface.consoleConnected`,
  `onToggled: MOTIONInterface.setConsoleFan(checked)`, plus an On/Off text.
- **State read on open:** the switch binds to `consoleFanOn`; `open()` already
  re-runs each time the modal opens, so the displayed state reflects the
  cached value whenever Settings opens. No extra wiring needed.

## Feature 3 — Move Calibrate/Test into Developer card; remove Calibration card

- Move the **entire** body of the "Calibration" `SectionCard` into the
  "Developer" `SectionCard`: the Calibrate + Test buttons, the
  Both/Left/Right target `StyledCombo`, the status indicator light, the status
  `TextArea`, the `calibTimer`, and **both** `Connections` blocks
  (calibration-state timer reset and test-scan-state `TestResultsWindow`
  raise). All element `id`s and the status strings ("Calibrating… (Ns/Ns)",
  "Calibration Passed", "Calibration Failed — …", "Calibration Aborted")
  are preserved verbatim so the UIA-tree contract used by HIL tests is intact.
- Delete the standalone "Calibration" `SectionCard`.
- Net effect: Calibrate/Test/target/status appear only when
  `developerMode === true`.

## Test impact (must update in this change)

`test_calibration_ui.py` and `test_calibration_target_isolation.py` drive the
calibration UI but **do not** force `developerMode` — they rely on the old
default of `true`. With the new default `false` and calibration gated, they
will fail (the Developer/Calibration section won't render). Both must:

- Force `developerMode = True` before launch at module-import time, using the
  established `hil_helpers.force_app_config_value("developerMode", True)`
  pattern, with a module-scoped fixture restoring the prior value (mirror
  `test_raw_csv_save.py`'s approach).
- Re-point any "scroll until the Calibration card title is visible" locator to
  the **Developer** card (the calibration controls now live under the
  "Developer" heading, not a "Calibration" heading). Button labels
  ("Calibrate", "Test") and status strings are unchanged, so label-based
  locators for those keep working.

`test_raw_csv_save.py` already forces `developerMode=True`, so it is
unaffected by the default flip. `test_reducedmode.py` references
`developerMode` only in a comment.

## Out of scope

- No hashing / per-deployment password (hardcoded constant per decision).
- No change to sensor-fan controls (`setFanControl`/`getFanControlStatus`).
- No change to reduced-mode behavior beyond what the dev gate implies.
- No new release tag — versioning is tag-driven; 1.1.3 is just the target.

## Verification plan

- **Static:** app launches with `cameraFakeData: true` and
  `developerMode: false` → no Calibrate/Test/Developer UI; double-click logo →
  password modal; correct password → Developer card (with calibration + fan
  switch) appears; "Disable developer mode" hides it again; value persists
  across a restart.
- **Tests:** update the two calibration HIL tests as above. Full HIL run
  requires hardware (run on the self-hosted runner / locally when hardware is
  attached). Pure-software portions and app launch verified locally in
  fake-data mode.
- QML has no hot-reload — restart the app to pick up `.qml` changes.
