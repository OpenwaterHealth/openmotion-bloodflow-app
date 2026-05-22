# Differentiated Orange Lights — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In developer mode, color-code the Contact Quality modal's per-camera dots by warning type (dark orange = ambient_light, light orange = poor_contact, vertical split = both), with a small legend below the sensor diagrams. RUO / reduced mode is unchanged.

**Architecture:** A new `components/CameraDot.qml` encapsulates a single dot's render logic (single color or two clipped half-rects for the split case). `ContactQualityModal.qml` replaces its 16 inline dot `Rectangle` blocks with `CameraDot` instances, adds a `cameraWarningTypes(side, camIndex1)` helper, removes the now-unused `cameraColor`, and renders the legend strip below the sensor `RowLayout`. Two new color tokens (`accentOrangeAmbient`, `accentOrangeContact`) live in `AppTheme.qml`. No SDK changes; warning typeKeys (`"ambient_light"`, `"poor_contact"`) are already distinct on the wire.

**Tech Stack:** QML 6.0 + Qt Quick Controls (PyQt6). Manual UI verification only — no QML test harness exists in this repo and adding one is out of scope for this feature.

**Spec:** `docs/superpowers/specs/2026-05-21-128-differentiated-orange-lights-design.md`

---

## Working branch

Already on `feature/128-differentiated-orange-lights` (off `next`). All commits in this plan land on that branch. The spec is already committed (commit `068ee8a`).

---

## File map

- **Create** `components/CameraDot.qml` — single dot rendering, ~50 LOC.
- **Modify** `components/AppTheme.qml` — add two color tokens.
- **Modify** `components/ContactQualityModal.qml` — add `cameraWarningTypes` helper, replace 16 inline `Rectangle` dot blocks with `CameraDot` instances, remove `cameraColor`, add the legend strip.

---

## Task 1 — Add the two color tokens to `AppTheme.qml`

**Files:**
- Modify: `components/AppTheme.qml`

Standalone, zero-risk addition. Lands first so subsequent tasks can reference the tokens.

- [ ] **Step 1: Add the two tokens next to the existing `accentOrange`**

Open `components/AppTheme.qml`. Find the accent-colors block (around lines 41–47):

```qml
    // ── accent colours (same in both modes) ───────────────────────
    readonly property color accentBlue:   "#4A90E2"
    readonly property color accentGreen:  "#2ECC71"
    readonly property color accentRed:    "#E74C3C"
    readonly property color accentYellow: "#F1C40F"
    readonly property color accentOrange: "#E67E22"
```

Replace it with:

```qml
    // ── accent colours (same in both modes) ───────────────────────
    readonly property color accentBlue:          "#4A90E2"
    readonly property color accentGreen:         "#2ECC71"
    readonly property color accentRed:           "#E74C3C"
    readonly property color accentYellow:        "#F1C40F"
    readonly property color accentOrange:        "#E67E22"
    readonly property color accentOrangeAmbient: "#9A4012"
    readonly property color accentOrangeContact: "#F4A460"
```

- [ ] **Step 2: Syntax-check by launching the app briefly**

Run:
```powershell
cd C:\Users\ethan\Projects\openmotion-bloodflow-app
python -c "from PyQt6.QtQml import QQmlEngine; e = QQmlEngine(); print('QML engine ok')"
```
Expected: `QML engine ok`. This doesn't load the QML file but proves PyQt6 is healthy; the real load check happens once the app opens in Task 4.

- [ ] **Step 3: Commit**

```bash
git add components/AppTheme.qml
git commit -m "feat(bloodflow-app): add ambient/contact orange tokens to AppTheme (#128)"
```

---

## Task 2 — Add `cameraWarningTypes` helper to `ContactQualityModal.qml`

**Files:**
- Modify: `components/ContactQualityModal.qml`

Pure-JS function, no consumers yet. Lands as its own commit so the bigger restructuring in Task 4 only contains the wiring change.

- [ ] **Step 1: Add the helper next to `cameraStatus`**

Open `components/ContactQualityModal.qml`. Find `cameraStatus` (around line 181). Immediately after the closing `}` of `cameraStatus` (around line 192, before `cameraTooltip` starts at line 194), insert:

