# History Data-Management Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rework the History modal into a sortable, searchable, multi-select table of scans driven entirely by the scan DB, with Load / Export / password-gated Delete.

**Architecture:** New DB-only connector slots (`get_scan_sessions`, `get_session_stats`, `deleteScans`) read the `sessions` table and the `session_meta` JSON the SDK already writes; the camera-config name mapping is computed in Python so it's unit-testable. `HistoryModal.qml` is rebuilt around a header + `ListView` table with multiselect checkboxes, a read-only detail pane, and actions; Delete reuses the existing `PasswordPromptModal` (developer password). No SDK changes.

**Tech Stack:** Python 3.13, PyQt6, QML (QtQuick 6), pytest, SQLite (omotion `ScanDatabase`).

**Spec:** [docs/superpowers/specs/2026-06-15-history-data-management-design.md](../specs/2026-06-15-history-data-management-design.md)

---

## File Structure

- **Modify** `motion_connector.py` — add module-level `_CONFIG_NAMES` + `_config_name()`, and three `@pyqtSlot`s + one private `_session_to_row()` helper, inserted right after `get_scan_details` (ends ~line 1559, before the `directory` property at ~1561). The existing `get_scan_list` / `get_scan_details` stay (still used by `loadPastScan` and `test_scan_history_list.py`).
- **Create** `tests/test_history_sessions.py` — unit tests (no hardware) for the three new slots + the config-name helper.
- **Rewrite** `components/HistoryModal.qml` — table UI. Same root contract (`label`, `open()`, `close()`, `visible`, busy overlay, `MotionInterface` `Connections`) so `ModalManager` and `BloodFlow.qml` need no changes.
- **Modify** `tests/test_history.py` — HIL (`@pytest.mark.dev`) UI test; update for the new table (no ComboBox; "Load in viewer →" button). **Cannot be validated in this environment — requires the self-hosted hardware runner.**

---

## Task 1: Connector — `get_scan_sessions()` + config-name helper

**Files:**
- Modify: `motion_connector.py` (module-level helper near other module helpers ~line 75; `_session_to_row` + `get_scan_sessions` after `get_scan_details` ~line 1559)
- Test: `tests/test_history_sessions.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_history_sessions.py`:

```python
"""History data-management connector slots — DB-only, no hardware."""

from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector, _config_name
from omotion.ScanDatabase import ScanDatabase

pytestmark = pytest.mark.unit


def _connector(tmp_path, scan_db_path):
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = scan_db_path
    return MotionConnector(
        interface=iface, app_config={"developerMode": False},
        data_dir=str(tmp_path), config_dir="config",
    )


def _make_session(db_path, label, start, end, left_mask, right_mask,
                  reduced=True, notes=None, subject=None, operator="ethan"):
    # Default the subject_id to the label's trailing user-label segment so
    # rows carry distinct userLabels (matches how scans are really named).
    if subject is None:
        parts = label.split("_", 2)
        subject = parts[2] if len(parts) > 2 else ""
    db = ScanDatabase(db_path=db_path)
    sid = db.create_session(
        session_label=label, session_start=start, session_end=end,
        session_notes=notes,
        session_meta={
            "scan_id": label.rsplit("_", 1)[0], "subject_id": subject,
            "operator": operator, "duration_sec": (end - start) if end else 0,
            "sdk_flags": {
                "reduced_mode": reduced,
                "left_camera_mask": left_mask,
                "right_camera_mask": right_mask,
            },
        },
    )
    db.close()
    return sid


def test_config_name_known_and_unknown():
    assert _config_name(0x5A) == "Near"
    assert _config_name(0xC3) == "Far"
    assert _config_name(0x00) == "None"
    assert _config_name(0x12) == "0x12"   # unmapped -> hex
    assert _config_name(-1) == "—"        # unknown


def test_get_scan_sessions_rows_and_sort(tmp_path):
    db_path = str(tmp_path / "scans.db")
    _make_session(db_path, "20260612_092000_subjA", 100.0, 105.0, 0xC3, 0xC3)
    _make_session(db_path, "20260612_093100_subjB", 200.0, 215.0, 0x5A, 0x66)
    c = _connector(tmp_path, db_path)

    rows = c.get_scan_sessions()
    assert [r["userLabel"] for r in rows] == ["subjB", "subjA"]  # newest first
    top = rows[0]
    assert top["configL"] == "Near" and top["configR"] == "Middle"
    assert top["durationSec"] == 15.0
    assert top["leftMask"] == 0x5A and top["rightMask"] == 0x66
    assert top["interrupted"] is False
    assert top["dateTime"] == "2026-06-12 09:31:00"


def test_get_scan_sessions_interrupted_open_session(tmp_path):
    db_path = str(tmp_path / "scans.db")
    _make_session(db_path, "20260612_100000_subjC", 300.0, None, 0xFF, 0xFF)
    c = _connector(tmp_path, db_path)
    row = c.get_scan_sessions()[0]
    assert row["interrupted"] is True
    assert row["durationSec"] == -1.0


def test_get_scan_sessions_empty_without_db(tmp_path):
    c = _connector(tmp_path, None)
    assert c.get_scan_sessions() == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_history_sessions.py -v`
Expected: FAIL — `ImportError: cannot import name '_config_name'` / `AttributeError: ... 'get_scan_sessions'`.

- [ ] **Step 3: Add the module-level config-name helper**

In `motion_connector.py`, just after the `developer_password_matches` function (~line 90, module scope — NOT inside the class), add:

