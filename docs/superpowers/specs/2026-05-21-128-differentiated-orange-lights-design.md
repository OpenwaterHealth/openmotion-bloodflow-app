# Differentiated Orange Lights — Design Spec

**Date:** 2026-05-21
**Issue:** openmotion-bloodflow-app#128
**Feature:** In developer mode, the Contact Quality modal's per-camera dots color-encode *which* warning type each camera tripped: dark orange for `ambient_light`, light orange for `poor_contact`, and a vertical dark/light split when both fire on the same camera. A small legend strip below the sensor diagrams names the colors. RUO / reduced mode is unchanged — every "bad" camera stays the existing single orange.

---

## Background

Today every per-camera dot in `components/ContactQualityModal.qml` is one of four colors driven by `cameraStatus(side, camIndex1)`:

| Status     | Color (hex)   | Source                         |
|------------|---------------|--------------------------------|
| `good`     | `#A3E4A1`     | Pale green, inline             |
| `bad`      | `#E67E22`     | Strong orange, inline (same as `theme.accentOrange`) |
| `checking` | `#666666`     | Gray, inline                   |
| `inactive` | `#666666`     | Gray, inline                   |

`bad` collapses two distinct conditions into one color. The connector already emits warnings with distinct `typeKey` values (`"ambient_light"` and `"poor_contact"` — `motion_connector.py:2657, 2673`) and the modal stores them in `entries[]`. Today's UI just throws that distinction away at render time.

The two warnings have different operator responses:
- **`ambient_light`** — lift the sensor / dim the room. Optical hygiene.
- **`poor_contact`** — reseat the sensor on the head. Physical placement.

When a dev is debugging a flaky bench setup, knowing *which* failure mode is firing across the eight cameras determines whether they reach for a blackout cloth or re-seat the headset. The current undifferentiated orange forces them to hover each dot in turn.

The modal has 16 nearly-identical inline `Rectangle` dot blocks (8 cameras × 2 sensors) — already past the threshold where extracting a component is the cleaner path.

---

## Requirements

| #  | Requirement |
|----|-------------|
| R1 | A new `components/CameraDot.qml` encapsulates a single dot's color + hover + tooltip rendering. Takes `side`, `camIndex1`, `modal` (the parent `ContactQualityModal`), and an optional `size`. |
| R2 | `ContactQualityModal.qml` replaces its 16 inline `Rectangle` dot blocks with `CameraDot { … }` instantiations. Per-camera tooltip behavior is preserved. |
| R3 | `components/AppTheme.qml` defines two new tokens: `accentOrangeAmbient = "#9A4012"` and `accentOrangeContact = "#F4A460"`. Existing `accentOrange = "#E67E22"` is unchanged and still used by the modal's panel border. |
| R4 | When `developerMode` is true *and* the camera is `bad`: dot color is `accentOrangeAmbient` if only `ambient_light` is present, `accentOrangeContact` if only `poor_contact` is present, and a vertical split (dark left half, light right half) if both are present. |
| R5 | When `developerMode` is false *and* the camera is `bad`: dot color falls back to the existing `#E67E22`. No split rendering. The legend strip is hidden. |
| R6 | A legend strip is inserted below the sensor `RowLayout`, visible only when `developerMode && (state_ === "ok" || state_ === "warnings")`. Three entries: an ambient swatch + label, a contact swatch + label, a split swatch + label "both". Minimal chrome — no card / background. |
| R7 | If a future third `typeKey` lands (not `"ambient_light"` or `"poor_contact"`), `CameraDot` falls back to single dark-orange and logs at debug level once per render. The change here must not actively break in that case. |
| R8 | A new `cameraWarningTypes(side, camIndex1)` helper on `ContactQualityModal` returns a deduped array of typeKeys present for that camera. The existing `cameraColor(side, camIndex1)` is removed — `CameraDot` computes its own color from `cameraStatus` + `cameraWarningTypes` internally, and the inline `cameraColor` callers go away when the 16 dot blocks are replaced. No external callers exist (verified by grep). |

---

## Architecture

### `components/AppTheme.qml`

Add two color tokens alongside the existing `accentOrange`:

```qml
readonly property color accentOrange:        "#E67E22"   // unchanged
readonly property color accentOrangeAmbient: "#9A4012"   // NEW — dev-mode ambient_light
readonly property color accentOrangeContact: "#F4A460"   // NEW — dev-mode poor_contact
```

### `components/CameraDot.qml` — new file

