import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import QtQuick.Dialogs as Dialogs
import OpenMotion 1.0

// Audit-log viewer. Lists machine-readable audit entries (newest first)
// and exports them as CSV. Opened from the password gate in SettingsModal.
// Governed by ModalManager (see HistoryModal.qml for the modal contract).
Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 9998

    AppTheme { id: theme }

    readonly property string label: "Audit Log"
    property var entries: []

    function open() {
        // Record the view first, then load (so the view event is included).
        MotionInterface.recordAuditLogViewed()
        refresh()
        root.visible = true
    }
    function close() { root.visible = false }

    function refresh() {
        try { entries = MotionInterface.auditLogEntries(500) || [] }
        catch (e) { entries = [] }
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
        color: theme.bgContainer
        border.color: theme.borderSubtle
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
                    color: theme.textPrimary
                }
                Item { Layout.fillWidth: true }

                Button {
                    text: "Export CSV"
                    Layout.preferredWidth: 110; Layout.preferredHeight: 32
                    hoverEnabled: true
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13; color: theme.textSecondary
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? theme.accentBlue : theme.bgInput
                        border.color: parent.hovered ? theme.textPrimary : theme.textSecondary; radius: 4
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
                    Text { text: "Time"; Layout.preferredWidth: col.time
                           color: theme.textSecondary; font.pixelSize: 12; font.weight: Font.DemiBold }
                    Text { text: "Event"; Layout.preferredWidth: col.event
                           color: theme.textSecondary; font.pixelSize: 12; font.weight: Font.DemiBold }
                    Text { text: "Details"; Layout.fillWidth: true
                           color: theme.textSecondary; font.pixelSize: 12; font.weight: Font.DemiBold }
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
                                color: theme.textSecondary; font.pixelSize: 12
                                font.family: "Consolas"; verticalAlignment: Text.AlignVCenter
                            }
                            Text {
                                Layout.preferredWidth: col.event
                                text: modelData.event_type || ""
                                color: theme.textPrimary; font.pixelSize: 12
                                verticalAlignment: Text.AlignVCenter
                            }
                            Text {
                                Layout.fillWidth: true
                                text: modelData.details || ""
                                color: theme.textSecondary; font.pixelSize: 12
                                font.family: "Consolas"
                                elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter
                            }
                        }
                    }

                    Text {
                        anchors.centerIn: parent
                        visible: root.entries.length === 0
                        text: "No audit entries yet."
                        color: theme.textSecondary; font.pixelSize: 14
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