```python
# Camera-mask → human config name, mirroring CameraSelectionModal's
# pattern table. Unmapped masks render as hex; -1 (unknown, e.g. a
# reduced-mode scan whose meta lacks sdk_flags) renders as an em dash.
_CONFIG_NAMES = {
    0x00: "None", 0x5A: "Near", 0x66: "Middle", 0xC3: "Far",
    0x99: "Outer", 0x0F: "Left", 0xF0: "Right", 0x42: "Third Row",
    0xFF: "All",
}


def _config_name(mask) -> str:
    if mask is None or mask < 0:
        return "—"
    m = int(mask) & 0xFF
    return _CONFIG_NAMES.get(m, f"0x{m:02X}")
```

- [ ] **Step 4: Add `_session_to_row` + `get_scan_sessions`**

In `motion_connector.py`, immediately after `get_scan_details` returns (~line 1559, before `@pyqtProperty(str, notify=directoryChanged) def directory`), add:

```python
    @staticmethod
    def _friendly_ts(ts: str) -> str:
        """'YYYYMMDD_HHMMSS' -> 'YYYY-MM-DD HH:MM:SS'; pass through otherwise."""
        if not ts or len(ts) != 15:
            return ts or "-"
        return (f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} "
                f"{ts[9:11]}:{ts[11:13]}:{ts[13:15]}")

    def _session_to_row(self, s: dict) -> dict:
        """Flatten one ScanDatabase session dict into the QVariantMap the
        History table consumes. Masks/operator come from session_meta
        (written by ScanDBSink); duration from session_start/end."""
        label = (s.get("session_label") or "").strip()
        meta = s.get("session_meta")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        flags = meta.get("sdk_flags") or {}

        left_mask = flags.get("left_camera_mask", -1)
        right_mask = flags.get("right_camera_mask", -1)
        if left_mask is None:
            left_mask = -1
        if right_mask is None:
            right_mask = -1

        user_label = meta.get("subject_id") or ""
        timestamp = ""
        m = re.match(r"^(\d{8}_\d{6})_?(.*)$", label)
        if m:
            timestamp = m.group(1)
            if not user_label:
                user_label = m.group(2)

        start = s.get("session_start")
        end = s.get("session_end")
        if start is not None and end is not None:
            duration = float(end) - float(start)
        else:
            duration = -1.0

        return {
            "sessionId": int(s.get("id")),
            "label": label,
            "userLabel": user_label,
            "operator": meta.get("operator") or "",
            "timestamp": timestamp or label,
            "dateTime": self._friendly_ts(timestamp),
            "durationSec": duration,
            "leftMask": int(left_mask),
            "rightMask": int(right_mask),
            "configL": _config_name(left_mask),
            "configR": _config_name(right_mask),
            "reducedMode": bool(flags.get("reduced_mode", False)),
            "notes": s.get("session_notes") or "",
            "interrupted": end is None,
        }

    @pyqtSlot(result="QVariantList")
    def get_scan_sessions(self):
        """Return one row per scan-DB session, newest first, for the
        History table. DB-only — does not list CSV-derived scans.
        Best-effort: a missing/unreadable DB yields []."""
        db_path = getattr(self._interface, "scan_db_path", None)
        if not db_path:
            return []
        rows = []
        try:
            from omotion.ScanDatabase import ScanDatabase
            db = ScanDatabase(db_path)
            try:
                for s in db.iter_sessions():
                    rows.append(self._session_to_row(s))
            finally:
                db.close()
        except Exception:
            logger.warning("get_scan_sessions: could not read scan DB",
                           exc_info=True)
            return []
        rows.sort(key=lambda r: r["timestamp"], reverse=True)
        return rows
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_history_sessions.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add motion_connector.py tests/test_history_sessions.py
git commit -m "feat: add get_scan_sessions connector slot for History table"
```

---

## Task 2: Connector — `get_session_stats()` + `deleteScans()`

**Files:**
- Modify: `motion_connector.py` (directly after `get_scan_sessions`)
- Test: `tests/test_history_sessions.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_history_sessions.py`:

```python
def _insert_rows(db_path, session_id, n):
    db = ScanDatabase(db_path=db_path)
    for i in range(n):
        db.insert_session_data(
            session_id=session_id, cam_id=0, side=0,
            timestamp_s=float(i), frame_id=i, bfi=1.0, bvi=2.0,
        )
    db.close()


def test_get_session_stats_counts_rows(tmp_path):
    db_path = str(tmp_path / "scans.db")
    sid = _make_session(db_path, "20260612_092000_subjA", 100.0, 105.0, 0xC3, 0xC3)
    _insert_rows(db_path, sid, 7)
    c = _connector(tmp_path, db_path)
    assert c.get_session_stats(sid)["sampleCount"] == 7


def test_delete_scans_removes_session_and_cascades(tmp_path):
    db_path = str(tmp_path / "scans.db")
    keep = _make_session(db_path, "20260612_092000_keep", 100.0, 105.0, 0xC3, 0xC3)
    drop = _make_session(db_path, "20260612_093000_drop", 200.0, 205.0, 0x5A, 0x5A)
    _insert_rows(db_path, drop, 5)
    c = _connector(tmp_path, db_path)

    removed = c.deleteScans([drop])
    assert removed == 1
    remaining = [r["sessionId"] for r in c.get_scan_sessions()]
    assert remaining == [keep]
    # session_data cascade-deleted with the session
    assert c.get_session_stats(drop)["sampleCount"] == 0


def test_delete_scans_empty_list_is_noop(tmp_path):
    db_path = str(tmp_path / "scans.db")
    _make_session(db_path, "20260612_092000_keep", 100.0, 105.0, 0xC3, 0xC3)
    c = _connector(tmp_path, db_path)
    assert c.deleteScans([]) == 0
    assert len(c.get_scan_sessions()) == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_history_sessions.py -k "stats or delete" -v`
