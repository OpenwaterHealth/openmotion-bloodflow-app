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

    // Settings values — initialised from live config on creation; open() re-syncs
    property int  defaultLeftMaskIndex:  4
    property int  defaultRightMaskIndex: 4
    property string dataOutputPath: MOTIONInterface.directory
    property bool showBfiBvi:  true
    property real bfiMin:      0.0
    property real bfiMax:      10.0
    property real bviMin:      0.0
    property real bviMax:      10.0
    property real meanMin:     0.0
    property real meanMax:     500.0
    property real contrastMin: 0.0
    property real contrastMax: 1.0

    signal settingsChanged()

    Component.onCompleted: {
        var cfg = MOTIONInterface.appConfig
        showBfiBvi   = cfg.showBfiBvi   !== undefined ? cfg.showBfiBvi   : true
        bfiMin       = cfg.bfiMin       !== undefined ? cfg.bfiMin       : 0.0
        bfiMax       = cfg.bfiMax       !== undefined ? cfg.bfiMax       : 10.0
        bviMin       = cfg.bviMin       !== undefined ? cfg.bviMin       : 0.0
        bviMax       = cfg.bviMax       !== undefined ? cfg.bviMax       : 10.0
        meanMin      = cfg.meanMin      !== undefined ? cfg.meanMin      : 0.0
        meanMax      = cfg.meanMax      !== undefined ? cfg.meanMax      : 500.0
        contrastMin  = cfg.contrastMin  !== undefined ? cfg.contrastMin  : 0.0
        contrastMax  = cfg.contrastMax  !== undefined ? cfg.contrastMax  : 1.0
    }

    function maskToIndex(mask) {
        for (var i = 0; i < cameraPatterns.count; i++) {
            if (parseInt(cameraPatterns.get(i).maskHex, 16) === mask) return i
        }
        return 4  // fall back to "Outer"
    }

    function maskFromIndex(index) {
        if (index < 0 || index >= cameraPatterns.count) return 0x99
        return parseInt(cameraPatterns.get(index).maskHex, 16)
    }

    function open() {
        var cfg = MOTIONInterface.appConfig
        defaultLeftMaskIndex  = maskToIndex(cfg.leftMask  !== undefined ? cfg.leftMask  : 0x99)
        defaultRightMaskIndex = maskToIndex(cfg.rightMask !== undefined ? cfg.rightMask : 0x99)
        showBfiBvi   = cfg.showBfiBvi   !== undefined ? cfg.showBfiBvi   : true
        bfiMin       = cfg.bfiMin       !== undefined ? cfg.bfiMin       : 0.0
        bfiMax       = cfg.bfiMax       !== undefined ? cfg.bfiMax       : 10.0
        bviMin       = cfg.bviMin       !== undefined ? cfg.bviMin       : 0.0
        bviMax       = cfg.bviMax       !== undefined ? cfg.bviMax       : 10.0
        meanMin      = cfg.meanMin      !== undefined ? cfg.meanMin      : 0.0
        meanMax      = cfg.meanMax      !== undefined ? cfg.meanMax      : 500.0
        contrastMin  = cfg.contrastMin  !== undefined ? cfg.contrastMin  : 0.0
        contrastMax  = cfg.contrastMax  !== undefined ? cfg.contrastMax  : 1.0
        dataPathField.text = MOTIONInterface.directory
        root.visible = true
    }
    function close() {
        MOTIONInterface.directory = dataPathField.text
        MOTIONInterface.saveConfigs({
            "leftMask":    maskFromIndex(defaultLeftMaskIndex),
            "rightMask":   maskFromIndex(defaultRightMaskIndex),
            "showBfiBvi":  showBfiBvi,
            "bfiMin":      bfiMin,
            "bfiMax":      bfiMax,
            "bviMin":      bviMin,
            "bviMax":      bviMax,
            "meanMin":     meanMin,
            "meanMax":     meanMax,
            "contrastMin": contrastMin,
            "contrastMax": contrastMax
        })
        settingsChanged()
        root.visible = false
    }

    ListModel {
        id: cameraPatterns
        ListElement { name: "None";      maskHex: "0x00" }
        ListElement { name: "Near";      maskHex: "0x5A" }
        ListElement { name: "Middle";    maskHex: "0x66" }
        ListElement { name: "Far";       maskHex: "0x55" }
        ListElement { name: "Outer";     maskHex: "0x99" }
        ListElement { name: "Left";      maskHex: "0x0F" }
        ListElement { name: "Right";     maskHex: "0xF0" }
        ListElement { name: "Third Row"; maskHex: "0x42" }
        ListElement { name: "All";       maskHex: "0xFF" }
    }

    // Dimmed backdrop
    Rectangle {
        anchors.fill: parent
        color: "#000000AA"
        MouseArea { anchors.fill: parent; onClicked: {} }
    }

    Rectangle {
        width: Math.min(parent.width - 80, 650)
        height: Math.min(parent.height - 40, 780)
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
            spacing: 0

            Text {
                text: "Settings"
                color: "#FFFFFF"
                font.pixelSize: 22
                font.weight: Font.Bold
                Layout.alignment: Qt.AlignHCenter
                Layout.bottomMargin: 16
            }

            // Separator
            Rectangle { Layout.fillWidth: true; height: 1; color: "#3E4E6F"; Layout.bottomMargin: 4 }

            ScrollView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

                ColumnLayout {
                    width: parent.width
                    spacing: 20

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

                    Text { text: "Left Sensor:"; color: "#BDC3C7"; font.pixelSize: 14; Layout.alignment: Qt.AlignVCenter; Layout.preferredWidth: 90 }

                    ComboBox {
                        id: defaultLeftCombo
                        Layout.preferredWidth: 160
                        Layout.preferredHeight: 36
                        model: cameraPatterns
                        textRole: "name"
                        currentIndex: root.defaultLeftMaskIndex
                        onCurrentIndexChanged: root.defaultLeftMaskIndex = currentIndex
                    }
                }

                RowLayout {
                    spacing: 12; Layout.fillWidth: true

                    Text { text: "Right Sensor:"; color: "#BDC3C7"; font.pixelSize: 14; Layout.alignment: Qt.AlignVCenter; Layout.preferredWidth: 90 }

                    ComboBox {
                        id: defaultRightCombo
                        Layout.preferredWidth: 160
                        Layout.preferredHeight: 36
                        model: cameraPatterns
                        textRole: "name"
                        currentIndex: root.defaultRightMaskIndex
                        onCurrentIndexChanged: root.defaultRightMaskIndex = currentIndex
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
                    text: "Plot Bounds"
                    color: "#BDC3C7"
                    font.pixelSize: 16
                    font.weight: Font.DemiBold
                }

                GridLayout {
                    columns: 3
                    columnSpacing: 10
                    rowSpacing: 6
                    Layout.fillWidth: true

                    Item {}
                    Text { text: "Min"; color: "#7F8C8D"; font.pixelSize: 13; horizontalAlignment: Text.AlignHCenter; Layout.alignment: Qt.AlignHCenter }
                    Text { text: "Max"; color: "#7F8C8D"; font.pixelSize: 13; horizontalAlignment: Text.AlignHCenter; Layout.alignment: Qt.AlignHCenter }

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

                    Text { text: "Mean"; color: "#2ECC71"; font.pixelSize: 14; Layout.alignment: Qt.AlignVCenter }
                    TextField {
                        Layout.preferredWidth: 80; Layout.preferredHeight: 32
                        text: root.meanMin.toFixed(0)
                        inputMethodHints: Qt.ImhFormattedNumbersOnly
                        font.pixelSize: 13; color: "white"; horizontalAlignment: Text.AlignHCenter
                        background: Rectangle { color: "#2E2E33"; radius: 4; border.color: "#3E4E6F"; border.width: 1 }
                        onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.meanMin = v; text = root.meanMin.toFixed(0) }
                    }
                    TextField {
                        Layout.preferredWidth: 80; Layout.preferredHeight: 32
                        text: root.meanMax.toFixed(0)
                        inputMethodHints: Qt.ImhFormattedNumbersOnly
                        font.pixelSize: 13; color: "white"; horizontalAlignment: Text.AlignHCenter
                        background: Rectangle { color: "#2E2E33"; radius: 4; border.color: "#3E4E6F"; border.width: 1 }
                        onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.meanMax = v; text = root.meanMax.toFixed(0) }
                    }

                    Text { text: "Contrast"; color: "#9B59B6"; font.pixelSize: 14; Layout.alignment: Qt.AlignVCenter }
                    TextField {
                        Layout.preferredWidth: 80; Layout.preferredHeight: 32
                        text: root.contrastMin.toFixed(2)
                        inputMethodHints: Qt.ImhFormattedNumbersOnly
                        font.pixelSize: 13; color: "white"; horizontalAlignment: Text.AlignHCenter
                        background: Rectangle { color: "#2E2E33"; radius: 4; border.color: "#3E4E6F"; border.width: 1 }
                        onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.contrastMin = v; text = root.contrastMin.toFixed(2) }
                    }
                    TextField {
                        Layout.preferredWidth: 80; Layout.preferredHeight: 32
                        text: root.contrastMax.toFixed(2)
                        inputMethodHints: Qt.ImhFormattedNumbersOnly
                        font.pixelSize: 13; color: "white"; horizontalAlignment: Text.AlignHCenter
                        background: Rectangle { color: "#2E2E33"; radius: 4; border.color: "#3E4E6F"; border.width: 1 }
                        onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.contrastMax = v; text = root.contrastMax.toFixed(2) }
                    }
                }
            }

            // Developer section — hidden unless developerMode is on
            ColumnLayout {
                spacing: 8
                Layout.fillWidth: true
                visible: MOTIONInterface.appConfig.developerMode ? true : false

                Rectangle { Layout.fillWidth: true; height: 1; color: "#3E4E6F" }

                Text {
                    text: "Developer"
                    color: "#BDC3C7"
                    font.pixelSize: 16
                    font.weight: Font.DemiBold
                }

                Button {
                    text: "Soft Reset Console"
                    Layout.preferredHeight: 36
                    hoverEnabled: true
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13; color: "#BDC3C7"
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? "#E67E22" : "#3A3F4B"
                        border.color: parent.hovered ? "#FFFFFF" : "#BDC3C7"; radius: 4
                    }
                    onClicked: MOTIONInterface.softResetSensor("CONSOLE")
                }
            }

            // Separator
            Rectangle { Layout.fillWidth: true; height: 1; color: "#3E4E6F" }

            // Version Info
            ColumnLayout {
                spacing: 4
                Layout.fillWidth: true

                Text {
                    text: "Version Info"
                    color: "#BDC3C7"
                    font.pixelSize: 16
                    font.weight: Font.DemiBold
                }

                Text {
                    text: "APP: " + appVersion
                    color: "#AAAAAA"
                    font.pixelSize: 13
                }

                Text {
                    text: "SDK: " + MOTIONInterface.get_sdk_version()
                    color: "#AAAAAA"
                    font.pixelSize: 13
                }
            }

                    Item { height: 8 }

                    } // inner ColumnLayout
                } // ScrollView

        } // outer ColumnLayout

        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape) { root.close(); event.accepted = true }
        }
    }
}