```qml
    // Returns the deduped typeKey array for a camera's active warnings.
    // Empty when the camera has no warnings. Used by CameraDot (#128)
    // to decide between single-color and split rendering in dev mode.
    function cameraWarningTypes(side, camIndex1) {
        var prefix = (side === "left") ? "L" : "R"
        var label = prefix + camIndex1
        var types = []
        for (var i = 0; i < entries.length; ++i) {
            if (entries[i].camera === label
                    && types.indexOf(entries[i].typeKey) === -1)
                types.push(entries[i].typeKey)
        }
        return types
    }
```

- [ ] **Step 2: Commit**

```bash
git add components/ContactQualityModal.qml
git commit -m "feat(bloodflow-app): cameraWarningTypes helper on CQ modal (#128)"
```

---

## Task 3 — Create `components/CameraDot.qml`

**Files:**
- Create: `components/CameraDot.qml`

Self-contained new component. Not wired in by anything yet — wiring happens in Task 4.

- [ ] **Step 1: Create the file**

Write `components/CameraDot.qml` with this exact content:

```qml
import QtQuick 6.0
import QtQuick.Controls as Controls

/*  CameraDot — single per-camera contact-quality indicator.
 *
 *  Used in ContactQualityModal sensor diagrams. Encapsulates the
 *  status/type lookup and renders either a solid circular dot or
 *  a vertical-split dot (dark left, light right) for the dev-mode
 *  "ambient + contact both fired" case (#128).
 *
 *  API:
 *      side       : "left" | "right"
 *      camIndex1  : 1..8
 *      modal      : the ContactQualityModal root (required — used to
 *                   call cameraStatus / cameraWarningTypes / cameraTooltip
 *                   and to read developerMode)
 *      size       : pixel size of the dot (default 18)
 */
Item {
    id: root

    property string side
    property int    camIndex1
    required property var modal
    property int    size: 18

    width: size
    height: size

    AppTheme { id: theme }

    readonly property string status: modal.cameraStatus(side, camIndex1)
    readonly property var    types: status === "bad" && modal.developerMode
                                    ? modal.cameraWarningTypes(side, camIndex1)
                                    : []
    readonly property bool   hasAmbient: types.indexOf("ambient_light") >= 0
    readonly property bool   hasContact: types.indexOf("poor_contact") >= 0
    readonly property bool   isSplit:    hasAmbient && hasContact

    readonly property color singleColor: {
        if (status === "good")     return "#A3E4A1"
        if (status === "checking") return "#666666"
        if (status === "inactive") return "#666666"
        // status === "bad" past this point
        if (!modal.developerMode)  return "#E67E22"
        if (hasAmbient && !isSplit) return theme.accentOrangeAmbient
        if (hasContact && !isSplit) return theme.accentOrangeContact
        // Unknown / future typeKey — fall back to dark orange.
        return theme.accentOrangeAmbient
    }

    // Solid case (no split) — single coloured circle.
    Rectangle {
        anchors.fill: parent
        visible: !root.isSplit
        radius: parent.width / 2
        color: root.singleColor
        border.color: "black"
        border.width: 1
    }

    // Split case — two clipped half-rects inset 1px on every edge so
    // they don't overdraw the outer Rectangle's 1px border. The outer
    // Rectangle owns the rounded border and clips its children to the
    // circular shape.
    Rectangle {
        id: splitFrame
        anchors.fill: parent
        visible: root.isSplit
        radius: parent.width / 2
        border.color: "black"
        border.width: 1
        color: "transparent"
        clip: true

        Rectangle {
            x: 1; y: 1
            width: (parent.width - 2) / 2
            height: parent.height - 2
            color: theme.accentOrangeAmbient
        }
        Rectangle {
            x: parent.width / 2
            y: 1
            width: (parent.width - 2) / 2
            height: parent.height - 2
            color: theme.accentOrangeContact
        }
    }

    MouseArea {
        id: hover
        anchors.fill: parent
        hoverEnabled: true
        acceptedButtons: Qt.NoButton
    }
    Controls.ToolTip.visible: hover.containsMouse
    Controls.ToolTip.text: modal.cameraTooltip(side, camIndex1)
}
```

- [ ] **Step 2: Commit**

```bash
git add components/CameraDot.qml
git commit -m "feat(bloodflow-app): CameraDot component for differentiated CQ indicators (#128)"
```

---

## Task 4 — Wire `CameraDot` into `ContactQualityModal.qml` and remove `cameraColor`

**Files:**
- Modify: `components/ContactQualityModal.qml`

The big visible step. Replaces 16 inline `Rectangle` blocks (8 per sensor) with `CameraDot` instances and deletes the now-unused `cameraColor` function.

