# Spacebar timestamped scan notes — design

**Date:** 2026-06-11
**Status:** Approved (design); pending implementation

## Problem

While a scan is running, the operator often wants to mark a moment — "patient moved",
"adjusted headset", etc. Today that requires clicking the Notes icon, finding the end of
the text, and manually typing the time. The goal is a single keystroke: **press Spacebar
during a scan to pop the Notes modal with a fresh newline + timestamp already inserted, cursor
ready to type.**

## Scope

In scope:
- A Spacebar shortcut, active only while a scan is actively running, that opens the existing
  `NotesModal` and inserts a timestamped line.
- The inserted timestamp is **both** elapsed scan time and wall-clock time:
  `[HH:MM:SS / HH:MM:SS]` → e.g. `[00:04:32 / 14:32:05] `.

Out of scope:
- Any change to how notes are persisted (the existing `scanNotes` property + on-close save and
  the post-scan notes-file write are unchanged).
- Multiple-timestamp-while-modal-open behavior. Once the modal is open the user is typing, so
  Spacebar inserts a literal space. To add another timestamped entry, close and reopen.

## Chosen approach (A)

QML `Shortcut` for the keystroke + a one-line Python accessor to expose elapsed scan time.
Contained, idiomatic, and adds essentially nothing to the oversized `motion_connector.py`.

Rejected alternatives:
- **B — `QApplication` event filter in Python.** More robust against keyboard-focus conflicts,
  but adds global input handling into the 4031-line connector. Kept only as a fallback if the
  QML `Shortcut` shows focus-routing problems during testing.
- **C — hidden focused Item + `Keys.onPressed`.** Fragile focus management. Rejected.

## Components

### 1. `motion_connector.py` — expose elapsed scan time

The connector already computes trigger-ON elapsed time in `_scan_elapsed_str()` (`HH:MM:SS`,
the same value used for the on-screen countdown, camera-dropout messages, and the
`duration:` line appended to notes at scan end). Add a thin public slot so QML can read it:

```python
@pyqtSlot(result=str)
def scanElapsedStr(self) -> str:
    return self._scan_elapsed_str()
```

No new state; `_scan_elapsed_str()` is already safe to call any time (returns `00:00:00`
when no scan is active).

### 2. `components/NotesModal.qml` — timestamped entry point

Add a new function alongside the existing `open()` / `close()`:

```qml
function openWithTimestamp(stamp) {
    var existing = MOTIONInterface.scanNotes
    var prefix = existing.length > 0 ? existing.replace(/\s+$/, "") + "\n" : ""
    notesArea.text = prefix + "[" + stamp + "] "
    root.visible = true
    notesArea.forceActiveFocus()
    notesArea.cursorPosition = notesArea.text.length
}
```

- Existing content gets a trailing-whitespace trim + a single newline before the timestamp, so
  the new entry always starts on its own line ("does a \n"). An empty note gets no leading blank
  line.
- Cursor lands immediately after `[stamp] `, ready to type.
- `close()` is unchanged — it already saves `notesArea.text` back to `MOTIONInterface.scanNotes`,
  which persists to disk. No new save path.

### 3. `pages/BloodFlow.qml` — the Spacebar shortcut

Placed next to the existing `NotesModal { id: notesModal }` block:

```qml
Shortcut {
    sequence: "Space"
    enabled: bloodFlow.scanning
             && MOTIONInterface.triggerState === "ON"
             && modalManager.current === null
    onActivated: {
        var elapsed = MOTIONInterface.scanElapsedStr()
        var wall = Qt.formatTime(new Date(), "HH:mm:ss")
        notesModal.openWithTimestamp(elapsed + " / " + wall)
    }
}
```

Gating rationale:
- `bloodFlow.scanning && triggerState === "ON"` — only fires during an active scan.
- `modalManager.current === null` — don't pop notes over another open modal, and (crucially)
  once `NotesModal` itself opens, `current` becomes non-null, so the shortcut disables and
  Spacebar types a literal space in the textarea (the chosen re-trigger behavior).

## Data flow

1. Operator presses Space while a scan runs and no modal is open.
2. `Shortcut.onActivated` reads `scanElapsedStr()` from the connector and formats wall-clock in QML.
3. `notesModal.openWithTimestamp("00:04:32 / 14:32:05")` appends a newline + `[stamp] ` to the
   current notes text, shows the modal, focuses the textarea, parks the cursor at the end.
4. Operator types the note. On close (X, Esc, backdrop click, or app teardown via ModalManager),
   the existing `close()` saves the text to `scanNotes`.

## Error / edge handling

- **No active scan:** shortcut `enabled` is false → Spacebar behaves normally elsewhere.
- **Empty notes:** no leading blank line; the timestamp is the first line.
- **Empty timestamped entry (user opens then closes without typing):** persists a `[stamp] ` line
  with no note. Acceptable — it still records that the operator marked a moment. Not specially trimmed.
- **Keyboard-focus conflict (risk to verify):** if the Start/Stop button retains keyboard focus
  during a scan, Qt could route Space to the button. To be confirmed during testing; if it
  occurs, fix by clearing button focus on scan start or falling back to approach B.

## Testing

Per repo convention QML does not hot-reload and layout/behavior changes need a real run + screenshot.

1. Set `cameraFakeData: true` in `config/app_config.json`, run `python main.py`.
2. Start a scan; once trigger is ON, press Space.
3. Screenshot the Notes modal — verify a `[elapsed / wall]` line was inserted on a fresh line and
   the cursor is ready to type.
4. Type a note; press Space again inside the modal — verify it inserts a literal space (no new
   timestamp).
5. Close the modal, reopen via the Notes icon — verify the note persisted.
6. Verify Spacebar does nothing note-related when not scanning.
