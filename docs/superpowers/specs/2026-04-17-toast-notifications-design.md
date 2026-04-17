# Toast Notification System — Design

**Status:** Approved
**Date:** 2026-04-17

## Summary

Add a VS Code-style toast notification system to the OpenMOTION bloodflow app.
Notifications slide in from the bottom-right corner, stack vertically with the
newest at the bottom, auto-dismiss after a configurable duration, and can be
fired from either Python (via `MOTIONInterface`) or QML (directly).

The first consumer is `NotesModal`: when the user closes the notes pane, a
"Note saved." success toast appears.

## Goals

- A single ergonomic API callable from both Python and QML.
- Four notification types — `info`, `success`, `warning`, `error` — each with
  a distinct icon and accent color.
- Stackable toasts (cap 5 visible) with smooth slide+fade animation.
- Optional per-toast dismiss button, optional indefinite duration.
- Hover-to-pause auto-dismiss timer.

## Non-goals

- Notification history pane.
- Audio alerts.
- Progress-style toasts that update over time.
- Inline action buttons within toasts.
- Toast positioning other than bottom-right.

These are deliberately out of scope and can be revisited later.

## Architecture

### NotificationRequest shape

A plain object/dict — no QML type or Python class needed:

```
{
    text: string,
    type: "info" | "success" | "warning" | "error",   // default "info"
    durationMs: int,                                   // default 4000; 0 = sticky
    dismissible: bool                                  // default true
}
```

### NotificationCenter (QML)

A new component at `components/NotificationCenter.qml`, mounted once in
`main.qml`. Responsibilities:

- Owns a `ListModel` of active notifications, each entry assigned a unique id.
- Exposes `notify(request)` — appends a notification, enforces the 5-item cap
  by removing the oldest entry.
- Exposes `dismiss(id)` — removes a single notification by id.
- Renders each notification as a `Toast` delegate inside a vertical `Column`
  anchored to its own bottom-right with 24px margin.
- Listens to `MOTIONInterface.notificationRequested` and forwards the payload
  to its own `notify()`.

The center is `anchors.fill: parent` and `z: 99999`. It does not capture mouse
events on the empty area — only the toast rectangles are interactive.

### Python bridge

Added to `motion_connector.py`:

```python
notificationRequested = pyqtSignal('QVariant')

@pyqtSlot(str, str, int, bool)
def notify(self, text, type_="info", duration_ms=4000, dismissible=True):
    self.notificationRequested.emit({
        "text": text,
        "type": type_,
        "durationMs": duration_ms,
        "dismissible": dismissible,
    })
```

Any Python caller writes:

```python
self.notify("Scan complete.", "success")
self.notify("Lost connection to console.", "error", 0, True)
```

QML callers write:

```qml
MOTIONInterface.notify("Note saved.", "success", 4000, true)
```

QML always routes through `MOTIONInterface.notify(...)` because `id`s in QML
are scoped per file and `NotificationCenter`'s id in `main.qml` is not
visible from nested component files like `NotesModal.qml`. Routing through
the singleton keeps a single uniform call site for all QML callers and
avoids leaking the center's id into the component tree.

## Visual design

### Toast layout

Single row, fixed width 340px, height auto-sized to text content.

```
┌──────────────────────────────────────────────┐
│ [▣]  Note saved.                          ✕  │
└──────────────────────────────────────────────┘
```

- 12px padding all around, 12px gap between icon and text
- 3px colored stripe along the left edge in the type's accent color
- Background: `theme.bgElevated`, `radius: 10`, 1px border `theme.borderSubtle`
- Soft drop shadow for elevation
- Left icon: 24px Keenicons glyph in the type's accent color
- Middle: text in `theme.textPrimary`, `wrapMode: Text.Wrap`
- Right: 20px ✕ close button (Keenicons `cross` glyph, `\ue9b4`) — only
  shown when `dismissible: true`. Hover: text color shifts from
  `theme.textTertiary` to `theme.textPrimary`.

### Type-to-color/icon mapping

| Type     | Color              | Keenicons glyph              |
|----------|--------------------|------------------------------|
| info     | `theme.accentBlue` | `\uea43` (information-2)     |
| success  | `theme.accentGreen`| `\ue99c` (check-circle)      |
| warning  | `theme.accentYellow`| `\uea82` (notification-bing)|
| error    | `theme.accentRed`  | `\ue9b2` (cross-circle)      |

(Keenicons has no triangle/alert glyph; `notification-bing` — a bell with a
notification dot — is the closest available match for warning. Easy to swap
the codepoint later if a better choice is added.)

### Container & stacking

- Anchored to `parent.right` and `parent.bottom` of `NotificationCenter` with
  24px margin from each edge.
- Stack uses `Column { spacing: 10 }`.
- New entries append to the end of the model → render at the bottom of the
  column → newest sits closest to the corner.
- When the model exceeds 5 entries, the oldest (index 0) is removed.

### Animation

Each toast wraps its content in an `Item` with state-driven transitions:

- **Enter:** start at `x: +60` (offscreen-right relative to its anchor) and
  `opacity: 0`, animate to `x: 0` and `opacity: 1` over ~180ms ease-out.
- **Exit:** animate back to `x: +60` and `opacity: 0` over ~160ms ease-in,
  then remove the model entry after the animation completes.
- The parent `Column` provides implicit position transitions so older entries
  glide upward when one exits, and downward when one enters above them.

### Auto-dismiss & hover

Each toast owns a `Timer { interval: durationMs; running: durationMs > 0 }`
that calls `dismiss(id)` on `triggered`. A hovering `MouseArea`:

- `onEntered`: `timer.stop()`
- `onExited`: `timer.restart()`

When `durationMs == 0` the timer never runs — toast stays until the user
clicks ✕ (or until programmatic dismissal).

## Test case: Notes pane save toast

In `components/NotesModal.qml`, modify `close()` (currently lines 19-22):

```qml
function close() {
    MOTIONInterface.scanNotes = notesArea.text
    MOTIONInterface.notify("Note saved.", "success", 4000, true)
    root.visible = false
}
```

## Files

**New**

- `components/NotificationCenter.qml` — the center + toast delegate.

**Modified**

- `main.qml` — mount `NotificationCenter` once, `anchors.fill: parent`,
  `z: 99999`, after the `UpdateBanner` block.
- `components/NotesModal.qml` — fire the success toast in `close()`.
- `motion_connector.py` — add `notificationRequested` signal and `notify` slot.

## Open questions

None. Glyph choices, animation timings, and stack direction are all settled
and easy to tune post-implementation if desired.