Expected: FAIL — `AttributeError: ... 'get_session_stats'` / `'deleteScans'`.

- [ ] **Step 3: Add the two slots**

In `motion_connector.py`, immediately after `get_scan_sessions`, add:

```python
    @pyqtSlot(int, result="QVariantMap")
    def get_session_stats(self, session_id: int):
        """Lazily fetch heavier per-scan stats (row count) when a History
        row is focused, so the list query itself stays cheap."""
        db_path = getattr(self._interface, "scan_db_path", None)
        if not db_path:
            return {"sampleCount": 0}
        try:
            from omotion.ScanDatabase import ScanDatabase
            db = ScanDatabase(db_path)
            try:
                row = next(
                    db._connection().execute(
                        "SELECT COUNT(*) FROM session_data WHERE session_id = ?",
                        (int(session_id),),
                    ),
                    None,
                )
                return {"sampleCount": int(row[0]) if row else 0}
            finally:
                db.close()
        except Exception:
            logger.warning("get_session_stats failed for %s", session_id,
                           exc_info=True)
            return {"sampleCount": 0}

    @pyqtSlot("QVariantList", result=int)
    def deleteScans(self, session_ids):
        """Delete the given scan-DB sessions (CASCADE removes their
        session_data). Returns the count actually deleted. The developer-
        password gate is enforced in QML before this is called."""
        db_path = getattr(self._interface, "scan_db_path", None)
        if not db_path:
            return 0
        deleted = 0
        try:
            from omotion.ScanDatabase import ScanDatabase
            db = ScanDatabase(db_path)
            try:
                for sid in session_ids:
                    try:
                        if db.delete_session(int(sid)):
                            deleted += 1
                            logger.info("deleteScans: removed session %s", sid)
                    except Exception:
                        logger.warning("deleteScans: failed to delete %s", sid,
                                       exc_info=True)
            finally:
                db.close()
        except Exception:
            logger.warning("deleteScans: could not open scan DB", exc_info=True)
        return deleted
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_history_sessions.py -v`
Expected: PASS (7 tests total).

- [ ] **Step 5: Commit**

```bash
git add motion_connector.py tests/test_history_sessions.py
git commit -m "feat: add get_session_stats and deleteScans connector slots"
```

---

## Task 3: Rewrite `HistoryModal.qml` as a table

QML can't be unit-tested; verify by launching the app against a seeded DB and screenshotting (per the project's "QML changes need a visual check" rule). The root contract (`label`, `open()`, `close()`, busy overlay, `Connections`) is preserved so `BloodFlow.qml` / `ModalManager` are untouched.

**Files:**
- Rewrite: `components/HistoryModal.qml`

- [ ] **Step 1: Replace the file contents**

Overwrite `components/HistoryModal.qml` with:

