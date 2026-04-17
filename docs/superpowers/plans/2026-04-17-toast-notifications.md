# Toast Notification System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a VS Code-style stackable toast notification system to the OpenMOTION bloodflow app, callable from both Python and QML, with the Notes-pane "Note saved." toast as the first consumer.

**Architecture:** A single `NotificationCenter.qml` component is mounted once in `main.qml`. It owns a `ListModel` of active notifications, exposes a `notify(request)` JS function, and listens to a new `MOTIONInterface.notificationRequested` signal so Python and QML callers share one path. Each toast is rendered by a delegate that handles its own slide+fade animation, auto-dismiss timer, and hover-to-pause behavior.

**Tech Stack:** Python 3.12 + PyQt6 (signals/slots), QML 6.0 (Qt Quick Controls + Layouts), existing Keenicons outline font for icons, existing `AppTheme` for colors.

**Spec:** [`docs/superpowers/specs/2026-04-17-toast-notifications-design.md`](../specs/2026-04-17-toast-notifications-design.md)

---

## File Structure

**New files**

- `components/NotificationCenter.qml` — the center + toast delegate. ~180 lines. Owns the model, public `notify(request)` and `dismiss(id)` functions, the bottom-right anchored `Column`, the inline `Toast` delegate (visual layout, animation, timer), and the `Connections` block that listens to `MOTIONInterface.notificationRequested`.

**Modified files**

- `motion_connector.py` — add `notificationRequested` signal and `notify` `pyqtSlot` near the other UI-facing signals/slots (around line 91, near `errorOccurred`). ~10 lines added.
- `main.qml` — mount one `NotificationCenter` after the `UpdateBanner` block. ~5 lines added.
- `components/NotesModal.qml` — fire the success toast in `close()`. 1 line added.

**No test infrastructure exists in this repo for QML or the connector** (the `tests/` folder holds manual scripts and logs). To stay focused on the feature, this plan uses:

