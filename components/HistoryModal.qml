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