```qml
import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import QtQuick.Dialogs as Dialogs
import OpenMotion 1.0

Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 9998

    AppTheme { id: theme }

    // Modal interface label (single source of truth — see ModalManager).
    readonly property string label: "Scan History"

    // Full session list from the connector (newest first).
    property var scans: []
    // Filtered + sorted view actually rendered.
    property var view: []
    // Multiselect membership: { sessionId: true }. Reassigned (not mutated)
    // on every toggle so delegate bindings re-evaluate.
    property var checked: ({})
    property int checkedCount: 0
    // Focused (detail-pane) row.
    property int focusedSessionId: -1
    property var focusedRow: null
    property int focusedSampleCount: -1
    // Toolbar filters / sort.
    property string searchText: ""
    property string configFilter: "All"
    property string sortKey: "timestamp"
    property bool sortAsc: false
    // Async "Load in viewer" busy state (issue #152 pattern).
    property bool loadingPlot: false

    // ── data plumbing ──────────────────────────────────────────────
    function open() {
        refresh()
        console.warn("[History] opened — " + scans.length + " scan(s)")
        root.visible = true
    }
    function close() { root.visible = false }

    function refresh() {
        try { scans = MotionInterface.get_scan_sessions() || [] }
        catch (e) { scans = [] }
        checked = ({}); checkedCount = 0
        rebuildView()
        if (view.length > 0) focusRow(view[0].sessionId)
        else { focusedSessionId = -1; focusedRow = null; focusedSampleCount = -1 }
    }

    function rebuildView() {
        var q = (searchText || "").toLowerCase()
        var cfg = configFilter
        var arr = scans.filter(function(r) {
            if (q.length > 0
                && (r.userLabel || "").toLowerCase().indexOf(q) < 0
                && (r.label || "").toLowerCase().indexOf(q) < 0)
                return false
            if (cfg !== "All" && r.configL !== cfg && r.configR !== cfg)
                return false
            return true
        })
        var key = sortKey, asc = sortAsc
        arr.sort(function(a, b) {
            var av = a[key], bv = b[key]
            if (av === undefined || av === null) av = ""
            if (bv === undefined || bv === null) bv = ""
            if (av < bv) return asc ? -1 : 1
            if (av > bv) return asc ? 1 : -1
            return 0
        })
        view = arr
    }

    function setSort(key) {
        if (sortKey === key) sortAsc = !sortAsc
        else { sortKey = key; sortAsc = (key === "userLabel") }
        rebuildView()
    }

    function focusRow(sid) {
        focusedSessionId = sid
        focusedRow = null
        for (var i = 0; i < view.length; i++)
            if (view[i].sessionId === sid) { focusedRow = view[i]; break }
        focusedSampleCount = -1
        if (sid >= 0) {
            try { focusedSampleCount = MotionInterface.get_session_stats(sid).sampleCount }
            catch (e) { focusedSampleCount = -1 }
        }
    }

    function toggleCheck(sid) {
        var c = Object.assign({}, checked)
        if (c[sid]) { delete c[sid]; checkedCount -= 1 }
        else { c[sid] = true; checkedCount += 1 }
        checked = c
    }

    function setAllChecked(on) {
        var c = {}, n = 0
        if (on) for (var i = 0; i < view.length; i++) { c[view[i].sessionId] = true; n++ }
        checked = c; checkedCount = n
    }

    function requestDelete() {
        if (checkedCount <= 0) return
        deletePrompt.open()
    }

    function doDelete() {
        var ids = []
        for (var k in checked) if (checked[k]) ids.push(parseInt(k))
        var removed = 0
        try { removed = MotionInterface.deleteScans(ids) } catch (e) {}
        console.warn("[History] deleted " + removed + " scan(s)")
        MotionInterface.notify(removed + " scan(s) deleted", "success")
        refresh()
    }

    function formatDuration(sec) {
        if (sec === undefined || sec === null || sec < 0) return "—"
        var m = Math.floor(sec / 60)
        var s = Math.round(sec % 60)
        return m + ":" + (s < 10 ? "0" + s : s)
    }

    // Re-filter whenever the search/config filter changes.
    onSearchTextChanged: rebuildView()
    onConfigFilterChanged: rebuildView()

    // ── backdrop ───────────────────────────────────────────────────
    Rectangle {
        anchors.fill: parent
        color: "#000000AA"
        MouseArea { anchors.fill: parent; onClicked: root.close() }
    }

    // Shared column widths (header + rows stay aligned).
    QtObject {
        id: col
        readonly property int chk: 34
        readonly property int date: 150
        readonly property int config: 160
        readonly property int dur: 70
        readonly property int status: 28
    }

    Rectangle {
        width: Math.min(parent.width - 60, 960)
        height: Math.min(parent.height - 60, 680)
        radius: 12
        color: theme.bgContainer
        border.color: theme.borderSubtle
        border.width: 2
        anchors.centerIn: parent

        // Absorb empty-space clicks so they don't reach the backdrop.
        MouseArea { anchors.fill: parent }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 18
            spacing: 12

            // ── toolbar ────────────────────────────────────────────
            RowLayout {
                Layout.fillWidth: true
                spacing: 10

                Text {
                    text: root.label
                    font.pixelSize: 20; font.weight: Font.Bold
                    color: theme.textPrimary
                }
                Item { Layout.preferredWidth: 8 }

                TextField {
                    id: searchField
                    Layout.preferredWidth: 200
                    Layout.preferredHeight: 32
                    placeholderText: "Search label…"
                    color: theme.textPrimary
                    placeholderTextColor: theme.textSecondary
                    font.pixelSize: 13
                    leftPadding: 10; rightPadding: 10
                    verticalAlignment: TextInput.AlignVCenter
                    background: Rectangle {
                        color: theme.bgInput; radius: 4
                        border.color: searchField.activeFocus ? theme.accentBlue : theme.borderSubtle
                        border.width: 1
                    }
                    onTextChanged: root.searchText = text
                }

                ComboBox {
                    id: configBox
                    Layout.preferredWidth: 130
                    Layout.preferredHeight: 32
                    model: ["All", "Near", "Middle", "Far", "Outer",
                            "Left", "Right", "Third Row", "None"]
                    font.pixelSize: 13
                    contentItem: Text {
                        leftPadding: 10; text: configBox.displayText
                        font: configBox.font; color: theme.textPrimary
                        verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight
                    }
                    background: Rectangle {
                        color: theme.bgInput; radius: 4
                        border.color: theme.borderSubtle; border.width: 1
                    }
                    onCurrentTextChanged: root.configFilter = currentText
                }

                Item { Layout.fillWidth: true }

                Button {
                    text: "Open Folder"
                    Layout.preferredWidth: 100; Layout.preferredHeight: 32
                    hoverEnabled: true
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13; color: theme.textSecondary
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? theme.accentBlue : theme.bgInput
                        border.color: parent.hovered ? theme.textPrimary : theme.textSecondary; radius: 4
                    }
                    onClicked: Qt.openUrlExternally("file:///" + MotionInterface.directory)
                }
                Button {
                    text: "Refresh"
                    Layout.preferredWidth: 80; Layout.preferredHeight: 32
                    hoverEnabled: true
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13; color: theme.textSecondary
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? theme.accentBlue : theme.bgInput
                        border.color: parent.hovered ? theme.textPrimary : theme.textSecondary; radius: 4
                    }
                    onClicked: root.refresh()
                }
                Rectangle {
                    width: 28; height: 28; radius: 14
                    color: xArea.containsMouse ? "#C0392B" : theme.borderStrong
                    border.color: theme.borderHover; border.width: 1
                    Behavior on color { ColorAnimation { duration: 120 } }
                    Text { anchors.centerIn: parent; text: "✕"; color: theme.textPrimary; font.pixelSize: 13 }
                    MouseArea {
                        id: xArea; anchors.fill: parent; hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor; onClicked: root.close()
                    }
                }
            }

            // ── table header ───────────────────────────────────────
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 30
                color: theme.bgCardAlt; radius: 4
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 8; anchors.rightMargin: 8
                    spacing: 8

                    // Select-all checkbox.
                    Text {
                        Layout.preferredWidth: col.chk
                        text: (root.checkedCount > 0 && root.checkedCount === root.view.length) ? "☑" : "☐"
                        color: theme.textSecondary; font.pixelSize: 15
                        horizontalAlignment: Text.AlignHCenter
                        MouseArea {
                            anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                            onClicked: root.setAllChecked(
                                !(root.checkedCount > 0 && root.checkedCount === root.view.length))
                        }
                    }
                    HistoryHeaderCell {
                        text: "User Label"; sortName: "userLabel"; Layout.fillWidth: true
                        activeSort: root.sortKey; ascending: root.sortAsc
                        onSortRequested: function(name) { root.setSort(name) }
                    }
                    HistoryHeaderCell {
                        text: "Date / Time"; sortName: "timestamp"; Layout.preferredWidth: col.date
                        activeSort: root.sortKey; ascending: root.sortAsc
                        onSortRequested: function(name) { root.setSort(name) }
                    }
                    HistoryHeaderCell {
                        text: "Config (L / R)"; sortName: "configL"; Layout.preferredWidth: col.config
                        activeSort: root.sortKey; ascending: root.sortAsc
                        onSortRequested: function(name) { root.setSort(name) }
                    }
                    HistoryHeaderCell {
                        text: "Duration"; sortName: "durationSec"; Layout.preferredWidth: col.dur
                        activeSort: root.sortKey; ascending: root.sortAsc
                        onSortRequested: function(name) { root.setSort(name) }
                    }
                    Item { Layout.preferredWidth: col.status }
                }
            }

            // ── table body ─────────────────────────────────────────
            Rectangle {
                Layout.fillWidth: true; Layout.fillHeight: true
                radius: 6; color: theme.bgCardAlt
                border.color: theme.borderSubtle; border.width: 1
                clip: true

                ListView {
                    id: listView
                    anchors.fill: parent
                    anchors.margins: 2
                    model: root.view
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: ScrollBar {}

                    delegate: Rectangle {
                        width: ListView.view.width
                        height: 34
                        color: modelData.sessionId === root.focusedSessionId
                               ? Qt.rgba(0.31, 0.55, 1.0, 0.16)
                               : (root.checked[modelData.sessionId]
                                  ? Qt.rgba(0.31, 0.55, 1.0, 0.07) : "transparent")

                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: root.focusRow(modelData.sessionId)
                        }

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 8; anchors.rightMargin: 8
                            spacing: 8

                            Text {
                                Layout.preferredWidth: col.chk
                                text: root.checked[modelData.sessionId] ? "☑" : "☐"
                                color: theme.textPrimary; font.pixelSize: 15
                                horizontalAlignment: Text.AlignHCenter
                                MouseArea {
                                    anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                                    onClicked: function(mouse) {
                                        root.toggleCheck(modelData.sessionId); mouse.accepted = true
                                    }
                                }
                            }
                            Text {
                                Layout.fillWidth: true
                                text: modelData.userLabel || "(unlabeled)"
                                color: theme.textPrimary; font.pixelSize: 13
                                elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter
                            }
                            Text {
                                Layout.preferredWidth: col.date
                                text: modelData.dateTime || "-"
                                color: theme.textSecondary; font.pixelSize: 13
                                verticalAlignment: Text.AlignVCenter
                            }
                            Text {
                                Layout.preferredWidth: col.config
                                text: (modelData.configL || "—") + " / " + (modelData.configR || "—")
                                color: theme.textSecondary; font.pixelSize: 13
                                elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter
                            }
                            Text {
                                Layout.preferredWidth: col.dur
                                text: root.formatDuration(modelData.durationSec)
                                color: theme.textSecondary; font.pixelSize: 13
                                verticalAlignment: Text.AlignVCenter
                            }
                            Text {
                                Layout.preferredWidth: col.status
                                text: modelData.interrupted ? "⚠" : "●"
                                color: modelData.interrupted ? "#E6A23C" : "#3EC97A"
                                font.pixelSize: 13
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
                        }
                    }

                    // Empty state.
                    Text {
                        anchors.centerIn: parent
                        visible: root.view.length === 0
                        text: root.scans.length === 0 ? "No scans yet."
                                                      : "No scans match the filter."
                        color: theme.textSecondary; font.pixelSize: 14
                    }
                }
            }

            // ── detail pane (focused row, read-only) ───────────────
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 150
                radius: 6; color: theme.bgCardAlt
                border.color: theme.borderSubtle; border.width: 1
                visible: root.focusedRow !== null

                RowLayout {
                    anchors.fill: parent; anchors.margins: 12; spacing: 16

                    GridLayout {
                        columns: 2; columnSpacing: 14; rowSpacing: 4
                        Layout.preferredWidth: 320
                        Layout.alignment: Qt.AlignTop
                        Text { text: "Full label:"; color: theme.textSecondary; font.pixelSize: 12 }
                        Text { text: root.focusedRow ? root.focusedRow.label : ""; color: theme.textPrimary; font.pixelSize: 12; elide: Text.ElideRight; Layout.fillWidth: true }
                        Text { text: "Operator:"; color: theme.textSecondary; font.pixelSize: 12 }
                        Text { text: root.focusedRow ? (root.focusedRow.operator || "-") : ""; color: theme.textPrimary; font.pixelSize: 12 }
                        Text { text: "Mask (L / R):"; color: theme.textSecondary; font.pixelSize: 12 }
                        Text {
                            color: theme.textPrimary; font.pixelSize: 12
                            text: root.focusedRow
                                  ? ("0x" + (root.focusedRow.leftMask & 0xFF).toString(16).toUpperCase()
                                     + " / 0x" + (root.focusedRow.rightMask & 0xFF).toString(16).toUpperCase())
                                  : ""
                        }
                        Text { text: "Config (L / R):"; color: theme.textSecondary; font.pixelSize: 12 }
                        Text { text: root.focusedRow ? (root.focusedRow.configL + " / " + root.focusedRow.configR) : ""; color: theme.textPrimary; font.pixelSize: 12 }
                        Text { text: "Samples:"; color: theme.textSecondary; font.pixelSize: 12 }
                        Text {
                            color: theme.textPrimary; font.pixelSize: 12
                            text: root.focusedSampleCount < 0 ? "…" : root.focusedSampleCount.toLocaleString()
                        }
                    }

                    ColumnLayout {
                        Layout.fillWidth: true; Layout.fillHeight: true; spacing: 4
                        Text { text: "Notes (read-only):"; color: theme.textSecondary; font.pixelSize: 12 }
                        Rectangle {
                            Layout.fillWidth: true; Layout.fillHeight: true
                            radius: 4; color: theme.bgInput
                            border.color: theme.borderSubtle; border.width: 1
                            ScrollView {
                                anchors.fill: parent; anchors.margins: 2
                                TextArea {
                                    readOnly: true; wrapMode: Text.Wrap; background: null
                                    text: root.focusedRow ? (root.focusedRow.notes || "") : ""
                                    color: theme.textPrimary; font.pixelSize: 12
                                }
                            }
                        }
                    }
                }
            }

            // ── actions ────────────────────────────────────────────
            RowLayout {
                Layout.fillWidth: true; spacing: 10

                Text {
                    text: root.checkedCount > 0 ? (root.checkedCount + " selected") : ""
                    color: theme.textSecondary; font.pixelSize: 12
                }
                Item { Layout.fillWidth: true }

                Button {
                    text: "Load in viewer →"
                    Layout.preferredWidth: 140; Layout.preferredHeight: 34
                    enabled: root.focusedRow !== null && !root.focusedRow.interrupted
                    hoverEnabled: enabled
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13
                        color: parent.enabled ? theme.textSecondary : theme.textTertiary
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: !parent.enabled ? theme.bgInput : parent.hovered ? theme.accentBlue : theme.bgInput
                        border.color: !parent.enabled ? theme.textTertiary : parent.hovered ? theme.textPrimary : theme.textSecondary; radius: 4
                    }
                    onClicked: {
                        root.loadingPlot = true
                        MotionInterface.loadPastScan(root.focusedRow.label)
                    }
                }
                Button {
                    text: "Export CSV"
                    Layout.preferredWidth: 110; Layout.preferredHeight: 34
                    enabled: root.focusedRow !== null && !root.focusedRow.interrupted
                    hoverEnabled: enabled
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13
                        color: parent.enabled ? theme.textSecondary : theme.textTertiary
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: !parent.enabled ? theme.bgInput : parent.hovered ? theme.accentBlue : theme.bgInput
                        border.color: !parent.enabled ? theme.textTertiary : parent.hovered ? theme.textPrimary : theme.textSecondary; radius: 4
                    }
                    onClicked: {
                        exportDialog.selectedScanId = root.focusedRow.label
                        exportDialog.selectedFile = "file:///" + MotionInterface.directory
                                                    + "/" + root.focusedRow.label + "_export.csv"
                        exportDialog.open()
                    }
                }
                Button {
                    text: "🔒 Delete" + (root.checkedCount > 0 ? " (" + root.checkedCount + ")" : "")
                    Layout.preferredWidth: 120; Layout.preferredHeight: 34
                    // Don't allow deleting while a scan is running (could be the
                    // in-flight session).
                    enabled: root.checkedCount > 0 && MotionInterface.state !== 4
                    hoverEnabled: enabled
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13
                        color: parent.enabled ? "#E8786A" : theme.textTertiary
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered && parent.enabled ? "#3A1714" : theme.bgInput
                        border.color: parent.enabled ? "#C0392B" : theme.textTertiary; radius: 4
                    }
                    onClicked: root.requestDelete()
                }
            }
        }

        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape) { root.close(); event.accepted = true }
        }

        // Busy overlay while "Load in viewer" is in flight.
        Rectangle {
            anchors.fill: parent; color: "#000"; opacity: 0.45
            visible: root.loadingPlot; z: 9999; radius: 12
            MouseArea { anchors.fill: parent }
            Column {
                anchors.centerIn: parent; spacing: 12
                BusyIndicator { running: root.loadingPlot; width: 48; height: 48 }
                Text { text: "Loading scan..."; color: theme.textPrimary; font.pixelSize: 14 }
            }
        }
    }

    // Reused developer-password prompt for delete confirmation.
    PasswordPromptModal {
        id: deletePrompt
        title: "Confirm Delete"
        description: "Enter the developer password to permanently delete the "
                     + "selected scan(s) from the database. This cannot be undone."
        confirmLabel: "Delete"
        onAccepted: root.doDelete()
    }

    Dialogs.FileDialog {
        id: exportDialog
        title: "Export Scan CSV"
        fileMode: Dialogs.FileDialog.SaveFile
        nameFilters: ["CSV files (*.csv)", "All files (*)"]
        property string selectedScanId: ""
        onAccepted: {
            var path = selectedFile.toString().replace("file:///", "")
            var ok = MotionInterface.exportScanCsv(selectedScanId, path)
            if (ok) MotionInterface.notify("Exported to " + path, "success")
        }
    }

    Dialogs.MessageDialog { id: histErrDialog; title: "Error"; text: "" }

    Connections {
        target: MotionInterface
        function onPastScanLoadFinished(label, ok) {
            root.loadingPlot = false
            if (ok) root.close()
        }
        function onDirectoryChanged() { if (root.visible) root.refresh() }
        function onErrorOccurred(msg) {
            root.loadingPlot = false
            histErrDialog.text = msg || "Unknown error."
            histErrDialog.visible = true
        }
    }
}
```

