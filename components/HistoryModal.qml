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


    // Modal interface label (single source of truth — see ModalManager).
    readonly property string label: "Scan History"

    // Full session list from the connector (newest first).
    property var scans: []
    // Filtered + sorted scans (select-all, focus and Delete work on these).
    property var view: []
    // What the ListView renders: `view` as flat scan rows, or, with
    // "Aggregate sessions" on, one collapsible group row per user label that
    // has 2+ scans (its scans nested below it while expanded). Items are
    // { kind: "scan", scan, nested } or
    // { kind: "group", key, ids, count, totalSec, dateText, configText, open }.
    property var rows: []
    // "Aggregate sessions" toggle and the expanded groups ({ userLabel: true }).
    // Both survive closing the modal for the rest of the app session.
    property bool aggregate: false
    property var expanded: ({})
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
    property string sortKey: "timestamp"
    property bool sortAsc: false
    // Async "Load in viewer" busy state (issue #152 pattern).
    property bool loadingPlot: false

    // The completed/interrupted status dot is a diagnostic with no column
    // header and no legend — clinical users only see an unexplained mark.
    // Interrupted rows already announce themselves on export ("Interrupted
    // scans can't be exported."), so hide the column outside engineering.
    readonly property bool engineeringMode:
        MotionInterface.appConfig.engineeringMode === true

    // Left-edge space to keep clear of the icon bar (BloodFlow's
    // ButtonPanel — 80px wide + 8px margin, pinned left at z:10000, which
    // is ABOVE this modal's z:9998). The card is centered in the region to
    // the RIGHT of this inset so it never slides under the bar on narrow
    // windows. Matches PlotViewer's left content edge (88 + 16 gutter).
    readonly property int iconBarInset: 104

    // Outline icon font shared with the sidebar (ButtonPanel).
    FontLoader {
        id: historyIconFont
        source: "../assets/fonts/keenicons-outline.ttf"
    }

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
        clearActive()
        rebuildView()
    }

    function rebuildView() {
        var q = (searchText || "").toLowerCase()
        // In clinical mode, omit scans not shot in clinical mode — the
        // clinical viewer can't render their per-camera data. Engineering
        // mode lists everything. A scan with no recorded mode counts as
        // non-clinical.
        var appClinical = MotionInterface.appConfig.clinicalMode === true
        var arr = scans.filter(function(r) {
            if (appClinical && !r.clinicalMode)
                return false
            if (q.length > 0
                && (r.userLabel || "").toLowerCase().indexOf(q) < 0
                && (r.label || "").toLowerCase().indexOf(q) < 0)
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
        rebuildRows()
    }

    function rebuildRows() {
        if (!aggregate) {
            rows = view.map(function(r) { return { kind: "scan", scan: r, nested: false } })
            return
        }
        // Group in view order, so the active sort orders the groups (by
        // their first scan) and the scans inside each group.
        var order = [], groups = {}
        for (var i = 0; i < view.length; i++) {
            var k = view[i].userLabel || ""
            if (!groups.hasOwnProperty(k)) { groups[k] = []; order.push(k) }
            groups[k].push(view[i])
        }
        var out = []
        for (var j = 0; j < order.length; j++) {
            var g = groups[order[j]]
            if (g.length === 1) { out.push({ kind: "scan", scan: g[0], nested: false }); continue }
            var ids = [], total = 0, cfgs = {}, nCfg = 0
            var dates = []
            for (var m = 0; m < g.length; m++) {
                ids.push(g[m].sessionId)
                if (g[m].durationSec > 0) total += g[m].durationSec
                var cfg = (g[m].configL || "—") + " / " + (g[m].configR || "—")
                if (!cfgs[cfg]) { cfgs[cfg] = true; nCfg++ }
                if (g[m].dateTime) dates.push(("" + g[m].dateTime).substring(0, 10))
            }
            dates.sort()
            var dateText = dates.length === 0 ? "-"
                         : (dates[0] === dates[dates.length - 1] ? dates[0]
                            : dates[0] + " – " + dates[dates.length - 1].substring(5))
            var open = expanded[order[j]] === true
            out.push({ kind: "group", key: order[j], ids: ids, count: g.length,
                       totalSec: total, dateText: dateText,
                       configText: nCfg === 1 ? Object.keys(cfgs)[0] : "Mixed",
                       open: open })
            if (open)
                for (var n = 0; n < g.length; n++)
                    out.push({ kind: "scan", scan: g[n], nested: true })
        }
        rows = out
    }

    function toggleGroup(key) {
        var e = Object.assign({}, expanded)
        if (e[key]) delete e[key]; else e[key] = true
        expanded = e
        rebuildRows()
    }

    function groupCheckedCount(ids) {
        var n = 0
        for (var i = 0; i < ids.length; i++) if (checked[ids[i]]) n++
        return n
    }

    // Group checkbox: check every scan in the group, or clear them all when
    // they're already all checked.
    function setGroupChecked(ids, on) {
        var c = Object.assign({}, checked), n = checkedCount
        for (var i = 0; i < ids.length; i++) {
            if (on && !c[ids[i]]) { c[ids[i]] = true; n++ }
            else if (!on && c[ids[i]]) { delete c[ids[i]]; n-- }
        }
        checked = c; checkedCount = n
        reconcileActive()
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

    // Reset the active (detail-pane / Load + Export target) row.
    function clearActive() {
        focusedSessionId = -1; focusedRow = null; focusedSampleCount = -1
    }

    // Plain row-body click = standard single-select: replace the checked
    // set with just this row and make it the active row.
    function selectRow(sid) {
        var c = {}; c[sid] = true
        checked = c; checkedCount = 1
        focusRow(sid)
    }

    // Keep the active row valid after a checkbox change: if it's no longer
    // checked, move the highlight to the first checked row in view order,
    // else clear it. (Invariant: the active row is always checked.)
    function reconcileActive() {
        if (focusedSessionId >= 0 && checked[focusedSessionId]) return
        for (var i = 0; i < view.length; i++)
            if (checked[view[i].sessionId]) { focusRow(view[i].sessionId); return }
        clearActive()
    }

    // Every checked row, across the full list (ignores the search filter,
    // matching Delete which also operates on all checked ids).
    function checkedRows() {
        var rows = []
        for (var i = 0; i < scans.length; i++)
            if (checked[scans[i].sessionId]) rows.push(scans[i])
        return rows
    }

    function toggleCheck(sid) {
        var c = Object.assign({}, checked)
        if (c[sid]) { delete c[sid]; checkedCount -= 1 }
        else { c[sid] = true; checkedCount += 1 }
        checked = c
        reconcileActive()
    }

    function setAllChecked(on) {
        var c = {}, n = 0
        if (on) for (var i = 0; i < view.length; i++) { c[view[i].sessionId] = true; n++ }
        checked = c; checkedCount = n
        reconcileActive()
    }

    function requestDelete() {
        if (checkedCount <= 0) return
        deleteConfirm.open()
    }

    function requestExport() {
        var rows = checkedRows()
        if (rows.length === 0) return

        if (rows.length === 1) {
            // Single export keeps the Save As dialog (lets the user rename).
            if (rows[0].interrupted) {
                MotionInterface.notify("Interrupted scans can't be exported.", "warning")
                return
            }
            // Export by the unique DB session id — labels can collide, and a
            // by-label lookup silently exports the newest match (issue #254).
            exportDialog.exportSessionId = rows[0].sessionId
            exportDialog.selectedFile = "file:///" + MotionInterface.directory
                                        + "/" + rows[0].label + "_export.csv"
            exportDialog.open()
            return
        }

        // 2+ checked -> pick a destination folder. Interrupted scans can't be
        // exported, so drop them here and fold them into the skip count.
        var ids = [], skipped = 0
        for (var i = 0; i < rows.length; i++) {
            if (rows[i].interrupted) { skipped += 1; continue }
            ids.push(rows[i].sessionId)
        }
        if (ids.length === 0) {
            MotionInterface.notify("Interrupted scans can't be exported.", "warning")
            return
        }
        exportFolderDialog.pendingSessionIds = ids
        exportFolderDialog.pendingSkipped = skipped
        exportFolderDialog.open()
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

    // Trim a scan label for display on the fixed-width Load button.
    function shortLabel(s) {
        s = "" + (s || "")
        return s.length > 16 ? (s.substring(0, 15) + "…") : s
    }

    // Format an 8-bit mask as "0xNN", or "—" when unknown (< 0). Keeps the
    // detail pane consistent with the Config cell, which also shows "—" for
    // an unknown mask (a -1 rendered as 0xFF would misread as "All").
    function maskLabel(m) {
        if (m === undefined || m === null || m < 0) return "—"
        return "0x" + (m & 0xFF).toString(16).toUpperCase()
    }

    // Re-filter whenever the search text changes.
    onSearchTextChanged: rebuildView()
    onAggregateChanged: rebuildRows()

    // ── backdrop ───────────────────────────────────────────────────
    Rectangle {
        anchors.fill: parent
        color: "#000000AA"
        // Capture ALL pointer input so it can't fall through to the
        // interactive plot viewer behind the modal (issue #214): without
        // hoverEnabled the plot's crosshair tracks the cursor, and without
        // an onWheel handler the scroll wheel zooms/pans the plot.
        MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            onClicked: root.close()
            onWheel: function(wheel) { wheel.accepted = true }
        }
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
        width: Math.min(parent.width - root.iconBarInset - 40, 1040)
        height: Math.min(parent.height - 60, 680)
        radius: 12
        color: AppTheme.sheetBg
        border.color: AppTheme.borderSubtle
        border.width: 2
        // Center within [iconBarInset, parent.width] so the card clears the
        // icon bar instead of bleeding under it. horizontalCenterOffset
        // shifts the full-width center right by half the reserved inset.
        anchors.verticalCenter: parent.verticalCenter
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.horizontalCenterOffset: root.iconBarInset / 2

        // Absorb empty-space clicks so they don't reach the backdrop.
        MouseArea { anchors.fill: parent }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 18
            spacing: 12

            // ── toolbar ────────────────────────────────────────────
            RowLayout {
                Layout.fillWidth: true
                spacing: 12

                Text {
                    text: root.label
                    font.pixelSize: 20; font.weight: Font.Bold
                    color: AppTheme.textPrimary
                }
                Item { Layout.fillWidth: true }

                HistoryCheckBox {
                    objectName: "historyAggregate"
                    text: "Aggregate sessions"
                    checked: root.aggregate
                    onToggled: root.aggregate = !root.aggregate
                }

                TextField {
                    id: searchField
                    Layout.preferredWidth: 240
                    Layout.preferredHeight: 34
                    placeholderText: "Search label…"
                    color: AppTheme.textPrimary
                    placeholderTextColor: AppTheme.textSecondary
                    font.pixelSize: 13
                    leftPadding: 12; rightPadding: 12
                    verticalAlignment: TextInput.AlignVCenter
                    background: Rectangle {
                        color: AppTheme.bgInput; radius: 6
                        border.color: searchField.activeFocus ? AppTheme.accentInteractive : AppTheme.borderSubtle
                        border.width: 1
                    }
                    onTextChanged: root.searchText = text
                }

                Button {
                    id: deleteBtn
                    objectName: "historyDeleteButton"
                    // Icon-only. The count lives in the tooltip and the
                    // confirm prompt.
                    text: "Delete" + (root.checkedCount > 0 ? " " + root.checkedCount
                                      + (root.checkedCount === 1 ? " scan" : " scans") : "")
                    display: AbstractButton.IconOnly
                    // Square, same height as the search field. Zero the
                    // Material insets, which otherwise shrink the background
                    // to a short pill inside the 34 px cell.
                    Layout.preferredWidth: 34; Layout.preferredHeight: 34
                    topInset: 0; bottomInset: 0; leftInset: 0; rightInset: 0
                    padding: 0
                    leftPadding: 0; rightPadding: 0; topPadding: 0; bottomPadding: 0
                    // Don't allow deleting while a scan is running (could be the
                    // in-flight session). The confirm prompt still guards it.
                    enabled: root.checkedCount > 0 && MotionInterface.state !== 4
                    hoverEnabled: true
                    ToolTip.visible: hovered
                    ToolTip.delay: 400
                    ToolTip.text: enabled ? text
                                          : (root.checkedCount > 0 ? "Can't delete while a scan is running"
                                                                   : "Select scans to delete")
                    // Keenicons trash glyph (same icon font as the sidebar)
                    // instead of the 🗑 emoji, which rendered as a full-colour
                    // system emoji that clashed with the outline UI.
                    // Centre on the glyph's ink, not its line box: the icon
                    // font's ascent/descent sit the trash low in a plain
                    // centred Text.
                    contentItem: Item {
                        TextMetrics {
                            id: trashMetrics
                            font.family: historyIconFont.name
                            font.pixelSize: 18
                            text: "\ueb0e"   // keenicons "trash"
                        }
                        Text {
                            text: trashMetrics.text
                            font: trashMetrics.font
                            color: deleteBtn.enabled ? AppTheme.accentRed : AppTheme.textTertiary
                            x: Math.round((parent.width - trashMetrics.tightBoundingRect.width) / 2
                                          - trashMetrics.tightBoundingRect.x)
                            y: Math.round((parent.height - trashMetrics.tightBoundingRect.height) / 2
                                          - trashMetrics.tightBoundingRect.y - baselineOffset)
                        }
                    }
                    background: Rectangle {
                        radius: 6
                        color: deleteBtn.hovered && deleteBtn.enabled
                               ? Qt.rgba(0.75, 0.22, 0.17, 0.16) : "transparent"
                        border.color: deleteBtn.enabled ? AppTheme.accentRed : AppTheme.textTertiary
                        border.width: 1
                    }
                    onClicked: root.requestDelete()
                }

                Rectangle {
                    width: 30; height: 30; radius: 15
                    color: xArea.containsMouse ? "#C0392B" : AppTheme.borderStrong
                    border.color: AppTheme.borderHover; border.width: 1
                    Behavior on color { ColorAnimation { duration: 120 } }
                    Text { anchors.centerIn: parent; text: "✕"; color: AppTheme.textPrimary; font.pixelSize: 13 }
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
                color: AppTheme.bgCardAlt; radius: 4
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 8; anchors.rightMargin: 8
                    spacing: 8

                    // Select-all checkbox.
                    // Tri-state: dash while only some rows are checked.
                    HistoryCheckBox {
                        objectName: "historySelectAll"
                        Layout.preferredWidth: col.chk
                        Layout.fillHeight: true
                        readonly property bool all:
                            root.checkedCount > 0 && root.checkedCount === root.view.length
                        checked: all
                        partial: root.checkedCount > 0 && !all
                        onToggled: root.setAllChecked(!all)
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
                    Item {
                        visible: root.engineeringMode
                        Layout.preferredWidth: col.status
                    }
                }
            }

            // ── table body ─────────────────────────────────────────
            Rectangle {
                Layout.fillWidth: true; Layout.fillHeight: true
                radius: 6; color: AppTheme.bgCardAlt
                border.color: AppTheme.borderSubtle; border.width: 1
                clip: true

                ListView {
                    id: listView
                    anchors.fill: parent
                    anchors.margins: 2
                    model: root.rows
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: ScrollBar {}

                    delegate: Item {
                        id: row
                        width: ListView.view.width
                        height: 34
                        readonly property bool isScan: modelData.kind === "scan"

                        // Group header (aggregate mode): chevron, tri-state
                        // checkbox for the whole group, label + count, the
                        // date span, shared config, and total duration.
                        Rectangle {
                            id: grpRow
                            anchors.fill: parent
                            visible: !row.isScan
                            readonly property var grp: row.isScan ? ({ ids: [], count: 0 }) : modelData
                            readonly property int nChecked: root.groupCheckedCount(grp.ids)
                            color: grpArea.containsMouse ? AppTheme.bgHover : "transparent"

                            MouseArea {
                                id: grpArea
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: root.toggleGroup(grpRow.grp.key)
                            }

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 8; anchors.rightMargin: 8
                                spacing: 8

                                HistoryCheckBox {
                                    Layout.preferredWidth: col.chk
                                    Layout.fillHeight: true
                                    checked: grpRow.nChecked > 0
                                             && grpRow.nChecked === grpRow.grp.count
                                    partial: grpRow.nChecked > 0 && !checked
                                    onToggled: root.setGroupChecked(grpRow.grp.ids, !checked)
                                }
                                Row {
                                    Layout.fillWidth: true
                                    spacing: 8
                                    clip: true
                                    Text {
                                        anchors.verticalCenter: parent.verticalCenter
                                        text: "\ue9e5"   // keenicons "down"
                                        font.family: historyIconFont.name
                                        font.pixelSize: 12
                                        color: AppTheme.textSecondary
                                        rotation: grpRow.grp.open ? 0 : -90
                                        Behavior on rotation { NumberAnimation { duration: 120 } }
                                    }
                                    Text {
                                        anchors.verticalCenter: parent.verticalCenter
                                        text: grpRow.grp.key || "(unlabeled)"
                                        color: AppTheme.textPrimary
                                        font.pixelSize: 13; font.weight: Font.DemiBold
                                    }
                                    Rectangle {
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: countText.implicitWidth + 14; height: 18; radius: 9
                                        color: AppTheme.bgInput
                                        border.color: AppTheme.borderSubtle; border.width: 1
                                        Text {
                                            id: countText
                                            anchors.centerIn: parent
                                            text: grpRow.grp.count + " sessions"
                                            color: AppTheme.textSecondary; font.pixelSize: 11
                                        }
                                    }
                                }
                                Text {
                                    Layout.preferredWidth: col.date
                                    text: grpRow.grp.dateText || "-"
                                    color: AppTheme.textSecondary; font.pixelSize: 13
                                    elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter
                                }
                                Text {
                                    Layout.preferredWidth: col.config
                                    text: grpRow.grp.configText || "—"
                                    color: AppTheme.textSecondary; font.pixelSize: 13
                                    elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter
                                }
                                Text {
                                    Layout.preferredWidth: col.dur
                                    text: root.formatDuration(grpRow.grp.totalSec)
                                    color: AppTheme.textSecondary; font.pixelSize: 13
                                    verticalAlignment: Text.AlignVCenter
                                }
                                Item {
                                    visible: root.engineeringMode
                                    Layout.preferredWidth: col.status
                                }
                            }
                        }

                        // Scan row (flat list, or nested under an open group).
                        Rectangle {
                            id: scanRow
                            anchors.fill: parent
                            visible: row.isScan
                            readonly property var sc: row.isScan ? modelData.scan : ({})
                            // Row tints follow the theme accent so they match
                            // the checkboxes (blue in dark, terracotta in light).
                            readonly property color tint: AppTheme.accentInteractive
                            color: scanRow.sc.sessionId === root.focusedSessionId
                                   ? Qt.rgba(tint.r, tint.g, tint.b, 0.16)
                                   : (root.checked[scanRow.sc.sessionId]
                                      ? Qt.rgba(tint.r, tint.g, tint.b, 0.07) : "transparent")

                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: root.selectRow(scanRow.sc.sessionId)
                            }

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: modelData.nested ? 28 : 8
                                anchors.rightMargin: 8
                                spacing: 8

                                HistoryCheckBox {
                                    Layout.preferredWidth: col.chk
                                    Layout.fillHeight: true
                                    checked: root.checked[scanRow.sc.sessionId] === true
                                    onToggled: root.toggleCheck(scanRow.sc.sessionId)
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: scanRow.sc.userLabel || "(unlabeled)"
                                    color: AppTheme.textPrimary; font.pixelSize: 13
                                    elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter
                                }
                                Text {
                                    Layout.preferredWidth: col.date
                                    text: scanRow.sc.dateTime || "-"
                                    color: AppTheme.textSecondary; font.pixelSize: 13
                                    verticalAlignment: Text.AlignVCenter
                                }
                                Text {
                                    Layout.preferredWidth: col.config
                                    text: (scanRow.sc.configL || "—") + " / " + (scanRow.sc.configR || "—")
                                    color: AppTheme.textSecondary; font.pixelSize: 13
                                    elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter
                                }
                                Text {
                                    Layout.preferredWidth: col.dur
                                    text: root.formatDuration(scanRow.sc.durationSec)
                                    color: AppTheme.textSecondary; font.pixelSize: 13
                                    verticalAlignment: Text.AlignVCenter
                                }
                                Text {
                                    visible: root.engineeringMode
                                    Layout.preferredWidth: col.status
                                    text: scanRow.sc.interrupted ? "⚠" : "●"
                                    color: scanRow.sc.interrupted ? "#E6A23C" : "#3EC97A"
                                    font.pixelSize: 13
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                }
                            }
                        }
                    }

                    // Empty state.
                    Text {
                        anchors.centerIn: parent
                        visible: root.view.length === 0
                        text: root.scans.length === 0 ? "No scans yet."
                                                      : "No scans match the filter."
                        color: AppTheme.textSecondary; font.pixelSize: 14
                    }
                }
            }

            // ── detail pane (focused row, read-only) ───────────────
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 230
                radius: 6; color: AppTheme.bgCardAlt
                border.color: AppTheme.borderSubtle; border.width: 1
                visible: root.focusedRow !== null

                // Stacked: a compact metadata strip on top, then the notes box
                // spanning the full pane width below (much wider than the old
                // side-by-side layout).
                ColumnLayout {
                    anchors.fill: parent; anchors.margins: 14; spacing: 10

                    GridLayout {
                        Layout.fillWidth: true
                        columns: 6; columnSpacing: 12; rowSpacing: 6
                        Text { text: "Full label:"; color: AppTheme.textSecondary; font.pixelSize: 12 }
                        Text { text: root.focusedRow ? root.focusedRow.label : ""; color: AppTheme.textPrimary; font.pixelSize: 12; elide: Text.ElideRight; Layout.fillWidth: true }
                        Text { text: "Operator:"; color: AppTheme.textSecondary; font.pixelSize: 12 }
                        Text { text: root.focusedRow ? (root.focusedRow.operator || "-") : ""; color: AppTheme.textPrimary; font.pixelSize: 12 }
                        Text { text: "Samples:"; color: AppTheme.textSecondary; font.pixelSize: 12 }
                        Text { text: root.focusedSampleCount < 0 ? "…" : root.focusedSampleCount.toLocaleString(); color: AppTheme.textPrimary; font.pixelSize: 12; Layout.preferredWidth: 70 }

                        Text { text: "Mask (L / R):"; color: AppTheme.textSecondary; font.pixelSize: 12 }
                        Text {
                            color: AppTheme.textPrimary; font.pixelSize: 12
                            text: root.focusedRow
                                  ? (root.maskLabel(root.focusedRow.leftMask)
                                     + " / " + root.maskLabel(root.focusedRow.rightMask))
                                  : ""
                        }
                        Text { text: "Config (L / R):"; color: AppTheme.textSecondary; font.pixelSize: 12 }
                        Text { text: root.focusedRow ? (root.focusedRow.configL + " / " + root.focusedRow.configR) : ""; color: AppTheme.textPrimary; font.pixelSize: 12 }
                    }

                    Text { text: "Notes (read-only):"; color: AppTheme.textSecondary; font.pixelSize: 12 }
                    Rectangle {
                        Layout.fillWidth: true; Layout.fillHeight: true
                        radius: 4; color: AppTheme.bgInput
                        border.color: AppTheme.borderSubtle; border.width: 1
                        ScrollView {
                            anchors.fill: parent; anchors.margins: 2
                            TextArea {
                                readOnly: true; wrapMode: Text.Wrap; background: null
                                text: root.focusedRow ? (root.focusedRow.notes || "") : ""
                                color: AppTheme.textPrimary; font.pixelSize: 12
                            }
                        }
                    }
                }
            }

            // ── actions ────────────────────────────────────────────
            // Primary actions (Export, then the emphasized Load) on the
            // right. Delete lives in the toolbar, top right.
            RowLayout {
                Layout.fillWidth: true; spacing: 10

                Item { Layout.fillWidth: true }

                Button {
                    id: exportBtn
                    text: "Export CSV" + (root.checkedCount > 0 ? "  (" + root.checkedCount + ")" : "")
                    Layout.preferredWidth: 128; Layout.preferredHeight: 36
                    enabled: root.checkedCount > 0
                    hoverEnabled: enabled
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13
                        color: exportBtn.enabled ? AppTheme.textSecondary : AppTheme.textTertiary
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        radius: 6
                        color: exportBtn.hovered && exportBtn.enabled ? AppTheme.bgHover : "transparent"
                        border.color: exportBtn.enabled ? AppTheme.borderStrong : AppTheme.textTertiary
                        border.width: 1
                    }
                    onClicked: root.requestExport()
                }
                Button {
                    id: loadBtn
                    text: root.focusedRow
                          ? ("Load “" + root.shortLabel(root.focusedRow.userLabel
                                             || root.focusedRow.label || "scan") + "”  →")
                          : "Load in viewer  →"
                    Layout.preferredWidth: 200; Layout.preferredHeight: 36
                    enabled: root.focusedRow !== null && !root.focusedRow.interrupted
                    hoverEnabled: enabled
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13; font.weight: Font.DemiBold
                        color: loadBtn.enabled ? "#FFFFFF" : AppTheme.textTertiary
                        elide: Text.ElideRight
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        radius: 6
                        color: !loadBtn.enabled ? AppTheme.bgInput
                               : (loadBtn.hovered ? Qt.lighter(AppTheme.accentInteractive, 1.12)
                                                  : AppTheme.accentInteractive)
                    }
                    onClicked: {
                        root.loadingPlot = true
                        // Load by the unique DB session id — labels can
                        // collide, and a by-label lookup silently loads
                        // the newest match (issue #254). The label still
                        // names the scan's CSVs and log lines.
                        MotionInterface.loadPastScan(root.focusedRow.sessionId,
                                                     root.focusedRow.label)
                    }
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
                Text { text: "Loading scan..."; color: AppTheme.textPrimary; font.pixelSize: 14 }
            }
        }
    }

    // "Are you sure?" before a delete, in every build. No password (#703):
    // the requirement is a confirmation step, not authentication.
    ConfirmModal {
        id: deleteConfirm
        objectName: "deleteScansConfirm"
        title: "Confirm Delete"
        destructive: true
        description: {
            var n = root.checkedCount
            return "Are you sure you want to permanently delete "
                   + (n === 1 ? "this scan" : "these " + n + " scans")
                   + " from the database? This cannot be undone."
        }
        confirmLabel: "Delete"
        onAccepted: root.doDelete()
    }

    Dialogs.FileDialog {
        id: exportDialog
        title: "Export Scan CSV"
        fileMode: Dialogs.FileDialog.SaveFile
        nameFilters: ["CSV files (*.csv)", "All files (*)"]
        property int exportSessionId: -1
        onAccepted: {
            var path = selectedFile.toString().replace("file:///", "")
            // Async: runs on a worker thread; the success toast fires
            // from onScanCsvExportFinished below. By unique session id
            // (labels can collide — #254).
            MotionInterface.exportScanCsv(exportSessionId, path)
        }
    }

    Dialogs.FolderDialog {
        id: exportFolderDialog
        title: "Export Scans to Folder"
        currentFolder: "file:///" + (MotionInterface.directory || "")
        property var pendingSessionIds: []
        property int pendingSkipped: 0
        onAccepted: {
            var folder = selectedFolder.toString().replace("file:///", "")
            // Async: the batch runs on a worker thread; the summary
            // toast fires from onScansExportFinished below, which folds
            // in pendingSkipped (interrupted scans filtered pre-dialog).
            // By unique session id (labels can collide — #254).
            MotionInterface.exportScansToFolder(pendingSessionIds, folder)
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
        function onScanCsvExportFinished(ok, path) {
            if (ok) MotionInterface.notify("Exported to " + path, "success")
            // Failure toast comes from errorOccurred (handler below).
        }
        function onScansExportFinished(exported, skipped, folder) {
            var sk = exportFolderDialog.pendingSkipped + skipped
            exportFolderDialog.pendingSkipped = 0
            var msg = "Exported " + exported + " scan(s) to " + folder
            if (sk > 0) msg += " (" + sk + " skipped)"
            MotionInterface.notify(msg, exported > 0 ? "success" : "warning",
                                   4000, true, "scan-export")
        }
        // Re-apply the clinical-mode row filter if the app's mode changes
        // while History is open.
        function onAppConfigChanged() { if (root.visible) root.rebuildView() }
        function onErrorOccurred(msg) {
            root.loadingPlot = false
            histErrDialog.text = msg || "Unknown error."
            histErrDialog.visible = true
        }
    }
}