```qml
import QtQuick 6.0
import QtQuick.Controls as Controls

Item {
    id: root
    property string side
    property int camIndex1
    required property var modal
    property int size: 18

    width: size; height: size

    AppTheme { id: theme }

    readonly property string status: modal.cameraStatus(side, camIndex1)
    readonly property var    types: status === "bad" && modal.developerMode
                                    ? modal.cameraWarningTypes(side, camIndex1)
                                    : []

    readonly property bool   isSplit: types.indexOf("ambient_light") >= 0
                                      && types.indexOf("poor_contact") >= 0

    readonly property color  singleColor: {
        if (status === "good")     return "#A3E4A1"
        if (status === "checking") return "#666666"
        if (status === "inactive") return "#666666"
        // status === "bad" past this point
        if (!modal.developerMode)  return "#E67E22"
        if (types.indexOf("ambient_light") >= 0 && !isSplit)
            return theme.accentOrangeAmbient
        if (types.indexOf("poor_contact") >= 0 && !isSplit)
            return theme.accentOrangeContact
        // Unknown / future typeKey — fall back to dark orange.
        return theme.accentOrangeAmbient
    }

    // Solid case (no split)
    Rectangle {
        anchors.fill: parent
        visible: !root.isSplit
        radius: parent.width / 2
        color: root.singleColor
        border.color: "black"
        border.width: 1
    }

    // Split case — two clipped half-rects inside a circular outline
    Rectangle {
        anchors.fill: parent
        visible: root.isSplit
        radius: parent.width / 2
        border.color: "black"
        border.width: 1
        color: "transparent"
        clip: true

        Rectangle {
            width: parent.width / 2; height: parent.height
            anchors.left: parent.left
            color: theme.accentOrangeAmbient
        }
        Rectangle {
            width: parent.width / 2; height: parent.height
            anchors.right: parent.right
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

Split rendering uses two clipped child Rectangles rather than a `LinearGradient`. Two reasons: (a) sharp vertical transition at 50% is trivially exact with anchored half-rects, (b) `LinearGradient` on a `Rectangle` with `radius` antialiases the gradient edge, which gets fuzzy at 18px.

Implementation note: a child `Rectangle` anchored flush to the outer Rectangle's edge will overdraw the outer's border. Inset the child rects by `border.width` (1px) on each side that touches the outer border, or layer the border on top with a higher-z transparent overlay. Either works; pick whichever reads cleaner once on screen.

### `components/ContactQualityModal.qml`

**Replace the 16 inline dot blocks.** Each row in the existing 3-column grid currently looks like:

```qml
Rectangle { width: parent.cs; height: parent.cs; radius: parent.cs/2
    color: cameraColor("left", 1); border.color: "black"; border.width: 1
    MouseArea { id: lh1; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
    Controls.ToolTip.visible: lh1.containsMouse; Controls.ToolTip.text: cameraTooltip("left", 1) }
```

Becomes:

```qml
CameraDot { side: "left"; camIndex1: 1; modal: root; size: parent.cs }
```

The grid layout stays identical (3-column with empty `Item {}` spacers and the yellow connector dot at the bottom — out of scope).

**Add `cameraWarningTypes` helper** next to the existing `cameraStatus` function:

```qml
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

`entries.length` is ≤16 (one entry per camera per typeKey), so the linear scan is fine.

**Remove `cameraColor`.** Once `CameraDot` replaces the inline Rectangles, nothing calls `cameraColor` anymore (the legend uses theme tokens directly). Grep confirms no external callers. Delete the function rather than leaving it as dead code.

**Add the legend strip** below the sensor `RowLayout`, inside the same `ColumnLayout`:

```qml
RowLayout {
    visible: root.developerMode
             && (root.state_ === "ok" || root.state_ === "warnings")
    Layout.alignment: Qt.AlignHCenter
    spacing: 18

    RowLayout { spacing: 6
        Rectangle { width: 10; height: 10; radius: 5
            color: theme.accentOrangeAmbient
            border.color: "black"; border.width: 1
        }
        Text { text: "ambient"; color: theme.textSecondary; font.pixelSize: 11 }
    }
    RowLayout { spacing: 6
        Rectangle { width: 10; height: 10; radius: 5
            color: theme.accentOrangeContact
            border.color: "black"; border.width: 1
        }
        Text { text: "contact"; color: theme.textSecondary; font.pixelSize: 11 }
    }
    RowLayout { spacing: 6
        // Reuse the same split-render approach as CameraDot.
        Item { width: 10; height: 10
            Rectangle { anchors.fill: parent; radius: 5
                border.color: "black"; border.width: 1
                color: "transparent"; clip: true
                Rectangle { width: parent.width/2; height: parent.height
                    anchors.left: parent.left
                    color: theme.accentOrangeAmbient
                }
                Rectangle { width: parent.width/2; height: parent.height
                    anchors.right: parent.right
                    color: theme.accentOrangeContact
                }
            }
        }
        Text { text: "both"; color: theme.textSecondary; font.pixelSize: 11 }
    }
}
```

Inline rather than extracted because the legend is rendered exactly once.

---

## Data Flow

```
motion_connector.py
  └─ emits contactQualityWarning(camera, typeKey, typeText, value)
       (typeKey ∈ {"ambient_light", "poor_contact"})

BloodFlow.qml
  └─ contactQualityModal.addWarning(camera, typeKey, typeText, value)

ContactQualityModal.qml
  └─ entries[] gets {camera, typeKey, typeText, value}
  └─ state_ → "warnings"

CameraDot.qml (per-camera, on entries change)
  └─ status = modal.cameraStatus(side, camIndex1)        // good/bad/checking/inactive
  └─ if (status === "bad" && modal.developerMode):
        types = modal.cameraWarningTypes(side, camIndex1) // ["ambient_light"], ["poor_contact"], or both
        if both → render split (two clipped half-Rectangles)
        else    → render solid (singleColor)
  └─ else → render solid (existing color logic)
```

---

## Edge Cases

- **Non-developer mode:** `singleColor` returns existing `#E67E22` for any `bad`; `isSplit` is always false (gated on `developerMode`); legend's `visible` is false. Net behavior = today.
- **`addWarning` upserts:** the existing implementation already dedupes by `(camera, typeKey)`. `cameraWarningTypes` dedupes again defensively (cheap, ≤16 entries).
- **Unknown future `typeKey`:** `singleColor` falls back to `accentOrangeAmbient` (dark orange) and `isSplit` requires both `"ambient_light"` *and* `"poor_contact"` specifically, so the unknown type can't accidentally trigger a split. Visually the dev sees a dark-orange dot whose tooltip names the unknown type. No silent breakage.
- **Camera mask changes mid-scan:** `cameraStatus` already returns `"inactive"` first; `CameraDot.singleColor` short-circuits on `inactive` and never reaches the types-based branches.
- **Modal panel border:** `theme.accentOrange` stays the panel border color when `state_ === "warnings"`. Border is a generic "warnings exist" indicator and intentionally does not encode type. Out of scope for this issue.
- **Dark mode toggle:** the new tokens are not mode-dependent (orange reads on both backgrounds). If a future light-mode pass wants different oranges, add the conditional to `AppTheme` then; not needed now.

---

## Testing

### Manual verification (golden path)

1. Launch the app from source on the `feature/128-differentiated-orange-lights` branch with `"developerMode": true` in `config/app_config.json`.
2. Open the Contact Quality modal (either via Check button or trigger a live scan with warnings). With no warnings present, confirm: all dots green, **no legend visible** (because `state_ === "checking"` momentarily, then `"ok"` shows legend per R6 — verify it appears in both `ok` and `warnings`).
3. Drive synthetic warnings via the connector or a test script. Three cases per camera:
   - **Ambient only** — `MOTIONInterface.contactQualityWarning.emit("L3", "ambient_light", "Ambient light", 4.2)`. Dot L3 → dark orange.
   - **Contact only** — same with `"poor_contact"`. Dot L3 → light orange.
   - **Both** — emit both. Dot L3 → vertical split, dark left / light right.
4. Verify the legend strip displays the same three swatches under the sensor diagrams.
5. Hover each dot. Tooltip text is unchanged from today.

### Manual verification (non-dev fallback)

6. Flip `"developerMode": false`. Restart app. Repeat step 3. Every "bad" dot is `#E67E22` regardless of type. Legend is hidden.

### Regression net (optional)

The current test suite (`tests/`) does not exercise CQ modal rendering. Adding a pixmap-diff harness for this one feature is more scaffolding than the change is worth. A lightweight QML helper that asserts `cameraWarningTypes` returns the expected results for synthetic entry sets is cheap if you want a regression net; not included in scope by default.

---

## Implementation Order

Single repo (bloodflow-app), small scope, no SDK changes. Order:

1. **Add theme tokens.** `accentOrangeAmbient` and `accentOrangeContact` in `components/AppTheme.qml`. One commit.
2. **Add `cameraWarningTypes` helper.** Pure-JS function in `ContactQualityModal.qml`; safe to land before the rest because nothing calls it yet. One commit.
3. **Create `components/CameraDot.qml`.** New file, self-contained. Not yet wired in. One commit.
4. **Wire `CameraDot` into the modal.** Replace all 16 inline `Rectangle` blocks. One commit.
5. **Add the legend strip.** One commit.
6. **Manual verification** per the test plan above. Fix anything that surfaces, then PR.

Each step is small, individually verifiable, and reversible.

---

## YAGNI / Out of Scope

- **Animation on color transitions.** Snap is fine; the dot already snaps between green/orange/gray today.
- **Tooltip text changes.** Existing text already names the type (e.g. `"Ambient light (3.7 DN)"`); the new colors add a second-channel cue, not a replacement.
- **Modal panel border differentiation.** Border stays generic `accentOrange` — it's a "warnings exist" indicator, not a type indicator.
- **Other UI surfaces.** Per the brainstorming confirmation, dots are the only place this lives. No live-scan banner / BloodFlow page badge / etc.
- **Per-sensor (aggregate) version of the indicator.** Per-camera matches both the existing dot model and the issue text reading.
- **SDK changes.** `typeKey` is already distinct on the wire; nothing to plumb.
- **Light-mode-specific orange palette.** Current pair reads on both backgrounds; if light-mode QA dislikes it, defer to a separate change.
- **Automated regression test.** Not included by default; cheap to add later if regression risk justifies it.