- [ ] **Step 2: Create the sortable header-cell component**

The header uses a small reusable cell. It takes its sort state via properties
and reports clicks via a signal — a child `.qml` component has its own id
scope and **cannot** see the `root` id defined in `HistoryModal.qml`, so all
coupling goes through the public interface. Create `components/HistoryHeaderCell.qml`:

```qml
import QtQuick 6.0
import QtQuick.Layouts 6.0

// One clickable, sort-aware column header in the History table.
Item {
    id: cell
    property string text: ""
    property string sortName: ""
    property string activeSort: ""   // the table's current sort key
    property bool ascending: false
    signal sortRequested(string name)

    Layout.preferredHeight: 30

    AppTheme { id: theme }

    Row {
        anchors.verticalCenter: parent.verticalCenter
        spacing: 4
        Text {
            text: cell.text
            color: theme.textSecondary
            font.pixelSize: 12; font.weight: Font.DemiBold
        }
        Text {
            visible: cell.activeSort === cell.sortName
            text: cell.ascending ? "▲" : "▼"
            color: theme.textSecondary; font.pixelSize: 9
            anchors.verticalCenter: parent.verticalCenter
        }
    }
    MouseArea {
        anchors.fill: parent
        cursorShape: Qt.PointingHandCursor
        onClicked: cell.sortRequested(cell.sortName)
    }
}
```

