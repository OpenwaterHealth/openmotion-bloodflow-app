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
        height: Math.min(parent.height - 80, 480)
        radius: 12
        color: "#1E1E20"
        border.color: "#3E4E6F"
        border.width: 2
        anchors.centerIn: parent

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

            Item { Layout.fillHeight: true }

            // Close button
            Button {
                text: "Save & Close"
                Layout.alignment: Qt.AlignHCenter
                Layout.preferredWidth: 130; Layout.preferredHeight: 40
                hoverEnabled: true
                contentItem: Text {
                    text: parent.text; font.pixelSize: 14; color: "#FFFFFF"
                    horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                }
                background: Rectangle {
                    color: parent.hovered ? "#4A90E2" : "#3A3F4B"
                    border.color: parent.hovered ? "#FFFFFF" : "#BDC3C7"; radius: 6
                }
                onClicked: root.close()
            }
        }

        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape) { root.close(); event.accepted = true }
        }
    }
}
