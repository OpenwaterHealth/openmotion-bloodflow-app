# Spacebar Timestamped Scan Notes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** While a scan is running, pressing Spacebar pops the Notes modal with a fresh newline + `[elapsed / wall-clock]` timestamp inserted, cursor ready to type.

**Architecture:** A QML `Shortcut` in `pages/BloodFlow.qml` (gated to active scans, disabled while any modal is open) reads elapsed scan time from a new one-line `@pyqtSlot` on the connector and wall-clock time from QML's `Date`, then calls a new `openWithTimestamp()` on the existing `NotesModal`. Persistence is unchanged — `NotesModal.close()` already saves the text back to `scanNotes`.

**Tech Stack:** PyQt6, QML (QtQuick Controls 6), pytest (Python-side unit test). Mock hardware via `cameraFakeData: true` for the manual run.

**Spec:** [docs/superpowers/specs/2026-06-11-spacebar-timestamped-scan-notes-design.md](../specs/2026-06-11-spacebar-timestamped-scan-notes-design.md)

---

### Task 1: Expose elapsed scan time to QML

**Files:**
- Modify: `motion_connector.py` (add slot near the other `@pyqtSlot` methods; `_scan_elapsed_str` is defined at line ~2880)
- Test: `tests/test_scan_elapsed_slot.py` (create)

The connector already has the private `_scan_elapsed_str()` returning trigger-ON elapsed time
as `HH:MM:SS`, and it is safe to call any time (returns `00:00:00` when no scan is active,
because `_trigger_cumulative_s` is `0.0` and `_trigger_on_mono` is `None` at construction).
We add a public slot wrapper so QML can read it.

- [ ] **Step 1: Write the failing test**

This test constructs the connector and asserts the new public slot delegates to the existing
elapsed-time computation. We avoid touching hardware by only calling the pure string method.

```python
# tests/test_scan_elapsed_slot.py
"""Unit test for the QML-facing scanElapsedStr slot (no hardware)."""
from motion_connector import MotionConnector


def test_scan_elapsed_str_slot_delegates(monkeypatch):
    # Build the object without running __init__ (avoids hardware/Qt setup).
    conn = MotionConnector.__new__(MotionConnector)
    monkeypatch.setattr(conn, "_scan_elapsed_str", lambda: "01:02:03")
    assert conn.scanElapsedStr() == "01:02:03"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_scan_elapsed_slot.py -v`
Expected: FAIL with `AttributeError: 'MotionConnector' object has no attribute 'scanElapsedStr'`

- [ ] **Step 3: Add the slot**

Add immediately above the `_scan_elapsed_str` definition (around line 2880 in
`motion_connector.py`):

```python
    @pyqtSlot(result=str)
    def scanElapsedStr(self) -> str:
        """QML-facing accessor for trigger-ON elapsed time (HH:MM:SS)."""
        return self._scan_elapsed_str()
```

`pyqtSlot` is already imported in this file (used by `notify` and many others) — no new import.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_scan_elapsed_slot.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add motion_connector.py tests/test_scan_elapsed_slot.py
git commit -m "feat: expose scan elapsed time to QML via scanElapsedStr slot"
```

---

### Task 2: Add `openWithTimestamp()` to NotesModal

**Files:**
- Modify: `components/NotesModal.qml` (add a function next to existing `open()` / `close()` at lines 17-26)

No automated test — QML behavior is verified by the manual run in Task 4. This task is a
self-contained, committable change.

- [ ] **Step 1: Add the function**

In `components/NotesModal.qml`, directly after the existing `open()` function (line 21) and
before `close()`, add:

```qml
    // Open with a timestamped entry pre-inserted. `stamp` is the bracketed
    // contents (e.g. "00:04:32 / 14:32:05"). Existing notes get a newline
    // before the new entry; an empty note gets no leading blank line. Cursor
    // is parked after the timestamp, ready for the operator to type.
    function openWithTimestamp(stamp) {
        var existing = MotionInterface.scanNotes
        var prefix = existing.length > 0 ? existing.replace(/\s+$/, "") + "\n" : ""
        notesArea.text = prefix + "[" + stamp + "] "
        root.visible = true
        notesArea.forceActiveFocus()
        notesArea.cursorPosition = notesArea.text.length
    }