- [ ] **Step 3: Sanity-check the QML parses on launch**

The Delete guard binds `MotionInterface.state !== 4` — confirmed valid: `state` is a `pyqtProperty(int, notify=stateChanged)` at `motion_connector.py:1055`, and `4 == RUNNING` per the state machine at `motion_connector.py:72`. No code change here; the parse check happens implicitly when the app launches in Step 5 (a QML syntax error aborts startup and prints the offending line to the console — watch for it).

- [ ] **Step 4: Seed a scan DB for visual verification**

The app needs a `scans.db` with sessions to show anything. Create one in the app's data directory (`dataDirectory` in `config/app_config.json`; default `C:\Users\ethan\Projects\scan_data`). Run:

```bash
python -c "
from omotion.ScanDatabase import ScanDatabase
import time
db = ScanDatabase(db_path=r'C:\Users\ethan\Projects\scan_data\scans.db')
def mk(label, dt, lm, rm, dur, notes):
    sid = db.create_session(session_label=label, session_start=time.time(),
        session_end=time.time()+dur, session_notes=notes,
        session_meta={'scan_id':label.rsplit('_',1)[0],'subject_id':label.split('_',2)[2],
            'operator':'ethan','duration_sec':dur,
            'sdk_flags':{'reduced_mode':True,'left_camera_mask':lm,'right_camera_mask':rm}})
    for i in range(50):
        db.insert_session_data(session_id=sid,cam_id=0,side=0,timestamp_s=float(i),frame_id=i,bfi=1.0,bvi=2.0)
mk('20260615_093100_Patient14', None, 0x5A, 0x5A, 15, 'Resting baseline. 00:08 subject moved.')
mk('20260615_092000_Patient14', None, 0xC3, 0xC3, 5, 'Far config check.')
mk('20260614_164000_BaselineA', None, 0x66, 0x66, 15, 'Middle row.')
db.close(); print('seeded')
"
```