- [ ] **Step 1: Delete `cameraColor`**

In `components/ContactQualityModal.qml`, find `cameraColor` (around lines 212–218):

```qml
    function cameraColor(side, camIndex1) {
        var st = cameraStatus(side, camIndex1)
        if (st === "good")     return "#A3E4A1"  // pale green
        if (st === "bad")      return "#E67E22"  // strong orange
        if (st === "checking") return "#666666"
        return "#666666"
    }
```

Delete the entire function (the block above, including its closing `}`). All color-decision logic now lives in `CameraDot.singleColor`.

- [ ] **Step 2: Replace the left-sensor `GridLayout` body**

Find the left-sensor `GridLayout` (around lines 394–444). Replace its body — keep the outer `GridLayout { columns: 3 … property int cs: 18 ` and its closing `}` — but swap the 16 dot `Rectangle` blocks for `CameraDot` instances. The full replacement:

```qml
                        GridLayout {
                            columns: 3; columnSpacing: 16; rowSpacing: 8
                            Layout.alignment: Qt.AlignHCenter
                            property int cs: 18

                            CameraDot { side: "left"; camIndex1: 1; modal: root; size: parent.cs }
                            Item {}
                            CameraDot { side: "left"; camIndex1: 8; modal: root; size: parent.cs }

                            CameraDot { side: "left"; camIndex1: 2; modal: root; size: parent.cs }
                            Item {}
                            CameraDot { side: "left"; camIndex1: 7; modal: root; size: parent.cs }

                            CameraDot { side: "left"; camIndex1: 3; modal: root; size: parent.cs }
                            Item {}
                            CameraDot { side: "left"; camIndex1: 6; modal: root; size: parent.cs }

                            CameraDot { side: "left"; camIndex1: 4; modal: root; size: parent.cs }
                            Item {}
                            CameraDot { side: "left"; camIndex1: 5; modal: root; size: parent.cs }

                            Item {}
                            Rectangle { width: parent.cs; height: parent.cs; radius: parent.cs/2
                                color: "#FFD700"; border.color: "black"; border.width: 1 }
                            Item {}
                        }
```

The yellow connector dot at the bottom stays as a plain `Rectangle` — it's not a contact-quality indicator and is out of scope.

- [ ] **Step 3: Replace the right-sensor `GridLayout` body**

Find the right-sensor `GridLayout` (around lines 464–513). Apply the same swap with `side: "right"` and identical camera indices:

```qml
                        GridLayout {
                            columns: 3; columnSpacing: 16; rowSpacing: 8
                            Layout.alignment: Qt.AlignHCenter
                            property int cs: 18

                            CameraDot { side: "right"; camIndex1: 1; modal: root; size: parent.cs }
                            Item {}
                            CameraDot { side: "right"; camIndex1: 8; modal: root; size: parent.cs }

                            CameraDot { side: "right"; camIndex1: 2; modal: root; size: parent.cs }
                            Item {}
                            CameraDot { side: "right"; camIndex1: 7; modal: root; size: parent.cs }

                            CameraDot { side: "right"; camIndex1: 3; modal: root; size: parent.cs }
                            Item {}
                            CameraDot { side: "right"; camIndex1: 6; modal: root; size: parent.cs }

                            CameraDot { side: "right"; camIndex1: 4; modal: root; size: parent.cs }
                            Item {}
                            CameraDot { side: "right"; camIndex1: 5; modal: root; size: parent.cs }

                            Item {}
                            Rectangle { width: parent.cs; height: parent.cs; radius: parent.cs/2
                                color: "#FFD700"; border.color: "black"; border.width: 1 }
                            Item {}
                        }
```

- [ ] **Step 4: Launch the app and verify default rendering**

Run:
```powershell
cd C:\Users\ethan\Projects\openmotion-bloodflow-app
python main.py
```

In the app:
1. Open the Contact Quality modal (e.g., via the Check button on the BloodFlow page, or trigger a live scan).
2. With no warnings active, confirm all dots render green (the same `#A3E4A1` as before).
3. Hover a dot — the tooltip should still show the camera label (e.g., `"L3"`) just like before.

If you see a missing-component error in the terminal (`CameraDot is not a type`), QML can't find the new file — check that `components/CameraDot.qml` was committed in Task 3 and is in the same `components/` directory as `ContactQualityModal.qml`. QML auto-discovers sibling components by filename.