- A standalone Python smoke script for Task 1 (the `notify` slot can be exercised without Qt's event loop using `QSignalSpy`-equivalent manual collection).
- Manual visual verification for the QML tasks via a temporary keyboard-shortcut trigger in `main.qml` that we remove in the final task.

---

## Task 1: Python `notify` slot and `notificationRequested` signal

**Files:**
- Modify: `motion_connector.py` (add signal near line 91, add slot method elsewhere in the class)
- Create: `scripts/smoke_notify.py` (one-off smoke test, kept in repo)

- [ ] **Step 1: Write the failing smoke test**

Create `scripts/smoke_notify.py`:

```python
"""Smoke test for MOTIONConnector.notify slot.

Run from repo root:
    python scripts/smoke_notify.py

Exits 0 on success, non-zero on failure.
"""
import sys
from PyQt6.QtCore import QCoreApplication
from motion_connector import MOTIONConnector


def main() -> int:
    app = QCoreApplication(sys.argv)  # required for signals
    conn = MOTIONConnector()

    received = []
    conn.notificationRequested.connect(lambda payload: received.append(payload))

    # Call with full args
    conn.notify("Hello", "success", 5000, True)
    # Call with defaults (only required arg is text)
    conn.notify("Default")

    if len(received) != 2:
        print(f"FAIL: expected 2 emissions, got {len(received)}")
        return 1

    a, b = received
    if a != {"text": "Hello", "type": "success", "durationMs": 5000, "dismissible": True}:
        print(f"FAIL: first payload wrong: {a}")
        return 1
    if b != {"text": "Default", "type": "info", "durationMs": 4000, "dismissible": True}:
        print(f"FAIL: second payload wrong (defaults): {b}")
        return 1

    print("OK: notify slot emits notificationRequested with correct payload")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the smoke test to verify it fails**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
python scripts/smoke_notify.py
```

Expected: `AttributeError: 'MOTIONConnector' object has no attribute 'notificationRequested'` (or `notify`).

- [ ] **Step 3: Add the signal to `motion_connector.py`**

In `motion_connector.py`, find the line `errorOccurred = pyqtSignal(str)` (around line 91). Add a new signal right after it:

```python
    errorOccurred = pyqtSignal(str)
    notificationRequested = pyqtSignal('QVariant')  # toast notification payload dict
```

- [ ] **Step 4: Add the `notify` slot method**

Find the `scanNotes` setter (around line 1090) and add the `notify` method immediately after the closing of that setter (before `generate_user_label`):

```python
    @pyqtSlot(str)
    @pyqtSlot(str, str)
    @pyqtSlot(str, str, int)
    @pyqtSlot(str, str, int, bool)
    def notify(self, text: str, type_: str = "info", duration_ms: int = 4000, dismissible: bool = True):
        """Fire a toast notification. Reachable from QML as MOTIONInterface.notify(...)
        and from any Python code holding the connector instance.

        Args:
            text: message shown in the toast
            type_: one of "info", "success", "warning", "error"
            duration_ms: auto-dismiss after N ms; 0 = sticky until user dismisses
            dismissible: whether to show the ✕ close button
        """
        if type_ not in ("info", "success", "warning", "error"):
            logger.warning(f"notify: unknown type '{type_}', falling back to 'info'")
            type_ = "info"
        self.notificationRequested.emit({
            "text": text,
            "type": type_,
            "durationMs": int(duration_ms),
            "dismissible": bool(dismissible),
        })
```

The four stacked `@pyqtSlot` decorators register multiple arities so QML callers can omit trailing arguments (`MOTIONInterface.notify("Hi")` works, as does the four-arg form).

- [ ] **Step 5: Run the smoke test to verify it passes**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
python scripts/smoke_notify.py
```

Expected: `OK: notify slot emits notificationRequested with correct payload` and exit code 0.

- [ ] **Step 6: Commit**

```bash
git add motion_connector.py scripts/smoke_notify.py
git commit -m "feat(notify): add notificationRequested signal and notify slot

Adds the Python entry point for the toast notification system.
QML callers reach it as MOTIONInterface.notify(text, type, durationMs, dismissible).
Includes a standalone smoke test script (scripts/smoke_notify.py)."
```

---

## Task 2: NotificationCenter shell + main.qml mount + temporary debug trigger

This task creates the component skeleton (model, notify/dismiss, no visual delegate yet — just placeholder rectangles), mounts it in `main.qml`, and adds a temporary keyboard shortcut so subsequent visual tasks can be exercised. The shortcut is removed in Task 6.

**Files:**
- Create: `components/NotificationCenter.qml`
- Modify: `main.qml`

- [ ] **Step 1: Create `components/NotificationCenter.qml` with skeleton**

```qml
import QtQuick 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

/*  NotificationCenter — bottom-right toast stack.
 *
 *  Mount once in main.qml:
 *      NotificationCenter { id: notificationCenter; anchors.fill: parent; z: 99999 }
 *
 *  Fire a notification from QML:
 *      MOTIONInterface.notify("Note saved.", "success", 4000, true)
 *  Or from Python:
 *      self.notify("Scan complete.", "success")
 */
Item {
    id: root

    // Visible only via its toasts; root itself is transparent and doesn't
    // capture mouse events on empty space.

    AppTheme { id: theme }

    // Maximum number of toasts visible at once. When exceeded, the oldest
    // (model index 0) is removed.
    readonly property int maxVisible: 5

    // Monotonically increasing id assigned to each notification.
    property int _nextId: 1

    ListModel { id: model_ }

    /* ── public API ────────────────────────────────────────────────── */

    // Append a notification. `request` is a JS object with at least a `text`
    // field; other fields fall back to defaults defined here.
    function notify(request) {
        var entry = {
            id: root._nextId++,
            text: request.text || "",
            type: request.type || "info",
            durationMs: (request.durationMs !== undefined) ? request.durationMs : 4000,
            dismissible: (request.dismissible !== undefined) ? request.dismissible : true
        }
        model_.append(entry)
        while (model_.count > root.maxVisible) {
            model_.remove(0)
        }
    }

    // Remove a notification by id. Used by the toast delegate when its timer
    // fires or the user clicks ✕.
    function dismiss(id) {
        for (var i = 0; i < model_.count; ++i) {
            if (model_.get(i).id === id) {
                model_.remove(i)
                return
            }
        }
    }

    /* ── Python bridge ─────────────────────────────────────────────── */

    Connections {
        target: MOTIONInterface
        function onNotificationRequested(payload) {
            root.notify(payload)
        }
    }

    /* ── visual stack (placeholder delegate; replaced in Task 3) ──── */

    Column {
        id: stack
        spacing: 10
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.rightMargin: 24
        anchors.bottomMargin: 24

        Repeater {
            model: model_
            delegate: Rectangle {
                width: 340
                height: 48
                radius: 10
                color: theme.bgElevated
                border.color: theme.borderSubtle
                border.width: 1
                Text {
                    anchors.fill: parent
                    anchors.margins: 12
                    text: "[" + model.type + "] " + model.text
                    color: theme.textPrimary
                    elide: Text.ElideRight
                }
                // Temporary: click anywhere to dismiss while we're building.
                MouseArea {
                    anchors.fill: parent
                    onClicked: root.dismiss(model.id)
                }
            }
        }
    }
}
```

- [ ] **Step 2: Mount `NotificationCenter` in `main.qml` and add a temporary debug trigger**

Open `main.qml`. After the `UpdateBanner { ... }` block (currently lines 44-49), add the center inside the same root `Rectangle` so it overlays everything:

```qml
        // Update available banner (slides in below header)
        UpdateBanner {
            id: updateBanner
            anchors.top: headerMenu.bottom
            anchors.left: parent.left
            anchors.right: parent.right
        }

        // Toast notification overlay — fills the window, positions toasts in its own bottom-right corner
        NotificationCenter {
            id: notificationCenter
            anchors.fill: parent
            z: 99999
        }
```

Then, **temporarily**, add a debug Shortcut at the bottom of the `ApplicationWindow` (right before the closing `}` of `ApplicationWindow`, after the existing `Connections { target: MOTIONInterface }` block). This is removed in Task 6:

```qml
    // TEMP-NOTIF-DEBUG: remove in Task 6.
    // Press Ctrl+Shift+1..4 to fire one of each type.
    Shortcut {
        sequence: "Ctrl+Shift+1"
        onActivated: MOTIONInterface.notify("Info: this is an informational message.", "info", 4000, true)
    }
    Shortcut {
        sequence: "Ctrl+Shift+2"
        onActivated: MOTIONInterface.notify("Note saved.", "success", 4000, true)
    }
    Shortcut {
        sequence: "Ctrl+Shift+3"
        onActivated: MOTIONInterface.notify("Calibration drift exceeds threshold.", "warning", 4000, true)
    }
    Shortcut {
        sequence: "Ctrl+Shift+4"
        onActivated: MOTIONInterface.notify("Lost connection to console.", "error", 0, true)
    }