Expected: `seeded`.

- [ ] **Step 5: Launch the app and screenshot the History modal**

Run `python main.py`, click the **History** sidebar panel, then capture the window (per the project's visual-check rule — PrintWindow flag 2). From a second terminal:

```bash
python -c "
import win32gui, win32ui, win32con
from ctypes import windll
h = win32gui.FindWindow(None, 'OpenWater Bloodflow')
l,t,r,b = win32gui.GetWindowRect(h); w,ht=r-l,b-t
dc = win32gui.GetWindowDC(h); mdc = win32ui.CreateDCFromHandle(dc); sdc = mdc.CreateCompatibleDC()
bmp = win32ui.CreateBitmap(); bmp.CreateCompatibleBitmap(mdc,w,ht); sdc.SelectObject(bmp)
windll.user32.PrintWindow(h, sdc.GetSafeHdc(), 2)
bmp.SaveBitmapFile(sdc, 'history_check.bmp'); print('saved history_check.bmp')
"
```

Verify in `history_check.bmp`: three rows; columns User Label / Date-Time / Config (L/R, e.g. "Near / Near") / Duration ("0:15") / status dot; clicking a row fills the detail pane (label, operator, masks, config, samples ~50, notes); clicking a checkbox shows "N selected" and enables Delete; sorting by a header reorders rows; the search box filters. Confirm Load opens the viewer and Delete pops the password prompt.

- [ ] **Step 6: Commit**

```bash
git add components/HistoryModal.qml components/HistoryHeaderCell.qml
git commit -m "feat: rework History into a sortable multiselect data table"
```

---

## Task 4: Update the HIL History UI test

`tests/test_history.py` is `@pytest.mark.dev` and drives the real UI with pyautogui/UIA. The old version keys off the ComboBox and the "View in plot →" button, both gone. **This task cannot be validated in this environment — it runs on the self-hosted hardware runner.** Make the edits, eyeball them for correctness, and flag for runner validation.

**Files:**
- Modify: `tests/test_history.py`

- [ ] **Step 1: Replace the open-detection + seed-skip helpers**

The seed fixture uses `selected_scan_text()` (ComboBox) to decide whether to skip, and `_is_history_open()` detects the modal via a ComboBox. The new modal has neither. Replace both with a text scan for the modal title.

In `tests/test_history.py`, replace the `_is_history_open` function (lines ~246–255) with:

```python
def _history_text_present(needle: str) -> bool:
    """True iff any UIA element under the app window shows ``needle``."""
    try:
        win = uia_window()
        for elem in win.descendants():
            try:
                if needle.lower() in (elem.window_text() or "").lower():
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _is_history_open() -> bool:
    """The History modal is up iff its 'Scan History' title is visible."""
    return _history_text_present("Scan History")
```

- [ ] **Step 2: Fix the seed fixture's skip probe**

In `_seed_with_short_scan` (the `try` block ~lines 198–211), replace the `selected_scan_text()` skip check with a row-presence check. Change:

```python
        click_panel("History")
        time.sleep(SLEEP)
        if selected_scan_text():
            log.info("Skipping seed scan — History already has entries")
```

to:

```python
        click_panel("History")
        time.sleep(SLEEP)
        # A populated table shows the column header; an empty one shows
        # "No scans yet." Either way the modal is open here.
        if _history_text_present("User Label") and not _history_text_present("No scans yet"):
            log.info("Skipping seed scan — History already has entries")
```

- [ ] **Step 3: Rewrite `test_02` to assert a table row, not a ComboBox**

Replace `test_02_latest_scan_listed` (lines ~283–302) with:

```python
    def test_02_latest_scan_listed(self, app):
        _ensure_history_open()
        # Poll up to 5 s for the table to populate (the seed scan was
        # Middle/Middle, so its config cell reads "Middle / Middle").
        deadline = time.time() + 5.0
        found = False
        while time.time() < deadline:
            if _history_text_present("Middle") or not _history_text_present("No scans yet"):
                found = True
                break
            time.sleep(0.3)
        assert found, (
            "History table is empty after 5 s — no scans found. Run a "
            "scan first, or the History modal failed to open."
        )
```

- [ ] **Step 4: Rewrite `test_03` to focus a row then Load**

The Load button needs a focused row first. Replace `test_03_view_in_plot` (lines ~304–307) with:

```python
    def test_03_view_in_plot(self, app):
        """Focus the first row, then 'Load in viewer →' loads it into the
        embedded PlotViewer."""
        # Click the first data row to focus it (just below the header).
        win = uia_window()
        rows = [e for e in win.descendants(control_type="Text")
                if "Middle" in (e.window_text() or "")]
        if rows:
            click_element_center(rows[0], "first history row")
            time.sleep(SLEEP)
        click_by_name("Load in viewer →")
        time.sleep(SLEEP)
```

- [ ] **Step 5: Verify no remaining references to the removed ComboBox helpers**

Run: `python -c "s=open('tests/test_history.py',encoding='utf-8').read(); assert 'selected_scan_text' not in s, 'still references selected_scan_text'; assert 'View in plot' not in s, 'still references old button'; print('clean')"`
Expected: `clean`. (`read_combobox_values` is still used by the seed `_select_sensor` flow for Scan Settings — that's a different ComboBox and stays.)

- [ ] **Step 6: Byte-compile the test to catch syntax errors**

Run: `python -m py_compile tests/test_history.py`
Expected: no output (exit 0). Full execution requires the hardware runner — note that in the commit.

- [ ] **Step 7: Commit**

```bash
git add tests/test_history.py
git commit -m "test: update HIL History test for the new table UI (runner-validate)"
```

---

## Final verification

- [ ] Run the unit suite for the new slots: `python -m pytest tests/test_history_sessions.py -v` → all PASS.
- [ ] Run the kept listing test to confirm no regression: `python -m pytest tests/test_scan_history_list.py -v` → all PASS.
- [ ] Lint the touched Python: `python -m flake8 motion_connector.py tests/test_history_sessions.py` → no errors.
- [ ] Confirm the visual check from Task 3 Step 5 looked correct (table, detail pane, multiselect, sort, delete prompt).
- [ ] `tests/test_history.py` edited and byte-compiles; flagged for hardware-runner validation.

## Spec coverage check

- Modal container, DB-only source → Task 1/3. ✓
- Columns User Label / Date-Time / Config (L/R) / Duration / status → Task 3 delegate. ✓
- Sortable headers, search, config filter → Task 3 (`setSort`, `rebuildView`, toolbar). ✓
- Multiselect + select-all → Task 3 (`checked`, `setAllChecked`). ✓
- Read-only detail pane w/ notes + lazy sample count → Task 3 + `get_session_stats` (Task 2). ✓
- Load (single, reuse `loadPastScan`) / Export (reuse `exportScanCsv`) → Task 3. ✓
- Password-gated delete via `PasswordPromptModal` → `deleteScans` (Task 2) + Task 3. ✓
- Delete disabled while RUNNING; interrupted rows block Load/Export → Task 3. ✓
- Config name mapping unit-tested → Task 1 (`_config_name`). ✓
- No SDK changes. ✓
