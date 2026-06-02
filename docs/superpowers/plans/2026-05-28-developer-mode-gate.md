# Developer-Mode Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the bloodflow-app locked (`developerMode: false`) and gate all engineering features behind a hidden unlock — double-click the Openwater logo, enter a password — while moving Calibrate/Test into the Developer settings card and adding a console-fan switch there.

**Architecture:** `developerMode` is already a reactive config flag read across the QML via `MOTIONInterface.appConfig.developerMode`; flipping it through `setConfig` re-evaluates every gated `visible:` binding with no restart. We add: (1) a Python password check + QML unlock modal triggered by a logo double-click that persists `developerMode=true`; (2) a console-fan connector slot/property surfaced as a switch in the Developer card; (3) relocation of the always-visible Calibration card's contents into the `developerMode`-gated Developer card, deleting the standalone card.

**Tech Stack:** Python 3.13 + PyQt6 (connector slots/properties), QML 6 (UI), pytest HIL suite (UIA-tree coordinate tests).

**Worktree:** `C:\Users\ethan\Projects\openmotion-bloodflow-app\.claude\worktrees\developer-mode-gate` on branch `feature/developer-mode-gate`. **All paths below are relative to that worktree.** Run all `git` commands from there.

**Spec:** `docs/superpowers/specs/2026-05-28-developer-mode-gate-design.md`

> **Password value:** This plan uses the constant `"OnePointOne"`. Change `_DEVELOPER_PASSWORD` in Task 1 if a different value is wanted — it is the only place it is defined.

---

## Task 1: Developer-password check (Python, TDD)

**Files:**
- Modify: `motion_connector.py` (add module-level constant + helper near the top of the file, after the existing imports/module constants; add a `@pyqtSlot` on `MOTIONConnector`)
- Test: `tests/test_developer_password.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_developer_password.py`:

```python
"""Pure-software unit test for the developer-mode password check.

Does NOT request the ``app`` fixture, so the bloodflow app is not
launched and no hardware is touched — only the lightweight
session-autouse QCoreApplication fixture runs.
"""
from motion_connector import developer_password_matches, _DEVELOPER_PASSWORD


def test_correct_password_matches():
    assert developer_password_matches(_DEVELOPER_PASSWORD) is True


def test_wrong_password_rejected():
    assert developer_password_matches("nope") is False


def test_empty_password_rejected():
    assert developer_password_matches("") is False


def test_none_password_rejected():
    assert developer_password_matches(None) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_developer_password.py -p no:cacheprovider -v`
Expected: FAIL at import — `ImportError: cannot import name 'developer_password_matches' from 'motion_connector'`.

- [ ] **Step 3: Write minimal implementation**

In `motion_connector.py`, add near the other module-level constants (top of file, after imports). Find a sensible spot among existing module-level definitions:

```python
# ── Developer-mode unlock ────────────────────────────────────────────────
# Hardcoded developer-mode password. Double-clicking the Openwater logo
# opens a prompt; entering this value sets developerMode=true (persisted).
# This is the ONLY place the literal is defined. The check lives in Python
# (not QML) so the literal never ships inside readable QML text.
_DEVELOPER_PASSWORD = "OnePointOne"


def developer_password_matches(pw) -> bool:
    """Return True iff ``pw`` equals the developer-mode password."""
    return isinstance(pw, str) and pw == _DEVELOPER_PASSWORD
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_developer_password.py -p no:cacheprovider -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Add the QML-facing slot**

In `motion_connector.py`, add a slot on `MOTIONConnector` next to `setConfig` (~line 1701). Use the module helper so there is one source of truth:

```python
    @pyqtSlot(str, result=bool)
    def checkDeveloperPassword(self, pw: str) -> bool:
        """Return True if ``pw`` matches the developer-mode password.

        Comparison lives in Python so the literal is not present in
        shipped QML source. QML calls this from the unlock modal and,
        on True, sets developerMode via setConfig.
        """
        ok = developer_password_matches(pw)
        if not ok:
            logger.info("[Connector] Developer unlock attempt failed")
        return ok
