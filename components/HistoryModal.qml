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

    property var scans: []
    property var selected: ({})
    property bool visualizing: false

    function open() {
        refreshScans()
        root.visible = true
    }
    function close() {
        root.visible = false
    }

    function refreshScans() {
        try {
            scans = MOTIONInterface.get_scan_list() || []
            if (scans.length > 0) {
                scanPicker.currentIndex = 0
                selected = MOTIONInterface.get_scan_details(scans[0]) || {}
            } else {
                selected = {}
            }
        } catch (e) {
            scans = []
            selected = {}
        }
    }

    function basename(p) {
        if (!p || p.length === 0) return ""
        const norm = p.replace(/\\/g, "/")
        const idx = norm.lastIndexOf("/")
        return idx >= 0 ? norm.slice(idx + 1) : norm
    }

    function friendlyDate(ts) {
        if (!ts || ts.length !== 15) return ts || "-"
        const y = ts.slice(0,4), m = ts.slice(4,6), d = ts.slice(6,8)
        const hh = ts.slice(9,11), mm = ts.slice(11,13), ss = ts.slice(13,15)
        return y + "-" + m + "-" + d + " " + hh + ":" + mm + ":" + ss
    }

    function formatMasks(leftMask, rightMask) {
        const left = leftMask ? "0x" + leftMask.toUpperCase() : ""
        const right = rightMask ? "0x" + rightMask.toUpperCase() : ""
        if (left && right) return left + ", " + right
        else if (left) return left
        else if (right) return right
        else return "-"
    }

    // Dimmed backdrop
    Rectangle {
        anchors.fill: parent
        color: "#000000AA"
        MouseArea { anchors.fill: parent; onClicked: {} }
    }

    Rectangle {
        width: Math.min(parent.width - 80, 900)
        height: Math.min(parent.height - 80, 600)
        radius: 12
        color: theme.bgContainer
        border.color: theme.borderSubtle
        border.width: 2
        anchors.centerIn: parent

        // X close button
        Rectangle {
            width: 28; height: 28; radius: 14
            color: xArea.containsMouse ? "#C0392B" : theme.borderStrong
            border.color: theme.borderHover; border.width: 1
            anchors.top: parent.top; anchors.right: parent.right
            anchors.topMargin: 10; anchors.rightMargin: 10
            z: 10
            Behavior on color { ColorAnimation { duration: 120 } }
            Text { anchors.centerIn: parent; text: "✕"; color: theme.textPrimary; font.pixelSize: 13 }
            MouseArea {
                id: xArea; anchors.fill: parent; hoverEnabled: true
                cursorShape: Qt.PointingHandCursor; onClicked: root.close()
            }
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 20
            spacing: 12

            // Header
            RowLayout {
                Layout.fillWidth: true
                spacing: 12

                Text {
                    text: "Scan History"
                    font.pixelSize: 20
                    font.weight: Font.Bold
                    color: theme.textPrimary
                }
                Item { Layout.fillWidth: true }

                Button {
                    text: "Open Folder"
                    Layout.preferredWidth: 110
                    Layout.preferredHeight: 32
                    hoverEnabled: true
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13; color: theme.textSecondary
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? theme.accentBlue : theme.bgInput
                        border.color: parent.hovered ? theme.textPrimary : theme.textSecondary; radius: 4
                    }
                    onClicked: Qt.openUrlExternally("file:///" + MOTIONInterface.directory)
                }

                Button {
                    text: "Refresh"
                    Layout.preferredWidth: 90
                    Layout.preferredHeight: 32
                    hoverEnabled: true
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13; color: theme.textSecondary
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? theme.accentBlue : theme.bgInput
                        border.color: parent.hovered ? theme.textPrimary : theme.textSecondary; radius: 4
                    }
                    onClicked: refreshScans()
                }
            }

            // Data directory
            RowLayout {
                Layout.fillWidth: true; spacing: 8
                Text { text: "Data Directory:"; color: theme.textSecondary; font.pixelSize: 13 }
                Text { text: MOTIONInterface.directory; color: theme.textPrimary; font.pixelSize: 13; elide: Text.ElideRight; Layout.fillWidth: true }
            }

            // Scan selector
            RowLayout {
                Layout.fillWidth: true; spacing: 12
                Text { text: "Scan:"; color: theme.textSecondary; font.pixelSize: 14 }
                ComboBox {
                    id: scanPicker
                    Layout.fillWidth: true
                    Layout.preferredHeight: 36
                    model: scans
                    font.pixelSize: 13
                    contentItem: Text {
                        leftPadding: 10
                        text: scanPicker.displayText
                        font: scanPicker.font
                        color: theme.textPrimary
                        verticalAlignment: Text.AlignVCenter
                        elide: Text.ElideRight
                    }
                    background: Rectangle {
                        color: theme.bgInput
                        radius: 4
                        border.color: scanPicker.activeFocus ? theme.accentBlue : theme.borderSubtle
                        border.width: 1
                    }
                    indicator: Text {
                        x: scanPicker.width - width - 10
                        y: (scanPicker.height - height) / 2
                        text: "\u25BE"
                        font.pixelSize: 14
                        color: theme.textSecondary
                    }
                    delegate: ItemDelegate {
                        width: scanPicker.width
                        height: 32
                        contentItem: Text {
                            text: modelData
                            font.pixelSize: 13
                            color: theme.textPrimary
                            verticalAlignment: Text.AlignVCenter
                            leftPadding: 8
                        }
                        background: Rectangle {
                            color: highlighted ? theme.accentBlue : "transparent"
                        }
                        highlighted: scanPicker.currentIndex === index
                    }
                    popup: Popup {
                        y: scanPicker.height
                        width: scanPicker.width
                        implicitHeight: contentItem.implicitHeight + 2
                        padding: 1
                        contentItem: ListView {
                            clip: true
                            implicitHeight: contentHeight
                            model: scanPicker.delegateModel
                            ScrollIndicator.vertical: ScrollIndicator {}
                        }
                        background: Rectangle {
                            color: theme.bgCard
                            radius: 4
                            border.color: theme.borderSubtle
                            border.width: 1
                        }
                    }
                    onCurrentIndexChanged: {
                        if (currentIndex >= 0 && currentIndex < scans.length) {
                            try { selected = MOTIONInterface.get_scan_details(scans[currentIndex]) || {} }
                            catch (e) { selected = {} }
                        } else { selected = {} }
                    }
                }
            }

            // Details
            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: 8; color: theme.bgCardAlt
                border.color: theme.borderSubtle; border.width: 1

                RowLayout {
                    anchors.fill: parent; anchors.margins: 12; spacing: 12

                    // Metadata + Notes
                    ColumnLayout {
                        Layout.fillWidth: true; Layout.fillHeight: true
                        spacing: 8

                        GridLayout {
                            columns: 4; columnSpacing: 16; rowSpacing: 6; Layout.fillWidth: true
                            Text { text: "Session ID:"; color: theme.textSecondary; font.pixelSize: 13 }
                            Text { text: selected.sessionId || "-"; color: theme.textPrimary; font.pixelSize: 13 }
                            Text { text: "Date:"; color: theme.textSecondary; font.pixelSize: 13 }
                            Text { text: selected.timestamp ? friendlyDate(selected.timestamp) : "-"; color: theme.textPrimary; font.pixelSize: 13 }

                            Text { text: "Left File:"; color: theme.textSecondary; font.pixelSize: 13 }
                            Text { text: basename(selected.leftPath) || "(none)"; color: theme.textPrimary; font.pixelSize: 12; elide: Text.ElideRight; Layout.fillWidth: true }
                            Text { text: "Mask:"; color: theme.textSecondary; font.pixelSize: 13 }
                            Text { text: formatMasks(selected.leftMask, selected.rightMask); color: theme.textPrimary; font.pixelSize: 13 }

                            Text { text: "Right File:"; color: theme.textSecondary; font.pixelSize: 13 }
                            Text { text: basename(selected.rightPath) || "(none)"; color: theme.textPrimary; font.pixelSize: 12; elide: Text.ElideRight; Layout.fillWidth: true }
                            Text { text: ""; } Text { text: ""; }
                        }

                        Text { text: "Notes:"; color: theme.textSecondary; font.pixelSize: 13 }
                        Rectangle {
                            Layout.fillWidth: true; Layout.fillHeight: true
                            radius: 4; color: theme.bgInput; border.color: theme.borderSubtle; border.width: 1
                            ScrollView {
                                anchors.fill: parent
                                TextArea {
                                    readOnly: true; wrapMode: Text.Wrap
                                    text: selected.notes || ""; color: theme.textPrimary; font.pixelSize: 13; background: null
                                }
                            }
                        }
                    }

                    // Actions
                    ColumnLayout {
                        Layout.preferredWidth: 200; Layout.fillHeight: true; spacing: 10

                        Text { text: "Actions"; color: theme.textPrimary; font.pixelSize: 15 }

                        Button {
                            text: "Visualize BFI/BVI (legacy)"
                            visible: MOTIONInterface.appConfig.developerMode ? true : false
                            Layout.fillWidth: true; Layout.preferredHeight: 36
                            enabled: !!(selected.leftPath || selected.rightPath)
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
                                root.visualizing = true
                                MOTIONInterface.visualize_bloodflow(selected.leftPath || "", selected.rightPath || "", 0.0, 0.0, false)
                            }
                        }

                        Button {
                            text: "Visualize Contrast/Mean (legacy)"
                            visible: MOTIONInterface.appConfig.developerMode ? true : false
                            Layout.fillWidth: true; Layout.preferredHeight: 36
                            enabled: !!(selected.leftPath || selected.rightPath)
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
                                root.visualizing = true
                                MOTIONInterface.visualize_bloodflow(selected.leftPath || "", selected.rightPath || "", 0.0, 0.0, true)
                            }
                        }

                        Button {
                            text: "Visualize BFI/BVI"
                            Layout.fillWidth: true; Layout.preferredHeight: 36
                            enabled: !!(selected.correctedPath)
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
                                root.visualizing = true
                                MOTIONInterface.visualize_corrected(selected.correctedPath || "")
                            }
                        }

                        Button {
                            text: "Visualize Contrast/Mean"
                            Layout.fillWidth: true; Layout.preferredHeight: 36
                            enabled: !!(selected.correctedPath)
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
                                root.visualizing = true
                                MOTIONInterface.visualize_corrected_signal(selected.correctedPath || "")
                            }
                        }

                        Item { Layout.fillHeight: true }
                    }
                }
            }

        }

        // Busy overlay
        Rectangle {
            anchors.fill: parent; color: "#000"; opacity: 0.45
            visible: root.visualizing; z: 9999; radius: 12
            MouseArea { anchors.fill: parent }
            Column {
                anchors.centerIn: parent; spacing: 12
                BusyIndicator { running: root.visualizing; width: 48; height: 48 }
                Text { text: "Processing..."; color: theme.textPrimary; font.pixelSize: 14 }
            }
        }
    }

    Dialogs.MessageDialog {
        id: histErrDialog
        title: "Visualization Error"
        text: ""
    }

    Connections {
        target: MOTIONInterface
        function onVizFinished() { root.visualizing = false }
        function onVisualizingChanged(b) { root.visualizing = b }
        function onDirectoryChanged() { if (root.visible) refreshScans() }
        function onErrorOccurred(msg) {
            root.visualizing = false
            histErrDialog.text = msg || "Unknown error."
            histErrDialog.visible = true
        }
    }
}