```

`close()` is unchanged — it already does `MOTIONInterface.scanNotes = notesArea.text`, so the
inserted entry persists on close exactly like a manually typed note.

- [ ] **Step 2: Verify the file parses (syntax sanity)**

Run: `python -c "import sys; src=open('components/NotesModal.qml',encoding='utf-8').read(); assert src.count('{')==src.count('}'), 'brace mismatch'; print('braces balanced')"`
Expected: `braces balanced`
(Full QML validation happens when the app loads in Task 4.)

- [ ] **Step 3: Commit**

```bash
git add components/NotesModal.qml
git commit -m "feat: add openWithTimestamp entry point to NotesModal"
```

---

### Task 3: Wire the Spacebar shortcut in BloodFlow.qml

**Files:**
- Modify: `pages/BloodFlow.qml` (add a `Shortcut` next to the `NotesModal { id: notesModal }` block at lines 311-313)

- [ ] **Step 1: Add the Shortcut**

In `pages/BloodFlow.qml`, immediately after the closing brace of the `NotesModal { id: notesModal }`
block (after line 313), add:

```qml
    // Spacebar during an active scan pops the Notes modal with a fresh
    // newline + [elapsed / wall-clock] timestamp, cursor ready to type.
    // Gated so it only fires mid-scan and never over another modal; once
    // NotesModal opens, modalManager.current is non-null so the shortcut
    // disables itself and Space types a literal space in the textarea.
    Shortcut {
        sequence: "Space"
        enabled: bloodFlow.scanning
                 && MOTIONInterface.triggerState === "ON"
                 && modalManager.current === null
        onActivated: {
            var elapsed = MotionInterface.scanElapsedStr()
            var wall = Qt.formatTime(new Date(), "HH:mm:ss")
            notesModal.openWithTimestamp(elapsed + " / " + wall)
        }
    }
```

`modalManager` is the `ModalManager` declared in this file (lines ~229); `notesModal` is the
`NotesModal` above; `bloodFlow` is the root id; `MotionInterface` is the registered singleton.
All are already in scope here.

- [ ] **Step 2: Verify the file parses (syntax sanity)**

Run: `python -c "import sys; src=open('pages/BloodFlow.qml',encoding='utf-8').read(); assert src.count('{')==src.count('}'), 'brace mismatch'; print('braces balanced')"`
Expected: `braces balanced`

- [ ] **Step 3: Commit**

```bash
git add pages/BloodFlow.qml
git commit -m "feat: spacebar opens timestamped scan note during a scan"
```

---

### Task 4: Manual verification (real run + screenshot)

**Files:** none (verification only). QML does not hot-reload, so the app must be launched fresh.

This is the real test of the feature — QML interaction has no unit-test harness in this repo,
and per project convention layout/behavior changes need a real run + screenshot.

- [ ] **Step 1: Enable mock hardware**

In `config/app_config.json`, set `cameraFakeData: true` (note the current value first so you can
restore it). Do NOT commit this change.

- [ ] **Step 2: Launch the app**

Run: `python main.py`
Expected: app window opens on the Blood Flow page, no QML errors in the console (watch for
`ReferenceError` / `Cannot assign` lines, which would mean a wiring typo from Task 3).

- [ ] **Step 3: Start a scan and trigger the shortcut**

Start a scan via the Start button. Once the trigger is ON (countdown running), press Spacebar.
Expected: the Notes modal opens with a line like `[00:00:07 / 14:32:05] ` inserted on its own
line and the cursor positioned right after it.

- [ ] **Step 4: Screenshot to confirm**

Capture the app window (PrintWindow flag 2) and visually confirm the timestamped line is present
and well-formed. (Per memory `feedback_qml_changes_need_visual_check`, a boot-log grep is not
sufficient for geometry/behavior.)

- [ ] **Step 5: Confirm re-trigger and persistence**

Type a note. Press Spacebar again *inside the modal* — expected: a literal space is inserted, NOT
a new timestamp. Close the modal, reopen via the Notes icon — expected: the note persisted.
Confirm Spacebar does nothing note-related when not scanning.

- [ ] **Step 6: Watch for the focus-conflict risk**

If pressing Space while scanning re-activates the Start/Stop button (or does nothing) instead of
opening the modal, that is the keyboard-focus routing risk noted in the spec. If observed, stop
and report — the fix is clearing button focus on scan start, or falling back to spec approach B
(a `QApplication` event filter). If the modal opens correctly, no action needed.

- [ ] **Step 7: Restore config**

Revert `cameraFakeData` in `config/app_config.json` to its original value. Confirm
`git status` shows no changes to `config/app_config.json`.
