# Session ID Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-scan session-ID regeneration with a persistent `userLabel` (editable in Scan Settings) and an internal per-scan `sessionId = {timestamp}_{userLabel}`. Notes are 1:1 with `sessionId`.

**Architecture:** `MotionConnector` exposes only `userLabel` to QML (read/write). `sessionId` is an internal Python composite computed at scan-start and used for file naming. The Scan Settings modal gains a User Label field (research-mode only — reduced mode already hides the Scan Settings button). `newSession()` is removed; the notes-clear side effect moves into the scan-start code path.

**Tech Stack:** PyQt6, QML (Qt Quick Controls 2), Python 3.12.

---

## Scope note

The repo's canonical code lives at the **root**: `components/`, `pages/`, `motion_connector.py`, `main.qml`. This is what runs in dev (`python main.py`) and what's tracked in git.

There is an unrelated `src/openmotion_bloodflow/` subtree on disk that is **entirely untracked** by git (briefcase/packaging scaffold). Do NOT edit it, do NOT `git add` it. All edits in this plan apply to the root copy only.

## File Map

**Modified:**
- `motion_connector.py`: rename state, consolidate properties, move notes-reset into scan-start, delete `newSession()`.
- `components/ScanSettingsModal.qml`: add User Label field.
- `pages/BloodFlow.qml`: remove `newSession()` call; rebind `ScanRunner.subjectId` to `userLabel`.
- `main.qml` / `components/WindowMenu.qml`: no functional change. Header binding continues to use `sessionId:` prop name on both `WindowMenu` and `BloodFlow`; only the source on `BloodFlow.qml` changes to read `MOTIONInterface.userLabel`.

**Deleted:**
- `components/SessionModal.qml`
- `components/UserSettingsModal.qml`

---

## Task 1: Connector — rename internal state and consolidate to `userLabel`

**Files:**
- Modify: `motion_connector.py`

- [ ] **Step 1: Rename the instance attribute and the generator method**

In `motion_connector.py`:

At line ~269 (the `__init__` block), change:
```python
        self._subject_id = self.generate_session_id()
        logger.info(f"[Connector] Generated session ID: {self._subject_id}")
```
to:
```python
        self._user_label = self.generate_user_label()
        logger.info(f"[Connector] Generated user label: {self._user_label}")
```

At line ~1118, change:
```python
    def generate_session_id(self):
        suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        return f"ow{suffix}"

    def generate_subject_id(self):  # deprecated alias
        return self.generate_session_id()
```
to:
```python
    def generate_user_label(self):
        suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        return f"ow{suffix}"
```

- [ ] **Step 2: Replace the `sessionId` + `subjectId` property pair with a single `userLabel` property**

At line ~84–85, change the signal declarations:
```python
    sessionIdChanged = pyqtSignal()  # Signal to notify QML of session ID changes
    subjectIdChanged = pyqtSignal()  # Deprecated alias — same as sessionIdChanged
```
to:
```python
    userLabelChanged = pyqtSignal()  # Signal to notify QML of user label changes
```

At lines ~593–625, replace the entire `getSessionId` / `setSessionId` / `sessionId` / `getSubjectId` / `setSubjectId` / `subjectId` block with:
```python
    # --- GETTERS/SETTERS FOR Qt PROPERTIES ---
    def getUserLabel(self) -> str:
        return self._user_label

    def setUserLabel(self, value: str):
        if not value:
            return
        # normalize to "ow" + alphanumerics (uppercase)
        if value.startswith("ow"):
            rest = value[2:]
        else:
            rest = value
        rest = "".join(ch for ch in rest.upper() if ch.isalnum())
        new_val = "ow" + rest
        if new_val != self._user_label:
            self._user_label = new_val
            self.userLabelChanged.emit()

    userLabel = pyqtProperty(
        str, fget=getUserLabel, fset=setUserLabel, notify=userLabelChanged
    )
```

- [ ] **Step 3: Update `_start_runlog` default-argument reference**

At line ~435, change:
```python
        base_subject = subject_id or self._subject_id or "unknown"
```
to:
```python
        base_subject = subject_id or self._user_label or "unknown"
```

- [ ] **Step 4: Delete `newSession()` and update `get_scan_details` return dict**