If the app crashes with `Cannot assign to non-existent property "modal"`, the `required property var modal` line was dropped — restore it in `CameraDot.qml`.

Close the app cleanly (don't `Ctrl+C` — use the window close button) so any pending writes flush.

- [ ] **Step 5: Commit**

```bash
git add components/ContactQualityModal.qml
git commit -m "refactor(bloodflow-app): replace inline CQ dots with CameraDot, drop cameraColor (#128)"
```

---

## Task 5 — Add the legend strip

**Files:**
- Modify: `components/ContactQualityModal.qml`

Inserted below the sensor `RowLayout`, inside the same `ColumnLayout`. Developer mode only.

- [ ] **Step 1: Locate the insertion point**

In `components/ContactQualityModal.qml`, find the sensor `RowLayout` that contains both sensor cards (it starts around line 371 with `RowLayout { visible: root.state_ === "ok" || root.state_ === "warnings"` and ends around line 516 with its closing `}`).

Immediately *after* that `RowLayout`'s closing `}` and *before* the `Item { Layout.fillHeight: true … }` spacer (around line 518), insert the legend strip:

```qml
            // Per-camera dot color legend (#128). Developer mode only —
            // RUO operators just see a single orange and don't need this.
            RowLayout {
                visible: root.developerMode
                         && (root.state_ === "ok" || root.state_ === "warnings")
                Layout.alignment: Qt.AlignHCenter
                spacing: 18

                RowLayout {
                    spacing: 6
                    Rectangle { width: 10; height: 10; radius: 5
                        color: theme.accentOrangeAmbient
                        border.color: "black"; border.width: 1 }
                    Text { text: "ambient"; color: theme.textSecondary; font.pixelSize: 11 }
                }
                RowLayout {
                    spacing: 6
                    Rectangle { width: 10; height: 10; radius: 5
                        color: theme.accentOrangeContact
                        border.color: "black"; border.width: 1 }
                    Text { text: "contact"; color: theme.textSecondary; font.pixelSize: 11 }
                }
                RowLayout {
                    spacing: 6
                    Item {
                        width: 10; height: 10
                        Rectangle {
                            anchors.fill: parent
                            radius: 5
                            border.color: "black"; border.width: 1
                            color: "transparent"
                            clip: true
                            Rectangle { x: 1; y: 1
                                width: (parent.width - 2) / 2
                                height: parent.height - 2
                                color: theme.accentOrangeAmbient }
                            Rectangle { x: parent.width / 2; y: 1
                                width: (parent.width - 2) / 2
                                height: parent.height - 2
                                color: theme.accentOrangeContact }
                        }
                    }
                    Text { text: "both"; color: theme.textSecondary; font.pixelSize: 11 }
                }
            }
```

- [ ] **Step 2: Launch the app to verify the legend appears in dev mode**

Make sure developer mode is on. Open `config/app_config.json` and confirm:
```json
"developerMode": true,
```

(If the key doesn't exist, add it inside the top-level object.)

Run:
```powershell
python main.py
```

Open the Contact Quality modal. After the initial "checking" state transitions to `"ok"`, you should see the three-swatch legend (ambient / contact / both) horizontally centered below the sensor diagrams.

If the legend doesn't show, check:
- `developerMode` is `true` in `config/app_config.json`.
- The legend's `visible` binding includes both `"ok"` and `"warnings"` states.

- [ ] **Step 3: Verify the legend is hidden in non-dev mode**

Stop the app. Edit `config/app_config.json` and set:
```json
"developerMode": false,
```

Run `python main.py`. Open the CQ modal. The three sensor-diagram dots should render and the legend should be absent.

Restore `developerMode: true` afterwards.

- [ ] **Step 4: Commit**

```bash
git add components/ContactQualityModal.qml
git commit -m "feat(bloodflow-app): per-camera dot legend in CQ modal (dev mode) (#128)"
```

---

## Task 6 — End-to-end manual verification

**Files:**
- None modified — verification + fix-up only.

Drive each color case and confirm rendering. This task does not commit unless a defect is found and fixed.

- [ ] **Step 1: Verify each color state in developer mode**

Ensure `"developerMode": true` in `config/app_config.json`. Launch the app and open the Contact Quality modal during a live scan (so that real warnings can flow through).

Three cases per the spec's Testing section — drive each via a real hardware condition that produces the corresponding warning. (Either on the bench by manipulating ambient light / sensor seating, or via an injection harness — see fallback in Step 2 below.)

For each case, confirm:

| Triggered condition           | Expected dot rendering                                      |
|-------------------------------|-------------------------------------------------------------|
| `ambient_light` only on L3    | Dot L3 is dark orange (`#9A4012`)                          |
| `poor_contact` only on L3     | Dot L3 is light orange (`#F4A460`)                         |
| Both on L3                    | Dot L3 is split: dark left half, light right half           |
| No warnings                   | All dots green                                              |
| L3 not in calibration mask    | Dot L3 is gray, no type colors apply                        |

Tooltip on hover continues to name the typeKey ("Ambient light (X.X DN)", "Poor contact (…)") for each.

- [ ] **Step 2: Fallback if hardware can't trigger both warning types on demand**

If you can't reliably drive `poor_contact` on the bench, inject a synthetic warning by attaching to the running app's `MOTIONInterface` from a Python REPL **in the same process** is not possible from outside. Instead, temporarily add a debug keybinding inside `ContactQualityModal.qml` to inject test warnings:

In `components/ContactQualityModal.qml`, add this temporary block inside the `Rectangle { id: panel … }` (e.g., just before the existing `Keys.onReleased`):

```qml
        // TEMP #128 verification — remove before merging.
        Keys.onPressed: function(event) {
            if (!root.developerMode) return
            if (event.modifiers !== Qt.ControlModifier) return
            if (event.key === Qt.Key_1) {
                root.addWarning("L3", "ambient_light", "Ambient light", 4.2)
                event.accepted = true
            } else if (event.key === Qt.Key_2) {
                root.addWarning("L3", "poor_contact", "Poor contact", 0.0)
                event.accepted = true
            } else if (event.key === Qt.Key_3) {
                root.addWarning("L3", "ambient_light", "Ambient light", 4.2)
                root.addWarning("L3", "poor_contact", "Poor contact", 0.0)
                event.accepted = true
            } else if (event.key === Qt.Key_0) {
                root.clearWarning("L3", "ambient_light")
                root.clearWarning("L3", "poor_contact")
                event.accepted = true
            }
        }
```

Then with the modal focused: `Ctrl+1` adds ambient only, `Ctrl+2` adds contact only, `Ctrl+3` adds both, `Ctrl+0` clears L3.

Walk through the four key combos. Verify dot L3 changes color/render exactly as the spec describes.

**Important:** Remove the temporary `Keys.onPressed` block before committing. Do not include the keybinding in any commit.

- [ ] **Step 3: Verify non-dev mode regression**

Stop the app. Set `"developerMode": false` in `config/app_config.json`. Run `python main.py`. Drive any "bad" condition (real or via temporarily re-adding the keybinding — but again, do not commit it). Confirm every "bad" dot renders the existing `#E67E22` regardless of type. Legend is hidden.

Restore `developerMode: true` after the regression check.

- [ ] **Step 4: Verify there are no leftover debug blocks**

Run:
```bash
git diff main components/ContactQualityModal.qml | grep -i "TEMP\|Keys.onPressed.*ambient_light"
```
Expected: no output. If anything appears, remove the debug block and re-stage.

- [ ] **Step 5: Final state check**

Run:
```bash
git status
git log --oneline 068ee8a..HEAD
```

You should see four feature commits on top of the spec commit (`068ee8a`):
1. `feat(bloodflow-app): add ambient/contact orange tokens to AppTheme (#128)`
2. `feat(bloodflow-app): cameraWarningTypes helper on CQ modal (#128)`
3. `feat(bloodflow-app): CameraDot component for differentiated CQ indicators (#128)`
4. `refactor(bloodflow-app): replace inline CQ dots with CameraDot, drop cameraColor (#128)`
5. `feat(bloodflow-app): per-camera dot legend in CQ modal (dev mode) (#128)`

The branch is ready for review. Do not merge to `next` per the milestone workflow — this branch will be merged into `next-next` alongside `feature/132-…` once both features land.

---

## YAGNI reminder

Resist while implementing:
- Animating the color transition. Snap is what the existing green/orange/gray does today.
- Refactoring the sensor diagrams (the surrounding `RowLayout`, the `GridLayout`, the yellow connector dot). Scope is the per-camera dot rendering only.
- Changing the modal panel border to also encode warning type. Border stays generic `accentOrange`.
- Adding light-mode-specific oranges. Current pair reads on both; defer if light-mode QA flags it.
- Adding automated UI tests. No QML test harness exists in this repo; introducing one is its own project.
