import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import QtQuick.Dialogs as Dialogs
import OpenMotion 1.0

// Audit-log viewer. Lists machine-readable audit entries (newest first),
// filterable by event type / date range / free text (#226), and exports
// them as CSV (always the COMPLETE log — filters are view-only, so the
// export stays the full audit record). Opened from the password gate in
// SettingsModal. Governed by ModalManager (see HistoryModal.qml for the
// modal contract).
Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 9998


    readonly property string label: "Audit Log"
    property var entries: []

    // ── filter state (#226) ────────────────────────────────────────
    property var eventTypes: []
    property string filterEventType: ""
    readonly property bool filtersActive: filterEventType !== ""
                                          || fromField.text !== ""
                                          || toField.text !== ""
                                          || searchField.text !== ""

    function open() {
        // Record the view first, then load (so the view event is included).
        MotionInterface.recordAuditLogViewed()
        reloadEventTypes()
        refresh()
        root.visible = true
    }
    function close() { root.visible = false }

    function refresh() {
        try {
            entries = MotionInterface.filteredAuditLogEntries({
                eventType: filterEventType,
                dateFrom: fromField.text,
                dateTo: toField.text,
                text: searchField.text,
                limit: 500
            }) || []
        } catch (e) { entries = [] }
    }

    function reloadEventTypes() {
        try { eventTypes = MotionInterface.auditEventTypes() || [] }
        catch (e) { eventTypes = [] }
        // Rebinding the combo model resets its index — restore the active
        // selection (or fall back to "All events" if the type vanished).
        var idx = eventTypes.indexOf(filterEventType)
        typeCombo.currentIndex = idx >= 0 ? idx + 1 : 0
        if (idx < 0) filterEventType = ""
    }

    function clearFilters() {
        typeCombo.currentIndex = 0
        filterEventType = ""
        fromField.text = ""
        toField.text = ""
        searchField.text = ""
        refresh()
    }

    // Debounce free-typing in the filter fields so each keystroke doesn't
    // hit the database.
    Timer {
        id: filterDebounce
        interval: 250
        onTriggered: root.refresh()
    }

    // Shared look for the filter text inputs. validDate drives the red
    // border on malformed dates (a malformed date simply doesn't filter).
    component FilterField: TextField {
        id: ff
        property bool validDate: true
        Layout.preferredHeight: 30
        font.pixelSize: 12
        color: AppTheme.textPrimary
        placeholderTextColor: AppTheme.textTertiary
        selectByMouse: true
        background: Rectangle {
            color: AppTheme.bgInput
            radius: 4
            border.color: !ff.validDate ? "#C0392B"
                          : ff.activeFocus ? AppTheme.accentBlue
                                           : AppTheme.borderSubtle
            border.width: 1
        }
    }

    QtObject {
        id: col
        readonly property int time: 160
        readonly property int event: 170
    }

    // ── backdrop ───────────────────────────────────────────────────
    Rectangle {
        anchors.fill: parent
        color: "#000000AA"
        // Capture ALL pointer input so scroll/hover can't fall through to
        // the interactive plot viewer behind the modal (issue #214).
        MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            onClicked: root.close()
            onWheel: function(wheel) { wheel.accepted = true }
        }
    }

    Rectangle {
        width: Math.min(parent.width - 60, 980)
        height: Math.min(parent.height - 60, 700)
        radius: 12
        color: AppTheme.bgContainer
        border.color: AppTheme.borderSubtle
        border.width: 2
        anchors.centerIn: parent

        MouseArea { anchors.fill: parent }   // absorb clicks

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
                    color: AppTheme.textPrimary
                }
                Item { Layout.fillWidth: true }

                Button {
                    text: "Export CSV"
                    Layout.preferredWidth: 110; Layout.preferredHeight: 32
                    hoverEnabled: true
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13; color: AppTheme.textSecondary
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? AppTheme.accentBlue : AppTheme.bgInput
                        border.color: parent.hovered ? AppTheme.textPrimary : AppTheme.textSecondary; radius: 4
                    }
                    onClicked: {
                        exportDialog.selectedFile = "file:///" + MotionInterface.directory
                                                    + "/audit_log.csv"
                        exportDialog.open()
                    }
                }
                Button {
                    text: "Refresh"
                    Layout.preferredWidth: 80; Layout.preferredHeight: 32
                    hoverEnabled: true
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13; color: AppTheme.textSecondary
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? AppTheme.accentBlue : AppTheme.bgInput
                        border.color: parent.hovered ? AppTheme.textPrimary : AppTheme.textSecondary; radius: 4
                    }
                    onClicked: root.refresh()
                }
                Rectangle {
                    width: 28; height: 28; radius: 14
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

            // ── filter bar (#226) ──────────────────────────────────
            RowLayout {
                Layout.fillWidth: true
                spacing: 8

                ComboBox {
                    id: typeCombo
                    objectName: "auditFilterType"
                    Layout.preferredWidth: 190
                    Layout.preferredHeight: 30
                    font.pixelSize: 12
                    model: ["All events"].concat(root.eventTypes)
                    onActivated: {
                        root.filterEventType =
                            currentIndex === 0 ? "" : currentText
                        root.refresh()
                    }
                    contentItem: Text {
                        leftPadding: 10; rightPadding: 24
                        text: typeCombo.displayText
                        font: typeCombo.font
                        color: AppTheme.textPrimary
                        verticalAlignment: Text.AlignVCenter
                        elide: Text.ElideRight
                    }
                    background: Rectangle {
                        color: AppTheme.bgInput; radius: 4
                        border.color: typeCombo.activeFocus
                                      ? AppTheme.accentBlue : AppTheme.borderSubtle
                        border.width: 1
                    }
                    indicator: Text {
                        x: typeCombo.width - width - 8
                        y: (typeCombo.height - height) / 2
                        text: "▾"; font.pixelSize: 12
                        color: AppTheme.textSecondary
                    }
                    popup: Popup {
                        y: typeCombo.height
                        width: typeCombo.width
                        implicitHeight: Math.min(contentItem.implicitHeight + 2, 320)
                        padding: 1
                        contentItem: ListView {
                            clip: true
                            implicitHeight: contentHeight
                            model: typeCombo.delegateModel
                            ScrollBar.vertical: ScrollBar {}
                        }
                        background: Rectangle {
                            color: AppTheme.bgCard; radius: 4
                            border.color: AppTheme.borderSubtle; border.width: 1
                        }
                    }
                    delegate: ItemDelegate {
                        width: typeCombo.width
                        height: 28
                        highlighted: typeCombo.highlightedIndex === index
                        contentItem: Text {
                            text: modelData
                            font.pixelSize: 12
                            color: AppTheme.textPrimary
                            verticalAlignment: Text.AlignVCenter
                            leftPadding: 8
                        }
                        background: Rectangle {
                            color: highlighted ? AppTheme.accentBlue : "transparent"
                        }
                    }
                }

                FilterField {
                    id: fromField
                    objectName: "auditFilterFrom"
                    Layout.preferredWidth: 118
                    placeholderText: "From YYYY-MM-DD"
                    validDate: text === "" || /^\d{4}-\d{2}-\d{2}$/.test(text)
                    onTextChanged: filterDebounce.restart()
                }
                Text { text: "–"; color: AppTheme.textSecondary; font.pixelSize: 12 }
                FilterField {
                    id: toField
                    objectName: "auditFilterTo"
                    Layout.preferredWidth: 118
                    placeholderText: "To YYYY-MM-DD"
                    validDate: text === "" || /^\d{4}-\d{2}-\d{2}$/.test(text)
                    onTextChanged: filterDebounce.restart()
                }
                FilterField {
                    id: searchField
                    objectName: "auditFilterSearch"
                    Layout.fillWidth: true
                    placeholderText: "Search events and details…"
                    onTextChanged: filterDebounce.restart()
                }
                Button {
                    text: "Clear"
                    enabled: root.filtersActive
                    Layout.preferredWidth: 64; Layout.preferredHeight: 30
                    hoverEnabled: true
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 12
                        color: parent.enabled ? AppTheme.textSecondary
                                              : AppTheme.textTertiary
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.enabled && parent.hovered
                               ? AppTheme.accentBlue : AppTheme.bgInput
                        border.color: parent.enabled && parent.hovered
                                      ? AppTheme.textPrimary : AppTheme.borderSubtle
                        radius: 4
                    }
                    onClicked: root.clearFilters()
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
                    Text { text: "Time"; Layout.preferredWidth: col.time
                           color: AppTheme.textSecondary; font.pixelSize: 12; font.weight: Font.DemiBold }
                    Text { text: "Event"; Layout.preferredWidth: col.event
                           color: AppTheme.textSecondary; font.pixelSize: 12; font.weight: Font.DemiBold }
                    Text { text: "Details"; Layout.fillWidth: true
                           color: AppTheme.textSecondary; font.pixelSize: 12; font.weight: Font.DemiBold }
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
                    model: root.entries
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: ScrollBar {}

                    delegate: Rectangle {
                        width: ListView.view.width
                        height: 30
                        color: index % 2 === 0 ? "transparent" : Qt.rgba(1, 1, 1, 0.02)
                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 8; anchors.rightMargin: 8
                            spacing: 8
                            Text {
                                Layout.preferredWidth: col.time
                                text: modelData.ts_iso || ""
                                color: AppTheme.textSecondary; font.pixelSize: 12
                                font.family: "Consolas"; verticalAlignment: Text.AlignVCenter
                            }
                            Text {
                                Layout.preferredWidth: col.event
                                text: modelData.event_type || ""
                                color: AppTheme.textPrimary; font.pixelSize: 12
                                verticalAlignment: Text.AlignVCenter
                            }
                            Text {
                                Layout.fillWidth: true
                                text: modelData.details || ""
                                color: AppTheme.textSecondary; font.pixelSize: 12
                                font.family: "Consolas"
                                elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter
                            }
                        }
                    }

                    Text {
                        anchors.centerIn: parent
                        visible: root.entries.length === 0
                        text: root.filtersActive
                              ? "No entries match the current filters."
                              : "No audit entries yet."
                        color: AppTheme.textSecondary; font.pixelSize: 14
                    }
                }
            }
        }

        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape) { root.close(); event.accepted = true }
        }
    }

    Dialogs.FileDialog {
        id: exportDialog
        title: "Export Audit Log CSV"
        fileMode: Dialogs.FileDialog.SaveFile
        nameFilters: ["CSV files (*.csv)", "All files (*)"]
        onAccepted: {
            var path = selectedFile.toString().replace("file:///", "")
            // exportAuditLogCsv returns the written path ("" on failure).
            var dest = MotionInterface.exportAuditLogCsv(path)
            if (dest) {
                MotionInterface.notify("Audit log exported to " + dest, "success")
                root.refresh()   // pick up the audit_log_exported entry
            }
        }
    }

    // Refresh if the data directory (and therefore the scan DB) changes
    // while the viewer is open — mirrors HistoryModal's behavior.
    Connections {
        target: MotionInterface
        function onDirectoryChanged() { if (root.visible) root.refresh() }
    }
}