At lines ~1125–1134, delete the entire `newSession()` method:
```python
    @pyqtSlot()
    def newSession(self):
        """Generate a fresh session ID and clear notes for a new scan."""
        self._subject_id = self.generate_session_id()
        self._scan_notes = ""
        self._scan_notes_path = ""
        self.sessionIdChanged.emit()
        self.subjectIdChanged.emit()
        self.scanNotesChanged.emit()
        logger.info(f"New session started: {self._subject_id}")
```

At lines ~1003–1014 in `get_scan_details`, update the return dict. The parser variable `subject` holds the short label (the userLabel); the composite sessionId is `{ts}_{subject}`. Change:
```python
        return {
            "sessionId": subject,
            "subjectId": subject,   # deprecated alias kept for compatibility
            "timestamp": ts,
            "leftMask": left_mask,
            "rightMask": right_mask,
            "leftPath": str(left) if left else "",
            "rightPath": str(right) if right else "",
            "correctedPath": str(corrected) if corrected else "",
            "notesPath": str(notes_path),
            "notes": notes,
        }
```
to:
```python
        return {
            "userLabel": subject,
            "sessionId": f"{ts}_{subject}",
            "timestamp": ts,
            "leftMask": left_mask,
            "rightMask": right_mask,
            "leftPath": str(left) if left else "",
            "rightPath": str(right) if right else "",
            "correctedPath": str(corrected) if corrected else "",
            "notesPath": str(notes_path),
            "notes": notes,
        }
```

- [ ] **Step 5: Move the notes-clear side effect into `startCapture`**

At line ~1185 (inside `startCapture`, just after `self._capture_stop = threading.Event()` and before `self._capture_start_time = time.time()`), add:
```python
        # New scan → clear notes buffer (formerly done by newSession)
        self._scan_notes = ""
        self._scan_notes_path = ""
        self.scanNotesChanged.emit()
```
Insert this just before the existing `self._capture_running = True` line.

- [ ] **Step 6: Grep for any remaining references**

Run in the project root:

```bash
grep -nE "_subject_id|\bsubjectId\b|sessionIdChanged|subjectIdChanged|generate_session_id|generate_subject_id|newSession" motion_connector.py
```