```

`Shortcut` is from `QtQuick.Controls 6.0`, which is already imported in `main.qml` (line 2).

- [ ] **Step 3: Run the app and verify the debug trigger works**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
python main.py
```

Press `Ctrl+Shift+2` four times. Expected: four placeholder rectangles appear stacked in the bottom-right, each reading `[success] Note saved.`. Press `Ctrl+Shift+1` a few more times — older entries are removed when the count exceeds 5 (newest at the bottom). Click any rectangle to dismiss it.

If nothing appears, check the console for QML errors (typically a missing import or a typo in `NotificationCenter`'s id mount).

- [ ] **Step 4: Commit**

```bash
git add components/NotificationCenter.qml main.qml
git commit -m "feat(notify): add NotificationCenter shell with placeholder delegate

Mounts the toast stack in main.qml, wires the Python signal to the QML
center via Connections, enforces the 5-toast cap. Includes a temporary
Ctrl+Shift+1..4 debug shortcut (removed in a later commit)."
```

---

## Task 3: Toast visual delegate (no animation yet)

Replace the placeholder `Rectangle` delegate with the real toast layout: left stripe, type icon, text, close button. The auto-dismiss timer and animations come in later tasks.

**Files:**
- Modify: `components/NotificationCenter.qml`

- [ ] **Step 1: Add a `FontLoader` for Keenicons inside `NotificationCenter`**

In `NotificationCenter.qml`, add the loader near the top of the root `Item` (right after the `AppTheme { id: theme }` line):

```qml
    AppTheme { id: theme }

    FontLoader {
        id: iconFont
        source: "../assets/fonts/keenicons-outline.ttf"
    }
```

- [ ] **Step 2: Add a helper that maps type → color and glyph**

Right after the `_nextId` property declaration, add two pure JS helpers:

```qml
    property int _nextId: 1

    function _accentColor(type) {
        switch (type) {
            case "success": return theme.accentGreen
            case "warning": return theme.accentYellow
            case "error":   return theme.accentRed
            case "info":
            default:        return theme.accentBlue
        }
    }

    function _glyph(type) {
        switch (type) {
            case "success": return "\ue99c"  // check-circle
            case "warning": return "\uea82"  // notification-bing
            case "error":   return "\ue9b2"  // cross-circle
            case "info":
            default:        return "\uea43"  // information-2
        }
    }
```

- [ ] **Step 3: Replace the placeholder delegate with the real toast**

Find the `Repeater { model: model_; delegate: Rectangle { ... } }` block from Task 2 and replace the entire `delegate:` body with this:

```qml
            delegate: Rectangle {
                id: toast
                width: 340
                implicitHeight: contentRow.implicitHeight + 24  // 12px padding top+bottom
                height: implicitHeight
                radius: 10
                color: theme.bgElevated
                border.color: theme.borderSubtle
                border.width: 1

                // Left accent stripe
                Rectangle {
                    width: 3
                    height: parent.height - 8
                    radius: 1.5
                    color: root._accentColor(model.type)
                    anchors.left: parent.left
                    anchors.leftMargin: 4
                    anchors.verticalCenter: parent.verticalCenter
                }

                RowLayout {
                    id: contentRow
                    anchors.fill: parent
                    anchors.leftMargin: 16     // leave room for stripe (4 + 3 + ~9)
                    anchors.rightMargin: 12
                    anchors.topMargin: 12
                    anchors.bottomMargin: 12
                    spacing: 12

                    // Type icon
                    Text {
                        text: root._glyph(model.type)
                        font.family: iconFont.name
                        font.pixelSize: 24
                        color: root._accentColor(model.type)
                        Layout.alignment: Qt.AlignTop
                    }

                    // Message
                    Text {
                        text: model.text
                        color: theme.textPrimary
                        font.pixelSize: 13
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                        Layout.alignment: Qt.AlignVCenter
                    }

                    // Close button (only when dismissible)
                    Item {
                        Layout.alignment: Qt.AlignTop
                        width: 20
                        height: 20
                        visible: model.dismissible
                        Text {
                            id: closeGlyph
                            anchors.centerIn: parent
                            text: "\ue9b4"   // cross
                            font.family: iconFont.name
                            font.pixelSize: 14
                            color: closeArea.containsMouse ? theme.textPrimary : theme.textTertiary
                        }
                        MouseArea {
                            id: closeArea
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: root.dismiss(model.id)
                        }
                    }
                }
            }
```

- [ ] **Step 4: Run the app and verify the visual**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
python main.py
```

- Press `Ctrl+Shift+1` — blue info toast with `information-2` glyph appears bottom-right.
- Press `Ctrl+Shift+2` — green success toast with `check-circle` glyph stacks above (newest at the bottom).
- Press `Ctrl+Shift+3` — yellow warning toast with `notification-bing` (bell) glyph.
- Press `Ctrl+Shift+4` — red error toast with `cross-circle` glyph.
- Click the ✕ on any toast → it disappears immediately.
- Fire a long-text notification by pressing the same shortcut again to confirm wrap behavior is sane.

If a glyph renders as a blank box, the font isn't loading — check the relative `source:` path in the `FontLoader` (`../assets/fonts/keenicons-outline.ttf` from `components/`).

- [ ] **Step 5: Commit**

```bash
git add components/NotificationCenter.qml
git commit -m "feat(notify): real toast delegate with stripe, icon, and close button"
```

---

## Task 4: Slide+fade animations and stack transitions

**Files:**
- Modify: `components/NotificationCenter.qml`

- [ ] **Step 1: Wrap the toast delegate in an animated `Item`**

The existing `delegate: Rectangle { ... }` becomes the inner of a wrapper `Item` that owns the animated `x` and `opacity` properties. Replace the entire `delegate:` from Task 3 with:

```qml
            delegate: Item {
                id: wrapper
                width: 340
                height: toast.height
                opacity: 0
                x: 60      // start offscreen-right (relative to its slot in the Column)

                // Enter animation runs on completion.
                Component.onCompleted: enterAnim.start()

                ParallelAnimation {
                    id: enterAnim
                    NumberAnimation { target: wrapper; property: "x";       to: 0; duration: 180; easing.type: Easing.OutCubic }
                    NumberAnimation { target: wrapper; property: "opacity"; to: 1; duration: 180; easing.type: Easing.OutCubic }
                }

                // Exit: triggered by `dismissAnimated()` (called by timer or close click).
                // When the parallel animation finishes, remove the model entry so the
                // Column's `move` transition can collapse the empty slot.
                function dismissAnimated() { exitAnim.start() }

                ParallelAnimation {
                    id: exitAnim
                    NumberAnimation { target: wrapper; property: "x";       to: 60; duration: 160; easing.type: Easing.InCubic }
                    NumberAnimation { target: wrapper; property: "opacity"; to: 0;  duration: 160; easing.type: Easing.InCubic }
                    onStopped: root.dismiss(model.id)
                }

                Rectangle {
                    id: toast
                    anchors.left: parent.left
                    anchors.right: parent.right
                    implicitHeight: contentRow.implicitHeight + 24
                    height: implicitHeight
                    radius: 10
                    color: theme.bgElevated
                    border.color: theme.borderSubtle
                    border.width: 1

                    // Left accent stripe
                    Rectangle {
                        width: 3
                        height: parent.height - 8
                        radius: 1.5
                        color: root._accentColor(model.type)
                        anchors.left: parent.left
                        anchors.leftMargin: 4
                        anchors.verticalCenter: parent.verticalCenter
                    }

                    RowLayout {
                        id: contentRow
                        anchors.fill: parent
                        anchors.leftMargin: 16
                        anchors.rightMargin: 12
                        anchors.topMargin: 12
                        anchors.bottomMargin: 12
                        spacing: 12

                        Text {
                            text: root._glyph(model.type)
                            font.family: iconFont.name
                            font.pixelSize: 24
                            color: root._accentColor(model.type)
                            Layout.alignment: Qt.AlignTop
                        }

                        Text {
                            text: model.text
                            color: theme.textPrimary
                            font.pixelSize: 13
                            wrapMode: Text.Wrap
                            Layout.fillWidth: true
                            Layout.alignment: Qt.AlignVCenter
                        }

                        Item {
                            Layout.alignment: Qt.AlignTop
                            width: 20
                            height: 20
                            visible: model.dismissible
                            Text {
                                anchors.centerIn: parent
                                text: "\ue9b4"
                                font.family: iconFont.name
                                font.pixelSize: 14
                                color: closeArea.containsMouse ? theme.textPrimary : theme.textTertiary
                            }
                            MouseArea {
                                id: closeArea
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: wrapper.dismissAnimated()
                            }
                        }
                    }
                }
            }
```

Key changes from Task 3:
- The `MouseArea`'s `onClicked` now calls `wrapper.dismissAnimated()` (which animates out, then removes from the model on `onStopped`).
- The wrapper drives both enter and exit animations.

- [ ] **Step 2: Add `Column` `add` and `displaced` transitions for smooth stack motion**

In the same file, find the `Column { id: stack ... }` block. Add transitions immediately after the `anchors.bottomMargin: 24` line:

```qml
    Column {
        id: stack
        spacing: 10
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.rightMargin: 24
        anchors.bottomMargin: 24

        add: Transition {
            // The wrapper's internal enter animation handles fade/slide; we only
            // animate the y-position here so existing toasts shift up smoothly.
            NumberAnimation { properties: "y"; duration: 180; easing.type: Easing.OutCubic }
        }
        move: Transition {
            NumberAnimation { properties: "y"; duration: 180; easing.type: Easing.OutCubic }
        }
        // (No `populate` transition — we don't want the entire stack to animate
        // on initial load; only newly-added toasts should slide in.)

        Repeater {
            ...existing repeater body unchanged...
        }
    }
```

- [ ] **Step 3: Run the app and verify the animations**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
python main.py
```

- Press `Ctrl+Shift+2` — toast slides in from the right and fades up.
- Press it twice more in quick succession — older toasts glide upward to make room for the new one at the bottom.
- Click ✕ on the middle toast — it slides out to the right and fades; the toast above it glides down into its slot.

If a toast snaps in instantly without animation, check that `Component.onCompleted` correctly starts `enterAnim`. If the click-to-dismiss snaps instead of animating, check the `MouseArea.onClicked` is `wrapper.dismissAnimated()` (not `root.dismiss(model.id)`).

- [ ] **Step 4: Commit**

```bash
git add components/NotificationCenter.qml
git commit -m "feat(notify): slide+fade enter/exit animations and stack transitions"
```

---

## Task 5: Auto-dismiss timer with hover-to-pause

**Files:**
- Modify: `components/NotificationCenter.qml`

- [ ] **Step 1: Add the timer and a HoverHandler inside the wrapper**

In the wrapper `Item` from Task 4, add a `Timer` right after the `Component.onCompleted` line, and a `HoverHandler` inside the `Rectangle { id: toast }` block (anywhere among toast's children). `HoverHandler` is purpose-built for hover detection and does not interfere with the inner `closeArea` `MouseArea`'s click handling, unlike a stacked `MouseArea`.

After the `Component.onCompleted: enterAnim.start()` line, add:

```qml
                Component.onCompleted: enterAnim.start()

                // Auto-dismiss timer. Disabled when durationMs == 0 (sticky).
                Timer {
                    id: autoDismiss
                    interval: model.durationMs
                    repeat: false
                    running: model.durationMs > 0
                    onTriggered: wrapper.dismissAnimated()
                }
```

Then, inside the `Rectangle { id: toast }` block — directly after the `border.width: 1` line and before the accent stripe `Rectangle` — add:

```qml
                Rectangle {
                    id: toast
                    anchors.left: parent.left
                    anchors.right: parent.right
                    implicitHeight: contentRow.implicitHeight + 24
                    height: implicitHeight
                    radius: 10
                    color: theme.bgElevated
                    border.color: theme.borderSubtle
                    border.width: 1

                    // Hovering anywhere on the toast pauses the auto-dismiss timer.
                    HoverHandler {
                        id: toastHover
                        onHoveredChanged: {
                            if (hovered) {
                                autoDismiss.stop()
                            } else if (model.durationMs > 0) {
                                autoDismiss.restart()
                            }
                        }
                    }

                    // ...existing left accent stripe and RowLayout follow...
```

(Keep the rest of the `Rectangle` body unchanged — accent stripe and `RowLayout` stay exactly as in Task 4.)

- [ ] **Step 2: Run the app and verify the timer + hover behavior**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
python main.py
```

- Press `Ctrl+Shift+2` — toast appears, then auto-dismisses ~4 seconds later with the slide-out animation.
- Press it again, immediately move the mouse over the toast and hold it there — the toast stays indefinitely.
- Move the mouse off — the timer restarts and dismisses ~4s later.
- Press `Ctrl+Shift+4` (error, `durationMs: 0`) — toast appears and stays until you click ✕. Hovering does nothing because the timer was never running.

- [ ] **Step 3: Commit**

```bash
git add components/NotificationCenter.qml
git commit -m "feat(notify): auto-dismiss timer with hover-to-pause"
```

---

## Task 6: Wire NotesModal close + remove debug shortcuts

**Files:**
- Modify: `components/NotesModal.qml`
- Modify: `main.qml` (remove temporary debug shortcuts)

- [ ] **Step 1: Fire the success toast when NotesModal closes**

In `components/NotesModal.qml`, modify the `close()` function (currently lines 19-22). Existing code:

```qml
    function close() {
        MOTIONInterface.scanNotes = notesArea.text
        root.visible = false
    }
```

Replace with:

```qml
    function close() {
        MOTIONInterface.scanNotes = notesArea.text
        MOTIONInterface.notify("Note saved.", "success", 4000, true)
        root.visible = false
    }
```

- [ ] **Step 2: Remove the temporary debug shortcuts from `main.qml`**

In `main.qml`, find and delete the `// TEMP-NOTIF-DEBUG: remove in Task 6.` block added in Task 2 (the four `Shortcut { ... }` blocks). The `Connections { target: MOTIONInterface }` block stays.

- [ ] **Step 3: End-to-end manual test**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
python main.py
```

- Open the app, navigate to a state where the Notes modal is reachable (the BloodFlow page — click the notes button, or press the existing keyboard shortcut wired in `BloodFlow.qml`).
- Type some text in the notes area.
- Click the ✕ in the top-right of the notes modal (or press Escape).

Expected:
- The notes modal closes.
- A green success toast slides in from the bottom-right reading "Note saved." with the `check-circle` glyph.
- After ~4 seconds it slides out and disappears.
- Hovering over it pauses the timer.
- Clicking ✕ on the toast dismisses it immediately.

Verify the saved text persists by reopening the notes modal — the same text should be there (this confirms `MOTIONInterface.scanNotes = notesArea.text` still ran before the toast call).

Confirm the four `Ctrl+Shift+1..4` shortcuts no longer fire any toast.

- [ ] **Step 4: Commit**

```bash
git add components/NotesModal.qml main.qml
git commit -m "feat(notes): show 'Note saved.' toast when notes modal closes

Also removes the Ctrl+Shift+1..4 debug shortcuts that were used to
develop the notification center. Notes are still persisted via
MOTIONInterface.scanNotes setter exactly as before; the toast is fired
right after."
```

---

## Task 7: Spec verification + branch cleanup

- [ ] **Step 1: Re-read the spec and confirm every requirement is implemented**

Open `docs/superpowers/specs/2026-04-17-toast-notifications-design.md` and walk through each section:

- NotificationRequest shape — ✓ Task 2 (defaults applied in `notify()`)
- NotificationCenter — ✓ Task 2 (model, notify, dismiss, MOTIONInterface bridge)
- Python bridge — ✓ Task 1 (`notificationRequested`, `notify` slot)
- Toast layout (stripe, icon, text, close button) — ✓ Task 3
- Type→color/icon mapping — ✓ Task 3 (`_accentColor`, `_glyph` helpers)
- Container & stacking (bottom-right, 24px margin, max 5, newest at bottom) — ✓ Task 2 + Task 4
- Animation (slide+fade enter/exit, stack transitions) — ✓ Task 4
- Auto-dismiss + hover pause — ✓ Task 5
- Notes pane test case — ✓ Task 6

If anything is missing, add a small task here to fill the gap.

- [ ] **Step 2: Run a final smoke pass**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
python scripts/smoke_notify.py
python main.py   # then trigger Notes pane close and observe toast
```

Both should succeed.

- [ ] **Step 3: Verify nothing else regressed**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
git log --oneline feature/small-notifications ^next | cat
git diff --stat next..HEAD
```

Expected: 6 commits on top of `next`, touching only `motion_connector.py`, `main.qml`, `components/NotificationCenter.qml`, `components/NotesModal.qml`, `scripts/smoke_notify.py`, and the spec doc. No other files modified.