```

- [ ] **Step 6: Commit**

```bash
git add motion_connector.py tests/test_developer_password.py
git commit -m "feat(bloodflow-app): developer-mode password check (Python)"
```

---

## Task 2: Console-fan connector slot + property (Python)

**Files:**
- Modify: `motion_connector.py` — `__init__` (~line 670), connect-time fan set (~line 1247), new signal + property + slot (near `consoleConnected` ~line 1097 and `setFanControl` ~line 3148)

There is no clean headless unit test for this (it drives `self._interface.console`); it is verified manually in Task 8 and exercised by hardware. Keep the slot defensive like the sibling console slots.

- [ ] **Step 1: Seed the cache in `__init__`**

In `motion_connector.py`, after `self._last_fan_status = {...}` (line 670), add:

```python
        # Console fan on/off cache. Seeded True because the connector
        # forces set_fan_speed(100) at console-connect time (see the
        # connect handler below). Surfaced to QML as ``consoleFanOn`` and
        # toggled via ``setConsoleFan``.
        self._console_fan_on: bool = True
```

- [ ] **Step 2: Add the notify signal**

Find the block of `pyqtSignal()` declarations (near `appConfigChanged = pyqtSignal()` ~line 570) and add:

```python
    consoleFanChanged = pyqtSignal()
```

- [ ] **Step 3: Keep the cache truthful at connect time**

In the connect handler, the existing block (~line 1247) reads:

```python
                    if self._interface.console.set_fan_speed(fan_speed=100):
                        logger.info("Console fan speed set to 100%")
                    else:
                        logger.error("Failed to set console fan speed")
```

Replace it with a version that updates the cache + notifies on success:

```python
                    if self._interface.console.set_fan_speed(fan_speed=100):
                        logger.info("Console fan speed set to 100%")
                        self._console_fan_on = True
                        self.consoleFanChanged.emit()
                    else:
                        logger.error("Failed to set console fan speed")
```

- [ ] **Step 4: Add the property + slot**

Add near the other console members (e.g. just after the `consoleConnected` property ~line 1099):

```python
    @pyqtProperty(bool, notify=consoleFanChanged)
    def consoleFanOn(self) -> bool:
        """Cached last-set console-fan state (True=on). Reflects the
        connect-time 100% default and any setConsoleFan call. Read by the
        Developer settings switch when the Settings modal opens."""
        return self._console_fan_on

    @pyqtSlot(bool, result=bool)
    def setConsoleFan(self, on: bool) -> bool:
        """Drive the console fan to 100% (on) or 0% (off).

        Updates the cached state + notifies QML on success. Guarded and
        wrapped like the other console slots so a mid-flight disconnect
        can't raise out of the Qt slot and kill the process.
        """
        if not self._consoleConnected:
            logger.error("Console not connected — cannot set console fan")
            return False
        try:
            speed = 100 if on else 0
            if self._interface.console.set_fan_speed(fan_speed=speed):
                self._console_fan_on = bool(on)
                self.consoleFanChanged.emit()
                logger.info("Console fan set to %s", "ON" if on else "OFF")
                return True
            logger.error("Failed to set console fan")
            return False
        except Exception as e:  # noqa: BLE001 — slot must not raise
            logger.error("Error setting console fan: %s", e)
            return False
```

> Note: `pyqtProperty` and `pyqtSlot` are already imported in this file (used throughout). No new imports needed.

- [ ] **Step 5: Smoke-import to catch syntax/typo errors**

Run: `python -c "import motion_connector; print('ok')"`
Expected: prints `ok` (no traceback).

- [ ] **Step 6: Commit**

```bash
git add motion_connector.py
git commit -m "feat(bloodflow-app): console-fan slot + cached consoleFanOn property"
```

---

## Task 3: Default `developerMode` to false

**Files:**
- Modify: `config/app_config.json:89`

- [ ] **Step 1: Flip the default**

In `config/app_config.json`, change line 89 from:

```json
  "developerMode": true,