Expected: zero hits. (`subject_id` as a local-variable and function-parameter name in `startCapture` and `_start_runlog` stays — that's the SDK's parameter name; only the connector's own member state is renamed. If any hit remains, re-read the task and fix.)

- [ ] **Step 7: Smoke test — app launches, header shows label, scans work**

Run:
```bash
cd /c/Users/ethan/Projects/openmotion-bloodflow-app
python main.py
```
Expected: app launches without QML errors. Header shows `Session: owXXXXXX`. (We haven't wired the QML binding yet — if the header still reads via the old prop name on BloodFlow.qml, it will still work because the BloodFlow prop reads `MOTIONInterface.sessionId` — which is now gone. So this step will likely surface a QML binding warning. That's expected; Task 3 fixes it. Proceed.)

- [ ] **Step 8: Commit**

```bash
git add motion_connector.py
git commit -m "refactor(connector): consolidate sessionId/subjectId into userLabel"
```

---

## Task 2: Add User Label field to Scan Settings modal

**Files:**
- Modify: `components/ScanSettingsModal.qml`

- [ ] **Step 1: Add a "Session" section at the top of the ColumnLayout**

In `components/ScanSettingsModal.qml`, locate the `ColumnLayout` inside the dialog `Rectangle` (around line 129). Immediately after the "Scan Settings" title Text (line ~135–141) and before the Camera Configuration section (the divider Rectangle at line ~144), insert:

```qml
            // ── Session ──────────────────────────────────────────────────
            Rectangle { Layout.fillWidth: true; height: 1; color: theme.borderSubtle }

            Text {
                text: "Session"
                color: theme.textSecondary
                font.pixelSize: 15
                font.weight: Font.DemiBold
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 10

                Text {
                    text: "User Label:"
                    color: theme.textSecondary
                    font.pixelSize: 14
                    Layout.alignment: Qt.AlignVCenter
                }

                TextField {
                    id: userLabelField
                    Layout.fillWidth: true
                    Layout.preferredHeight: 30
                    text: MOTIONInterface.userLabel
                    font.pixelSize: 14
                    color: theme.textPrimary
                    selectByMouse: true
                    background: Rectangle {
                        color: theme.bgInput; radius: 4
                        border.color: userLabelField.activeFocus ? theme.accentBlue : theme.borderSubtle
                        border.width: 1
                    }
                    onEditingFinished: {
                        if (text !== MOTIONInterface.userLabel) {
                            MOTIONInterface.userLabel = text
                            text = MOTIONInterface.userLabel  // reflect normalization
                        }
                    }
                }
            }
```

Note: `editingFinished` fires on focus-loss or Enter. We re-read `MOTIONInterface.userLabel` after assignment because the connector setter normalizes the value (e.g. uppercases, strips invalid chars), and the TextField should reflect what was actually stored.

- [ ] **Step 2: Ensure the field refreshes when the modal opens**

Find the `open()` function at line ~80. Change:
```qml
    function open() { root.visible = true }
```
to:
```qml
    function open() {
        userLabelField.text = MOTIONInterface.userLabel
        root.visible = true
    }
```

- [ ] **Step 3: Smoke test — open Scan Settings, edit label, reopen**

Run `python main.py`. Click the Scan Settings button (left panel). Expected: the dialog shows a "Session" section with a "User Label:" field pre-filled with the current `owXXXXXX`. Edit the value to e.g. `owtest123` and tab/click away, then close and reopen the modal. Expected: the field shows `owTEST123` (uppercased, normalized). The window-header "Session:" label does **not** yet update — that's fixed in Task 3.

- [ ] **Step 4: Commit**

```bash
git add components/ScanSettingsModal.qml
git commit -m "feat(scan-settings): add editable User Label field"
```

---

## Task 3: Rewire QML bindings off of `userLabel`

**Files:**
- Modify: `pages/BloodFlow.qml`
- Modify: `components/HistoryModal.qml`

- [ ] **Step 1: Change the `sessionId` property on BloodFlow.qml to read `userLabel`**

In `pages/BloodFlow.qml`, at line ~31:

Change:
```qml
    // Session ID (exposed for header bar)
    property string sessionId: MOTIONInterface.sessionId || ""
```
to:
```qml
    // User label (exposed for header bar — bound name kept as `sessionId` for now to avoid touching main.qml/WindowMenu props)
    property string sessionId: MOTIONInterface.userLabel || ""
```

The BloodFlow-level QML property stays named `sessionId` because `main.qml` and `WindowMenu.qml` bind against `sessionId:` — renaming the header-facing prop is out of scope. The header label reads the short userLabel, which matches the spec.

- [ ] **Step 2: Remove the `newSession()` call at scan-start**

At line ~122–123, change:
```qml
            } else {
                MOTIONInterface.newSession()
                bloodFlow.scanning = true
```
to:
```qml
            } else {
                bloodFlow.scanning = true
```

- [ ] **Step 3: Rebind `ScanRunner.subjectId` to `userLabel`**

At line ~254, change:
```qml
        subjectId: MOTIONInterface.sessionId
```
to:
```qml
        subjectId: MOTIONInterface.userLabel
```

(`ScanRunner` passes this as `subject_id` into `startCapture`, which becomes the label component of the composite sessionId inside the connector. Per-scan timestamp is captured separately inside `startCapture`.)

- [ ] **Step 3b: Update HistoryModal detail panel to use `userLabel`**

Task 1 changed `get_scan_details` so the `sessionId` key is now the composite `{timestamp}_{userLabel}` and a new `userLabel` key holds just the short label. The detail panel in `components/HistoryModal.qml` line ~245–246 shows a "Session ID:" row alongside a separate "Date:" row; with the new semantics that row would display the composite string which duplicates the date.

In `components/HistoryModal.qml` at line ~245:

Change:
```qml
                            Text { text: "Session ID:"; color: theme.textSecondary; font.pixelSize: 13 }
                            Text { text: selected.sessionId || "-"; color: theme.textPrimary; font.pixelSize: 13 }
```
to:
```qml
                            Text { text: "User Label:"; color: theme.textSecondary; font.pixelSize: 13 }
                            Text { text: selected.userLabel || "-"; color: theme.textPrimary; font.pixelSize: 13 }
```

- [ ] **Step 4: Smoke test — header updates live on label edit**

Run `python main.py`. Open Scan Settings. Edit User Label to e.g. `owtest`. Close. Expected: the window header now reads `Session: owTEST`. Run a short scan (use a short duration or just let it trigger for a few seconds and cancel). Expected: produced files are named `{YYYYMMDD_HHMMSS}_owTEST_*`. Start a second scan without touching settings. Expected: files use the same `owTEST` prefix but a newer timestamp.

- [ ] **Step 5: Grep for any stale refs**

```bash
grep -rnE "MOTIONInterface\.(sessionId|subjectId)|\.newSession\(" pages/ components/ main.qml
```
Expected: zero hits.

- [ ] **Step 6: Commit**

```bash
git add pages/BloodFlow.qml
git commit -m "feat(bloodflow): bind header + ScanRunner to userLabel, drop newSession at scan-start"
```

---

## Task 4: Delete unreferenced modals

**Files:**
- Delete: `components/SessionModal.qml`
- Delete: `components/UserSettingsModal.qml`

- [ ] **Step 1: Verify no references anywhere in repo**

```bash
grep -rnE "SessionModal|UserSettingsModal" --include="*.qml" --include="*.py" --include="*.json" --include="qmldir"
```
Expected: zero hits (design phase already verified this; re-check to catch anything added since).

- [ ] **Step 2: Delete the two files**

```bash
git rm components/SessionModal.qml components/UserSettingsModal.qml
```

- [ ] **Step 3: Smoke test — app still launches and runs a scan**

Run `python main.py`. Expected: no QML import errors, all modals open cleanly, a scan runs end-to-end.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore: remove unreferenced SessionModal and UserSettingsModal"
```

---

## Task 5: Full acceptance pass

- [ ] **Step 1: Research-mode acceptance**

Run `python main.py`. Verify each item in the spec's acceptance checklist:

- [ ] Startup generates a `userLabel`; header shows `Session: owXXXXXX`.
- [ ] Scan Settings modal has the "User Label:" field; editing persists across scans. Change it, run two scans — both get the new label with different timestamps.
- [ ] Header updates live when label is edited.
- [ ] Files produced are `{YYYYMMDD_HHMMSS}_{userLabel}_*`.
- [ ] Notes cleared at scan-start (open Notes modal during/between scans, confirm fresh scan starts blank).
- [ ] Notes saved at scan-end to that scan's `_notes.txt`; re-editing post-scan and closing Notes modal overwrites the same file.

- [ ] **Step 2: Reduced-mode acceptance**

Edit `config/app_config.json` and set `"reducedMode": true`. Relaunch. Verify:

- [ ] Scan Settings button is hidden in the left panel (existing behavior).
- [ ] Header shows auto-generated `userLabel`.
- [ ] A scan produces files with the auto-generated label.
- [ ] No UI path exists to edit the label.

Revert `reducedMode` to `false` afterward.

- [ ] **Step 3: History-view check**

Open the History modal. Verify each historical scan shows both its timestamp and userLabel (existing columns — no change expected, but confirm nothing broke).

- [ ] **Step 4: Final grep for leftover names**

```bash
grep -nE "_subject_id|\bsubjectId\b|sessionIdChanged|subjectIdChanged|generate_session_id|generate_subject_id|newSession" motion_connector.py
grep -rnE "MOTIONInterface\.(sessionId|subjectId)|\.newSession\(|SessionModal|UserSettingsModal" pages/ components/ main.qml
```
Expected: zero hits.

- [ ] **Step 5: Final commit (if any fixes were made during acceptance)**

If acceptance surfaced issues and you made fixes:
```bash
git add -A
git commit -m "fix: address issues found during session ID redesign acceptance"
```

Otherwise skip.

---

## Self-review notes

**Spec coverage:** Each acceptance item in the spec maps to a task — Task 1 (connector model), Task 2 (UI editor), Task 3 (QML rebinding + scan-start behavior change), Task 4 (cleanup), Task 5 (verification).

**Parameter names left intact:** `ScanRequest.subject_id` (SDK type) and the local variable `subject_id` inside `startCapture` remain — they're the SDK's parameter name and outside the scope of this plan. The connector now passes `self._user_label` as that argument. Filename format remains `{scan_timestamp}_{subject_id}_*.csv`, which is exactly the new composite sessionId.

**Edge case covered:** Notes written before any scan live in `_scan_notes` in memory. When a scan starts, the buffer is cleared (Task 1, Step 5), so those pre-scan notes are discarded — this is the documented trade-off in the spec.

**Edge case covered:** Notes written in the gap between scan #1 end and scan #2 start save to scan #1's file via the existing `scanNotes` setter write-back behavior; Task 1 Step 5 clears the buffer only when scan #2 actually starts, not when scan #1 finishes.
