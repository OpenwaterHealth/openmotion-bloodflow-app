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

    // Settings values
    property int defaultCameraIndex: 4  // "Outer" by default
    property string dataOutputPath: MOTIONInterface.directory
    property bool showBfiBvi: true  // true = BFI/BVI, false = Mean/StdDev
    property bool autoScale: true
    property real bfiMin: 0.0
    property real bfiMax: 10.0
    property real bviMin: 0.0
    property real bviMax: 10.0

    signal settingsChanged()

    function open() {
        dataPathField.text = MOTIONInterface.directory
        root.visible = true
    }
    function close() {
        MOTIONInterface.directory = dataPathField.text
        settingsChanged()
        root.visible = false
    }

    ListModel {
        id: cameraPatterns
        ListElement { name: "None" }
        ListElement { name: "Near" }
        ListElement { name: "Middle" }
        ListElement { name: "Far" }
        ListElement { name: "Outer" }
        ListElement { name: "Left" }
        ListElement { name: "Right" }
        ListElement { name: "Third Row" }
        ListElement { name: "All" }
    }

    // Dimmed backdrop
    Rectangle {
        anchors.fill: parent
        color: "#000000AA"
        MouseArea { anchors.fill: parent; onClicked: {} }
    }

    Rectangle {
        width: Math.min(parent.width - 80, 650)
        height: Math.min(parent.height - 40, 640)
        radius: 12
        color: "#1E1E20"
        border.color: "#3E4E6F"
        border.width: 2
        anchors.centerIn: parent

        // X close button
        Rectangle {
            width: 28; height: 28; radius: 14
            color: xArea.containsMouse ? "#C0392B" : "#2A2A2E"
            border.color: "#5A6B8C"; border.width: 1
            anchors.top: parent.top; anchors.right: parent.right
            anchors.topMargin: 10; anchors.rightMargin: 10
            z: 10
            Behavior on color { ColorAnimation { duration: 120 } }
            Text { anchors.centerIn: parent; text: "✕"; color: "#FFFFFF"; font.pixelSize: 13 }
            MouseArea {
                id: xArea; anchors.fill: parent; hoverEnabled: true
                cursorShape: Qt.PointingHandCursor; onClicked: root.close()
            }
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 24
            spacing: 20

            Text {
                text: "Settings"
                color: "#FFFFFF"
                font.pixelSize: 22
                font.weight: Font.Bold
                Layout.alignment: Qt.AlignHCenter
            }

            // Separator
            Rectangle { Layout.fillWidth: true; height: 1; color: "#3E4E6F" }

            // Default Camera Configuration
            ColumnLayout {
                spacing: 8
                Layout.fillWidth: true

                Text {
                    text: "Default Camera Configuration"
                    color: "#BDC3C7"
                    font.pixelSize: 16
                    font.weight: Font.DemiBold
                }

                RowLayout {
                    spacing: 12; Layout.fillWidth: true

                    Text { text: "Pattern:"; color: "#BDC3C7"; font.pixelSize: 14; Layout.alignment: Qt.AlignVCenter }

                    ComboBox {
                        id: defaultCameraCombo
                        Layout.preferredWidth: 200
                        Layout.preferredHeight: 36
                        model: cameraPatterns
                        textRole: "name"
                        currentIndex: root.defaultCameraIndex
                        onCurrentIndexChanged: root.defaultCameraIndex = currentIndex
                    }
                }
            }

            // Separator
            Rectangle { Layout.fillWidth: true; height: 1; color: "#3E4E6F" }

            // Data Output Path
            ColumnLayout {
                spacing: 8
                Layout.fillWidth: true

                Text {
                    text: "Data Output Path"
                    color: "#BDC3C7"
                    font.pixelSize: 16
                    font.weight: Font.DemiBold
                }

                RowLayout {
                    spacing: 8; Layout.fillWidth: true

                    TextField {
                        id: dataPathField
                        text: root.dataOutputPath
                        readOnly: true
                        font.pixelSize: 13
                        color: "white"
                        Layout.fillWidth: true
                        background: Rectangle {
                            color: "#2E2E33"; radius: 4
                            border.color: "#3E4E6F"; border.width: 1
                        }
                    }

                    Button {
                        text: "Browse"
                        Layout.preferredWidth: 80; Layout.preferredHeight: 36
                        hoverEnabled: true
                        contentItem: Text {
                            text: parent.text; font.pixelSize: 13; color: "#BDC3C7"
                            horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                        }
                        background: Rectangle {
                            color: parent.hovered ? "#4A90E2" : "#3A3F4B"
                            border.color: parent.hovered ? "#FFFFFF" : "#BDC3C7"; radius: 4
                        }
                        onClicked: folderDialog.open()
                    }

                    Dialogs.FolderDialog {
                        id: folderDialog
                        title: "Select Data Output Directory"
                        currentFolder: Qt.platform.os === "windows"
                            ? "file:///" + dataPathField.text.replace("\\", "/")
                            : dataPathField.text
                        onAccepted: dataPathField.text = selectedFolder.toString().replace("file:///", "")
                    }
                }
            }

            // Separator
            Rectangle { Layout.fillWidth: true; height: 1; color: "#3E4E6F" }

            // Plot Display Mode
            ColumnLayout {
                spacing: 8
                Layout.fillWidth: true

                Text {
                    text: "Realtime Plot Display"
                    color: "#BDC3C7"
                    font.pixelSize: 16
                    font.weight: Font.DemiBold
                }

                RowLayout {
                    spacing: 16; Layout.alignment: Qt.AlignLeft

                    Text {
                        text: "Mean / StdDev"
                        color: !root.showBfiBvi ? "#4A90E2" : "#BDC3C7"
                        font.pixelSize: 14
                        font.weight: !root.showBfiBvi ? Font.Bold : Font.Normal
                    }

                    Switch {
                        checked: root.showBfiBvi
                        onCheckedChanged: root.showBfiBvi = checked
                    }

                    Text {
                        text: "BFI / BVI"
                        color: root.showBfiBvi ? "#4A90E2" : "#BDC3C7"
                        font.pixelSize: 14
                        font.weight: root.showBfiBvi ? Font.Bold : Font.Normal
                    }
                }
            }

            // Separator
            Rectangle { Layout.fillWidth: true; height: 1; color: "#3E4E6F" }

            // Plot Scaling
            ColumnLayout {
                spacing: 8
                Layout.fillWidth: true

                Text {
                    text: "Plot Scaling"
                    color: "#BDC3C7"
                    font.pixelSize: 16
                    font.weight: Font.DemiBold
                }

                RowLayout {
                    spacing: 16; Layout.alignment: Qt.AlignLeft

                    Text {
                        text: "Fixed"
                        color: !root.autoScale ? "#4A90E2" : "#BDC3C7"
                        font.pixelSize: 14
                        font.weight: !root.autoScale ? Font.Bold : Font.Normal
                    }

                    Switch {
                        checked: root.autoScale
                        onCheckedChanged: root.autoScale = checked
                    }

                    Text {
                        text: "Auto"
                        color: root.autoScale ? "#4A90E2" : "#BDC3C7"
                        font.pixelSize: 14
                        font.weight: root.autoScale ? Font.Bold : Font.Normal
                    }
                }

                // Fixed range inputs — only shown when autoscale is off
                GridLayout {
                    visible: !root.autoScale
                    columns: 3
                    columnSpacing: 10
                    rowSpacing: 6
                    Layout.fillWidth: true

                    // Header row
                    Item {}
                    Text { text: "Min"; color: "#7F8C8D"; font.pixelSize: 13; horizontalAlignment: Text.AlignHCenter; Layout.alignment: Qt.AlignHCenter }
                    Text { text: "Max"; color: "#7F8C8D"; font.pixelSize: 13; horizontalAlignment: Text.AlignHCenter; Layout.alignment: Qt.AlignHCenter }

                    // BFI row
                    Text { text: "BFI"; color: "#E74C3C"; font.pixelSize: 14; Layout.alignment: Qt.AlignVCenter }
                    TextField {
                        Layout.preferredWidth: 80; Layout.preferredHeight: 32
                        text: root.bfiMin.toFixed(1)
                        inputMethodHints: Qt.ImhFormattedNumbersOnly
                        font.pixelSize: 13; color: "white"; horizontalAlignment: Text.AlignHCenter
                        background: Rectangle { color: "#2E2E33"; radius: 4; border.color: "#3E4E6F"; border.width: 1 }
                        onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.bfiMin = v; text = root.bfiMin.toFixed(1) }
                    }
                    TextField {
                        Layout.preferredWidth: 80; Layout.preferredHeight: 32
                        text: root.bfiMax.toFixed(1)
                        inputMethodHints: Qt.ImhFormattedNumbersOnly
                        font.pixelSize: 13; color: "white"; horizontalAlignment: Text.AlignHCenter
                        background: Rectangle { color: "#2E2E33"; radius: 4; border.color: "#3E4E6F"; border.width: 1 }
                        onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.bfiMax = v; text = root.bfiMax.toFixed(1) }
                    }

                    // BVI row
                    Text { text: "BVI"; color: "#3498DB"; font.pixelSize: 14; Layout.alignment: Qt.AlignVCenter }
                    TextField {
                        Layout.preferredWidth: 80; Layout.preferredHeight: 32
                        text: root.bviMin.toFixed(1)
                        inputMethodHints: Qt.ImhFormattedNumbersOnly
                        font.pixelSize: 13; color: "white"; horizontalAlignment: Text.AlignHCenter
                        background: Rectangle { color: "#2E2E33"; radius: 4; border.color: "#3E4E6F"; border.width: 1 }
                        onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.bviMin = v; text = root.bviMin.toFixed(1) }
                    }
                    TextField {
                        Layout.preferredWidth: 80; Layout.preferredHeight: 32
                        text: root.bviMax.toFixed(1)
                        inputMethodHints: Qt.ImhFormattedNumbersOnly
                        font.pixelSize: 13; color: "white"; horizontalAlignment: Text.AlignHCenter
                        background: Rectangle { color: "#2E2E33"; radius: 4; border.color: "#3E4E6F"; border.width: 1 }
                        onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.bviMax = v; text = root.bviMax.toFixed(1) }
                    }
                }
            }

            Item { Layout.fillHeight: true }
        }

        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape) { root.close(); event.accepted = true }
        }
    }
}