```

to:

```json
  "developerMode": false,
```

- [ ] **Step 2: Verify it is valid JSON**

Run: `python -c "import json; json.load(open('config/app_config.json', encoding='utf-8')); print('ok')"`
Expected: prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add config/app_config.json
git commit -m "chore(bloodflow-app): ship with developerMode disabled by default"
```

---

## Task 4: Logo double-click signal (QML)

**Files:**
- Modify: `components/WindowMenu.qml` — add signal (~line 16) and a `MouseArea` inside the logo `Rectangle` (~lines 57–114)

- [ ] **Step 1: Declare the signal**

In `components/WindowMenu.qml`, next to the existing `signal closeRequested()` (~line 16), add:

```qml
    // Emitted on double-click of the logo. main.qml owns the behavior
    // (opens the developer-unlock prompt) — this component stays dumb.
    signal logoDoubleClicked()
```

- [ ] **Step 2: Add the MouseArea over the logo**

In the logo `Rectangle` (the one with `width: 185; height: 42`, ~line 57), add a `MouseArea` as the LAST child (after the dark/light Image/Item blocks, before the `Rectangle`'s closing brace at ~line 114) so it sits on top:

```qml
            // Double-click → developer-mode unlock prompt (issue: dev gate).
            // Sits above the header drag MouseArea so only the logo area
            // captures the double-click; the rest of the bar still drags.
            MouseArea {
                anchors.fill: parent
                acceptedButtons: Qt.LeftButton
                onDoubleClicked: windowMenu.logoDoubleClicked()
            }
```

- [ ] **Step 3: Verify (deferred to Task 8 launch)**

QML has no standalone unit test here; correctness is confirmed in the Task 8 manual launch (double-click opens the modal; bar still drags elsewhere).

- [ ] **Step 4: Commit**

```bash
git add components/WindowMenu.qml
git commit -m "feat(bloodflow-app): emit logoDoubleClicked from WindowMenu logo"
```

---

## Task 5: Developer-unlock modal (QML)

**Files:**
- Create: `components/DeveloperUnlockModal.qml`

- [ ] **Step 1: Create the modal**

Create `components/DeveloperUnlockModal.qml`:

```qml
import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

// Small password prompt for unlocking developer mode. Opened from
// main.qml on a logo double-click. On the correct password it persists
// developerMode=true via setConfig and emits unlocked().
Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 10000

    AppTheme { id: theme }

    signal unlocked()

    function open() {
        pwField.text = ""
        errorLabel.visible = false
        root.visible = true
        pwField.forceActiveFocus()
    }
    function close() {
        root.visible = false
    }

    function _submit() {
        if (MOTIONInterface.checkDeveloperPassword(pwField.text)) {
            MOTIONInterface.setConfig("developerMode", true)
            MOTIONInterface.notify("Developer mode enabled.", "info", 3000, false, "dev-mode")
            root.unlocked()
            root.close()
        } else {
            errorLabel.visible = true
            pwField.text = ""
            pwField.forceActiveFocus()
        }
    }

    // Backdrop — click outside closes.
    Rectangle {
        anchors.fill: parent
        color: "#000000B0"
        MouseArea { anchors.fill: parent; onClicked: root.close() }
    }

    // Panel
    Rectangle {
        width: 360
        height: contentCol.implicitHeight + 48
        radius: 14
        color: theme.bgContainer
        border.color: theme.borderStrong
        border.width: 1
        anchors.centerIn: parent

        // Absorb clicks so they don't reach the backdrop.
        MouseArea { anchors.fill: parent }

        ColumnLayout {
            id: contentCol
            anchors.fill: parent
            anchors.margins: 24
            spacing: 16

            Text {
                text: "Developer Access"
                color: theme.textPrimary
                font.pixelSize: 18
                font.weight: Font.DemiBold
            }

            Text {
                text: "Enter the developer password to enable developer mode."
                color: theme.textSecondary
                font.pixelSize: 13
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            TextField {
                id: pwField
                Layout.fillWidth: true
                Layout.preferredHeight: 34
                echoMode: TextInput.Password
                placeholderText: "Password"
                color: theme.textPrimary
                font.pixelSize: 14
                background: Rectangle {
                    color: theme.bgInput
                    radius: 4
                    border.color: pwField.activeFocus ? theme.accentBlue : theme.borderSoft
                    border.width: 1
                }
                onAccepted: root._submit()
            }

            Text {
                id: errorLabel
                text: "Incorrect password"
                color: theme.accentRed
                font.pixelSize: 12
                visible: false
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 12
                Item { Layout.fillWidth: true }

                Button {
                    text: "Cancel"
                    Layout.preferredHeight: 32
                    onClicked: root.close()
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13
                        color: theme.textSecondary
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? theme.bgHover : theme.bgInput
                        radius: 4
                        border.color: theme.borderSoft; border.width: 1
                    }
                }

                Button {
                    text: "Unlock"
                    Layout.preferredHeight: 32
                    onClicked: root._submit()
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13
                        color: "#FFFFFF"
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? Qt.lighter(theme.accentBlue, 1.1) : theme.accentBlue
                        radius: 4
                    }
                }
            }
        }

        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape) { root.close(); event.accepted = true }
        }
    }
}
```

> **Verify before relying on these tokens:** `theme.accentRed`, `theme.accentBlue`, `theme.bgHover`, `theme.bgInput`, `theme.borderSoft`, `theme.borderStrong`, `theme.bgContainer` are all referenced elsewhere in `components/SettingsModal.qml` (e.g. `colAccent: theme.accentBlue`, `colBorderSoft: theme.borderSoft`). If any token name differs in `components/AppTheme.qml`, use the AppTheme name. `MOTIONInterface.notify(message, level, durationMs, persistent, tag)` signature matches the call in `main.qml:106`.

- [ ] **Step 2: Commit**

```bash
git add components/DeveloperUnlockModal.qml
git commit -m "feat(bloodflow-app): add DeveloperUnlockModal password prompt"
```

---

## Task 6: Wire the modal into main.qml

**Files:**
- Modify: `main.qml` — add `onLogoDoubleClicked` to `headerMenu` (~line 96 area) and instantiate the modal (near `TestResultsWindow` ~line 198)

- [ ] **Step 1: Handle the signal on the header**

In `main.qml`, inside the `WindowMenu { id: headerMenu ... }` block, after the `onCloseRequested: {...}` handler (closes at ~line 116), add:

```qml
            onLogoDoubleClicked: developerUnlockModal.open()
```

- [ ] **Step 2: Instantiate the modal**

The modal must be a child of the top-level fill `Rectangle` so its `anchors.fill: parent` covers the whole window and its `z: 10000` sits above content. Add it as the last child of the main `Rectangle { anchors.fill: parent; color: theme.bgBase ... }` block — i.e. just after the inner `Item { ... BloodFlow {} }` block (closes ~line 145), before that `Rectangle`'s closing brace:

```qml
        // Developer-mode unlock prompt (opened by logo double-click).
        DeveloperUnlockModal {
            id: developerUnlockModal
        }
```

- [ ] **Step 3: Verify (deferred to Task 8 launch)**

Confirmed in the Task 8 manual launch.

- [ ] **Step 4: Commit**

```bash
git add main.qml
git commit -m "feat(bloodflow-app): open DeveloperUnlockModal on logo double-click"
```

---

## Task 7: Move Calibration into Developer card; add fan switch + disable button; delete Calibration card

**Files:**
- Modify: `components/SettingsModal.qml` — Developer card (~lines 712–771) and Calibration card (~lines 773–909)

This is the largest edit. Do it as: (a) extend the Developer card, (b) delete the Calibration card. Keep all calibration element `id`s and status strings byte-identical so the HIL UIA contract holds.

- [ ] **Step 1: Add the console-fan switch to the Developer card**

In `components/SettingsModal.qml`, inside the `SectionCard { title: "Developer" ... }` (~line 712), after the existing "Console" `FieldRow` with the Soft Reset button (~line 725), add:

```qml
                    FieldRow {
                        label: "Console fans"
                        PillSwitch {
                            checked: MOTIONInterface.consoleFanOn
                            enabled: MOTIONInterface.consoleConnected
                            onToggled: MOTIONInterface.setConsoleFan(checked)
                        }
                        Text {
                            text: MOTIONInterface.consoleFanOn ? "On" : "Off"
                            color: MOTIONInterface.consoleFanOn ? root.colAccent : root.colTextMuted
                            font.pixelSize: 12
                        }
                        Item { Layout.fillWidth: true }
                    }
```

- [ ] **Step 2: Move the calibration controls into the Developer card**

Cut the **inner content** of the Calibration `SectionCard` — everything between `SectionCard { title: "Calibration"` and its matching closing brace (the `RowLayout` with Calibrate/Test/target/light/status, the `calibTimer` Timer, and BOTH `Connections` blocks; ~lines 777–908) — and paste it at the END of the Developer `SectionCard`, after the "Raw CSV duration" `FieldRow` (~line 770), inside the Developer card's closing brace. Prepend a small labeled separator so it reads as a sub-group:

```qml
                    // ── Calibration / Test (moved here from the former
                    //    standalone Calibration card; now developer-only) ──
                    Rectangle { Layout.fillWidth: true; height: 1; color: root.colBorderSoft }
                    Text {
                        text: "Calibration"
                        color: root.colTextPri
                        font.pixelSize: 13
                        font.weight: Font.DemiBold
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 12

                        ActionButton {
                            id: runCalibrationButton
                            text: "Calibrate"
                            Layout.preferredWidth: 110
                            Layout.preferredHeight: 40
                            enabled: MOTIONInterface.consoleConnected
                                  && !MOTIONInterface.calibrationRunning
                                  && !MOTIONInterface.testScanRunning
                            onClicked: MOTIONInterface.runCalibration(
                                calibrationTargetCombo.currentText.toLowerCase()
                            )
                        }

                        ActionButton {
                            id: runTestButton
                            text: "Test"
                            Layout.preferredWidth: 110
                            Layout.preferredHeight: 40
                            enabled: MOTIONInterface.consoleConnected
                                  && !MOTIONInterface.calibrationRunning
                                  && !MOTIONInterface.testScanRunning
                            onClicked: MOTIONInterface.runTestScan(
                                calibrationTargetCombo.currentText.toLowerCase()
                            )
                        }

                        StyledCombo {
                            id: calibrationTargetCombo
                            Layout.preferredWidth: 110
                            model: ["Both", "Left", "Right"]
                            currentIndex: 0
                            enabled: !MOTIONInterface.calibrationRunning
                                  && !MOTIONInterface.testScanRunning
                        }

                        Rectangle {
                            id: calibLight
                            width: 14
                            height: 14
                            radius: 7
                            border.width: 1
                            border.color: root.colBorderSoft
                            color: {
                                switch (MOTIONInterface.calibrationStatus) {
                                case "running": return "#2196F3"
                                case "passed":  return "#4CAF50"
                                case "failed":  return "#F44336"
                                case "aborted": return "#FF9800"
                                default:        return "#9E9E9E"
                                }
                            }
                        }

                        TextArea {
                            id: calibStatusLabel
                            Layout.fillWidth: true
                            Layout.preferredHeight: Math.max(40, implicitHeight)
                            readOnly: true
                            selectByMouse: false
                            activeFocusOnTab: false
                            background: null
                            padding: 0
                            wrapMode: TextEdit.Wrap
                            verticalAlignment: TextEdit.AlignVCenter
                            color: root.colTextPri
                            font.pixelSize: 13
                            text: {
                                switch (MOTIONInterface.calibrationStatus) {
                                case "running":
                                    return "Calibrating... (" + calibTimer.elapsedSec
                                           + "s / " + MOTIONInterface.maxCalibrationTimeSec + "s)"
                                case "passed":  return "Calibration Passed"
                                case "failed":
                                    var reason = MOTIONInterface.calibrationFailureReason
                                    return reason
                                        ? "Calibration Failed — " + reason
                                        : "Calibration Failed"
                                case "aborted": return "Calibration Aborted"
                                default:        return ""
                                }
                            }
                        }
                    }

                    Timer {
                        id: calibTimer
                        property int elapsedSec: 0
                        interval: 1000
                        repeat: true
                        running: MOTIONInterface.calibrationRunning
                        onTriggered: elapsedSec += 1
                    }

                    Connections {
                        target: MOTIONInterface
                        function onCalibrationStateChanged() {
                            if (MOTIONInterface.calibrationStatus === "running") {
                                calibTimer.elapsedSec = 0
                            }
                        }
                    }

                    Connections {
                        target: MOTIONInterface
                        function onTestScanStateChanged() {
                            var s = MOTIONInterface.testScanStatus
                            if (s === "running" || s === "done"
                                || s === "failed" || s === "aborted") {
                                testResultsWindow.show()
                                testResultsWindow.raise()
                                testResultsWindow.requestActivate()
                            }
                        }
                    }
```

- [ ] **Step 3: Add the "Disable developer mode" button at the end of the Developer card**

After the moved calibration block (still inside the Developer `SectionCard`), add:

```qml
                    Rectangle { Layout.fillWidth: true; height: 1; color: root.colBorderSoft }
                    FieldRow {
                        label: "Developer mode"
                        ActionButton {
                            text: "Disable developer mode"
                            Layout.preferredWidth: 200
                            hoverColor: "#C0392B"
                            onClicked: {
                                MOTIONInterface.setConfig("developerMode", false)
                                MOTIONInterface.notify("Developer mode disabled.", "info", 3000, false, "dev-mode")
                            }
                        }
                        Item { Layout.fillWidth: true }
                    }
```

- [ ] **Step 4: Delete the now-empty standalone Calibration card**

Remove the entire `SectionCard { title: "Calibration" ... }` block (its opening line through its matching closing brace). After Step 2 it contains only the header/divider scaffolding from `SectionCard`; delete the whole `SectionCard {...}`. Confirm no other `SectionCard` was disturbed (the next one is `title: "About"`).

- [ ] **Step 5: Brace/region sanity check**

Run a quick structural check that braces still balance and the markers exist:

```bash
python - <<'PY'
src = open('components/SettingsModal.qml', encoding='utf-8').read()
assert src.count('{') == src.count('}'), (src.count('{'), src.count('}'))
assert 'title: "Calibration"' not in src, "standalone Calibration card still present"
assert 'id: runCalibrationButton' in src and 'id: calibStatusLabel' in src
assert 'Console fans' in src and 'Disable developer mode' in src
print('ok')
PY
```
Expected: prints `ok`.

- [ ] **Step 6: Commit**

```bash
git add components/SettingsModal.qml
git commit -m "feat(bloodflow-app): move calibration into developer card; add console-fan switch and dev-mode disable"
```

---

## Task 8: Manual verification (fake-data, no hardware)

**Files:** none (verification only)

- [ ] **Step 1: Launch the app locked**

Ensure `config/app_config.json` has `cameraFakeData: true` and `developerMode: false` (temporarily set `cameraFakeData: true` for this check; revert before commit if you changed it).
Run: `python main.py`

- [ ] **Step 2: Confirm locked state**

Verify: no "Log" button in the side panel; open Settings → no "Developer" card, no "Calibration" card.

- [ ] **Step 3: Unlock**

Double-click the Openwater logo (top-left). The password modal appears. Enter a wrong password → "Incorrect password", no change. Enter `OnePointOne` → toast "Developer mode enabled."; modal closes.

- [ ] **Step 4: Confirm unlocked state**

Open Settings → the "Developer" card is present and contains: Soft Reset, Console fans switch, Save raw CSV, Raw CSV duration, the Calibrate/Test/target/status group, and "Disable developer mode". No separate "Calibration" card exists.

- [ ] **Step 5: Console-fan switch reflects state on open**

With a console connected the switch shows On (cached 100% default). Without hardware, confirm the switch renders and is disabled when `consoleConnected` is false. Toggling with hardware drives the fan and the On/Off text follows.

- [ ] **Step 6: Persistence + disable**

Restart the app → developer mode is still on (persisted). Open Settings → "Disable developer mode" → Developer card disappears; toast shown. Restart → locked again.

- [ ] **Step 7: Revert any temporary config**

If `cameraFakeData`/`developerMode` were changed for testing, restore `developerMode: false` (and `cameraFakeData` to its committed value). `git diff config/app_config.json` should show ONLY the `developerMode` default flip from Task 3.

---

## Task 9: Update calibration HIL tests for the dev gate

**Files:**
- Modify: `tests/test_calibration_ui.py`
- Modify: `tests/test_calibration_target_isolation.py`

Both tests drive calibration but assume `developerMode` defaults true and locate the controls under a "Calibration" heading. With the new default they must force `developerMode=True` before launch and locate the controls under "Developer".

- [ ] **Step 1: Inspect each test's current config-forcing + locator**

Read both files. Identify: (a) whether they already import from `hil_helpers`; (b) any module-level `force_app_config_value(...)` calls + restore fixtures; (c) where they scroll/locate the calibration section (look for the string `"Calibration"` used as a scroll target, e.g. a `_scroll_until_label_visible("Calibration")` or coordinate anchor).

- [ ] **Step 2: Force developerMode at module-import time**

In each file, near the top (module scope, after imports), mirror the `test_raw_csv_save.py` pattern:

```python
from hil_helpers import force_app_config_value, write_app_config_value

_INITIAL_DEVELOPER_MODE = force_app_config_value("developerMode", True)


@pytest.fixture(scope="module", autouse=True)
def _restore_developer_mode():
    yield
    write_app_config_value("developerMode", _INITIAL_DEVELOPER_MODE)
```

If the file already imports some of these helpers, extend the existing import rather than duplicating it.

- [ ] **Step 3: Re-point the section locator**

Wherever the test scrolls to or anchors on the "Calibration" section heading to reach the Calibrate/Test buttons, change the target to the "Developer" section heading (the controls now live under "Developer"). Button labels ("Calibrate", "Test") and status strings ("Calibration Passed", etc.) are unchanged — leave those locators as-is. If the test already scrolls to a button label (not the section title), no locator change is needed.

- [ ] **Step 4: Verify collection (no hardware)**

Run: `python -m pytest tests/test_calibration_ui.py tests/test_calibration_target_isolation.py --collect-only -q`
Expected: tests collect with no import/syntax errors. (Execution needs hardware + the `app` fixture; run on the self-hosted runner or locally with hardware attached.)

- [ ] **Step 5: Commit**

```bash
git add tests/test_calibration_ui.py tests/test_calibration_target_isolation.py
git commit -m "test(bloodflow-app): force developerMode for calibration HIL tests under dev gate"
```

---

## Task 10: Final review + branch finish

- [ ] **Step 1: Full diff review**

Run: `git -C . diff next --stat` and skim each file. Confirm: connector slots/property/signal added; `app_config.json` only flips `developerMode`; WindowMenu signal + MouseArea; new DeveloperUnlockModal; main.qml wiring; SettingsModal restructure; two HIL tests updated; spec + plan docs present.

- [ ] **Step 2: Re-run the pure-software unit test**

Run: `python -m pytest tests/test_developer_password.py -p no:cacheprovider -v`
Expected: 4 passed.

- [ ] **Step 3: Hand off via finishing-a-development-branch**

Use `superpowers:finishing-a-development-branch` to decide merge/PR. Target PR base is `next` (per repo branching: feature → `next`).
