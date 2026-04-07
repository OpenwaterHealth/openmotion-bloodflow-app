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

    // ── Settings values — initialised from live config on creation ──────────
    property int    defaultLeftMaskIndex:  4
    property int    defaultRightMaskIndex: 4
    property string dataOutputPath: MOTIONInterface.directory
    property bool   showBfiBvi:        true
    property bool   autoScale:         false
    property bool   autoScalePerPlot:  false
    property bool   reducedMode:       false
    property int    plotWindowSec:     15
    property color  bfiColor:          "#E74C3C"
    property color  bviColor:          "#3498DB"
    property bool   bviLowPassEnabled:  false
    property real   bviLowPassCutoffHz: 40.0
    property real   bfiMin:      0.0
    property real   bfiMax:      10.0
    property real   bviMin:      0.0
    property real   bviMax:      10.0
    property real   meanMin:     0.0
    property real   meanMax:     500.0
    property real   contrastMin: 0.0
    property real   contrastMax: 1.0

    // ── Theme tokens ────────────────────────────────────────────────────────
    readonly property color colBgPanel:    "#1E1E20"
    readonly property color colBgCard:     "#26262A"
    readonly property color colBgInput:    "#2E2E33"
    readonly property color colBorder:     "#3E4E6F"
    readonly property color colBorderSoft: "#33384A"
    readonly property color colAccent:     "#4A90E2"
    readonly property color colTextPri:    "#E6E6E6"
    readonly property color colTextSec:    "#9CA3AF"
    readonly property color colTextMuted:  "#6B7280"

    signal settingsChanged()

    // ── Lifecycle ───────────────────────────────────────────────────────────
    function _loadFromConfig() {
        var cfg = MOTIONInterface.appConfig
        defaultLeftMaskIndex  = maskToIndex(cfg.leftMask  !== undefined ? cfg.leftMask  : 0x99)
        defaultRightMaskIndex = maskToIndex(cfg.rightMask !== undefined ? cfg.rightMask : 0x99)
        showBfiBvi         = cfg.showBfiBvi         !== undefined ? cfg.showBfiBvi         : true
        autoScale          = cfg.autoScale          !== undefined ? cfg.autoScale          : false
        autoScalePerPlot   = autoScale
        reducedMode        = cfg.reducedMode        !== undefined ? cfg.reducedMode        : false
        plotWindowSec      = cfg.plotWindowSec      !== undefined ? cfg.plotWindowSec      : 15
        bfiColor           = cfg.bfiColor           !== undefined ? cfg.bfiColor           : "#E74C3C"
        bviColor           = cfg.bviColor           !== undefined ? cfg.bviColor           : "#3498DB"
        bviLowPassEnabled  = cfg.bviLowPassEnabled  !== undefined ? cfg.bviLowPassEnabled  : false
        bviLowPassCutoffHz = cfg.bviLowPassCutoffHz !== undefined ? cfg.bviLowPassCutoffHz : 40.0
        bfiMin       = cfg.bfiMin       !== undefined ? cfg.bfiMin       : 0.0
        bfiMax       = cfg.bfiMax       !== undefined ? cfg.bfiMax       : 10.0
        bviMin       = cfg.bviMin       !== undefined ? cfg.bviMin       : 0.0
        bviMax       = cfg.bviMax       !== undefined ? cfg.bviMax       : 10.0
        meanMin      = cfg.meanMin      !== undefined ? cfg.meanMin      : 0.0
        meanMax      = cfg.meanMax      !== undefined ? cfg.meanMax      : 500.0
        contrastMin  = cfg.contrastMin  !== undefined ? cfg.contrastMin  : 0.0
        contrastMax  = cfg.contrastMax  !== undefined ? cfg.contrastMax  : 1.0
    }

    Component.onCompleted: _loadFromConfig()

    function maskToIndex(mask) {
        for (var i = 0; i < cameraPatterns.count; i++) {
            if (parseInt(cameraPatterns.get(i).maskHex, 16) === mask) return i
        }
        return 4
    }
    function maskFromIndex(index) {
        if (index < 0 || index >= cameraPatterns.count) return 0x99
        return parseInt(cameraPatterns.get(index).maskHex, 16)
    }

    function open() {
        _loadFromConfig()
        dataPathField.text = MOTIONInterface.directory
        root.visible = true
    }
    function close() {
        MOTIONInterface.directory = dataPathField.text
        MOTIONInterface.saveConfigs({
            "leftMask":           maskFromIndex(defaultLeftMaskIndex),
            "rightMask":          maskFromIndex(defaultRightMaskIndex),
            "showBfiBvi":         showBfiBvi,
            "autoScale":          autoScale,
            "autoScalePerPlot":   autoScalePerPlot,
            "reducedMode":        reducedMode,
            "plotWindowSec":      plotWindowSec,
            "bfiColor":           "" + bfiColor,
            "bviColor":           "" + bviColor,
            "bviLowPassEnabled":  bviLowPassEnabled,
            "bviLowPassCutoffHz": bviLowPassCutoffHz,
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

    Dialogs.ColorDialog {
        id: bfiColorDialog
        title: "Select BFI trace color"
        onAccepted: root.bfiColor = selectedColor
    }
    Dialogs.ColorDialog {
        id: bviColorDialog
        title: "Select BVI trace color"
        onAccepted: root.bviColor = selectedColor
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

    // ── Reusable building blocks ────────────────────────────────────────────
    component SectionCard: Rectangle {
        property string title: ""
        default property alias contentItem: cardContent.data
        Layout.fillWidth: true
        color:        root.colBgCard
        radius:       8
        border.color: root.colBorderSoft
        border.width: 1
        implicitHeight: cardCol.implicitHeight + 28

        ColumnLayout {
            id: cardCol
            anchors.fill: parent
            anchors.margins: 14
            spacing: 12

            Text {
                text:           parent.parent.title
                color:          root.colTextPri
                font.pixelSize: 14
                font.weight:    Font.DemiBold
                font.letterSpacing: 0.3
            }

            Rectangle { Layout.fillWidth: true; height: 1; color: root.colBorderSoft }

            ColumnLayout {
                id: cardContent
                Layout.fillWidth: true
                spacing: 10
            }
        }
    }

    component FieldRow: RowLayout {
        property string label: ""
        Layout.fillWidth: true
        spacing: 12
        Text {
            text:           parent.label
            color:          root.colTextSec
            font.pixelSize: 13
            Layout.preferredWidth: 150
        }
    }

    component StyledNumberField: TextField {
        Layout.preferredWidth: 84
        Layout.preferredHeight: 30
        font.pixelSize: 13
        color: root.colTextPri
        horizontalAlignment: Text.AlignHCenter
        inputMethodHints: Qt.ImhFormattedNumbersOnly
        background: Rectangle {
            color: root.colBgInput
            radius: 4
            border.color: parent.activeFocus ? root.colAccent : root.colBorderSoft
            border.width: 1
        }
    }

    component PillSwitch: Switch {
        // Slimmer, neutral switch
        scale: 0.9
    }

    component ActionButton: Button {
        property color hoverColor: root.colAccent
        Layout.preferredHeight: 30
        hoverEnabled: true
        contentItem: Text {
            text:                parent.text
            font.pixelSize:      12
            color:               parent.hovered ? "#FFFFFF" : root.colTextPri
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment:   Text.AlignVCenter
        }
        background: Rectangle {
            color:        parent.hovered ? parent.hoverColor : "#3A3F4B"
            radius:       4
            border.color: parent.hovered ? "#FFFFFF" : root.colBorderSoft
            border.width: 1
        }
    }

    // ── Backdrop ────────────────────────────────────────────────────────────
    Rectangle {
        anchors.fill: parent
        color: "#000000B0"
        MouseArea { anchors.fill: parent; onClicked: {} }
    }

    // ── Modal panel ─────────────────────────────────────────────────────────
    Rectangle {
        id: panel
        width:  Math.min(parent.width - 80, 680)
        height: Math.min(parent.height - 40, 800)
        radius: 14
        color:  root.colBgPanel
        border.color: root.colBorder
        border.width: 1
        anchors.centerIn: parent

        // Title bar
        Rectangle {
            id: titleBar
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: parent.right
            height: 56
            color: "transparent"

            Text {
                anchors.left: parent.left
                anchors.leftMargin: 24
                anchors.verticalCenter: parent.verticalCenter
                text: "Settings"
                color: root.colTextPri
                font.pixelSize: 20
                font.weight: Font.DemiBold
                font.letterSpacing: 0.3
            }

            Rectangle {
                width: 30; height: 30; radius: 15
                color: xArea.containsMouse ? "#C0392B" : "#2A2A2E"
                border.color: root.colBorderSoft; border.width: 1
                anchors.right: parent.right
                anchors.rightMargin: 14
                anchors.verticalCenter: parent.verticalCenter
                Behavior on color { ColorAnimation { duration: 120 } }
                Text { anchors.centerIn: parent; text: "✕"; color: "#FFFFFF"; font.pixelSize: 13 }
                MouseArea {
                    id: xArea; anchors.fill: parent; hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor; onClicked: root.close()
                }
            }

            Rectangle {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                height: 1
                color: root.colBorderSoft
            }
        }

        // Content
        ScrollView {
            id: scroller
            anchors.top: titleBar.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            anchors.margins: 0
            anchors.topMargin: 0
            clip: true
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

            ColumnLayout {
                width: scroller.availableWidth
                spacing: 14
                anchors.margins: 20

                // Top padding
                Item { Layout.fillWidth: true; height: 6 }

                // ── Default Camera Configuration ─────────────────────────────
                SectionCard {
                    title: "Default Camera Configuration"

                    FieldRow {
                        label: "Left Sensor"
                        ComboBox {
                            Layout.preferredWidth: 180
                            Layout.preferredHeight: 32
                            model: cameraPatterns
                            textRole: "name"
                            currentIndex: root.defaultLeftMaskIndex
                            onCurrentIndexChanged: root.defaultLeftMaskIndex = currentIndex
                        }
                        Item { Layout.fillWidth: true }
                    }
                    FieldRow {
                        label: "Right Sensor"
                        ComboBox {
                            Layout.preferredWidth: 180
                            Layout.preferredHeight: 32
                            model: cameraPatterns
                            textRole: "name"
                            currentIndex: root.defaultRightMaskIndex
                            onCurrentIndexChanged: root.defaultRightMaskIndex = currentIndex
                        }
                        Item { Layout.fillWidth: true }
                    }
                }

                // ── Data Output ──────────────────────────────────────────────
                SectionCard {
                    title: "Data Output"

                    FieldRow {
                        label: "Output Folder"
                        TextField {
                            id: dataPathField
                            text: root.dataOutputPath
                            readOnly: true
                            font.pixelSize: 12
                            color: root.colTextPri
                            Layout.fillWidth: true
                            Layout.preferredHeight: 30
                            background: Rectangle {
                                color: root.colBgInput; radius: 4
                                border.color: root.colBorderSoft; border.width: 1
                            }
                        }
                        ActionButton {
                            text: "Browse"
                            Layout.preferredWidth: 80
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

                // ── Realtime Plot Display ────────────────────────────────────
                SectionCard {
                    title: "Realtime Plot Display"

                    FieldRow {
                        label: "Display mode"
                        Text {
                            text: "Mean / σ"
                            color: !root.showBfiBvi ? root.colAccent : root.colTextSec
                            font.pixelSize: 13
                            font.weight: !root.showBfiBvi ? Font.DemiBold : Font.Normal
                        }
                        PillSwitch {
                            checked: root.showBfiBvi
                            onCheckedChanged: root.showBfiBvi = checked
                        }
                        Text {
                            text: "BFI / BVI"
                            color: root.showBfiBvi ? root.colAccent : root.colTextSec
                            font.pixelSize: 13
                            font.weight: root.showBfiBvi ? Font.DemiBold : Font.Normal
                        }
                        Item { Layout.fillWidth: true }
                    }

                    FieldRow {
                        label: "Time window"
                        ComboBox {
                            id: windowCombo
                            Layout.preferredWidth: 110
                            Layout.preferredHeight: 32
                            model: [3, 5, 15, 30]
                            displayText: currentValue + " s"
                            currentIndex: {
                                var idx = model.indexOf(root.plotWindowSec)
                                return idx >= 0 ? idx : 2
                            }
                            onActivated: root.plotWindowSec = model[currentIndex]
                        }
                        Item { Layout.fillWidth: true }
                    }

                    FieldRow {
                        label: "Auto-scale Y-axes"
                        PillSwitch {
                            checked: root.autoScale
                            onCheckedChanged: {
                                root.autoScale = checked
                                root.autoScalePerPlot = checked
                            }
                        }
                        Text {
                            text: root.autoScale ? "On" : "Off"
                            color: root.autoScale ? root.colAccent : root.colTextMuted
                            font.pixelSize: 12
                        }
                        Item { Layout.fillWidth: true }
                    }

                    FieldRow {
                        label: "BVI low-pass filter"
                        PillSwitch {
                            checked: root.bviLowPassEnabled
                            onCheckedChanged: root.bviLowPassEnabled = checked
                        }
                        Text {
                            text: root.bviLowPassCutoffHz.toFixed(0) + " Hz cutoff"
                            color: root.colTextMuted
                            font.pixelSize: 12
                        }
                        Item { Layout.fillWidth: true }
                    }

                    FieldRow {
                        label: "Trace colors"
                        Rectangle {
                            width: 26; height: 26; radius: 4
                            color: root.bfiColor
                            border.color: root.colBorderSoft; border.width: 1
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: { bfiColorDialog.selectedColor = root.bfiColor; bfiColorDialog.open() }
                            }
                        }
                        Text { text: "BFI"; color: root.colTextSec; font.pixelSize: 12 }
                        Item { width: 8 }
                        Rectangle {
                            width: 26; height: 26; radius: 4
                            color: root.bviColor
                            border.color: root.colBorderSoft; border.width: 1
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: { bviColorDialog.selectedColor = root.bviColor; bviColorDialog.open() }
                            }
                        }
                        Text { text: "BVI"; color: root.colTextSec; font.pixelSize: 12 }
                        Item { Layout.fillWidth: true }
                        ActionButton {
                            text: "Reset"
                            Layout.preferredWidth: 70
                            onClicked: { root.bfiColor = "#E74C3C"; root.bviColor = "#3498DB" }
                        }
                    }
                }

                // ── Plot Bounds ──────────────────────────────────────────────
                SectionCard {
                    title: "Manual Plot Bounds"

                    Text {
                        text: "Used when auto-scale is off."
                        color: root.colTextMuted
                        font.pixelSize: 11
                        font.italic: true
                        Layout.bottomMargin: 4
                    }

                    GridLayout {
                        columns: 5
                        columnSpacing: 12
                        rowSpacing: 8
                        Layout.fillWidth: true

                        Item { Layout.preferredWidth: 80 }
                        Text { text: "Min"; color: root.colTextMuted; font.pixelSize: 12; Layout.alignment: Qt.AlignHCenter; Layout.preferredWidth: 84 }
                        Text { text: "Max"; color: root.colTextMuted; font.pixelSize: 12; Layout.alignment: Qt.AlignHCenter; Layout.preferredWidth: 84 }
                        Item { Layout.fillWidth: true }
                        Item { Layout.preferredWidth: 1 }

                        Text { text: "BFI"; color: "#E74C3C"; font.pixelSize: 13; font.weight: Font.DemiBold; Layout.preferredWidth: 80 }
                        StyledNumberField {
                            text: root.bfiMin.toFixed(1)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.bfiMin = v; text = root.bfiMin.toFixed(1) }
                        }
                        StyledNumberField {
                            text: root.bfiMax.toFixed(1)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.bfiMax = v; text = root.bfiMax.toFixed(1) }
                        }
                        Item { Layout.fillWidth: true }
                        Item { Layout.preferredWidth: 1 }

                        Text { text: "BVI"; color: "#3498DB"; font.pixelSize: 13; font.weight: Font.DemiBold; Layout.preferredWidth: 80 }
                        StyledNumberField {
                            text: root.bviMin.toFixed(1)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.bviMin = v; text = root.bviMin.toFixed(1) }
                        }
                        StyledNumberField {
                            text: root.bviMax.toFixed(1)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.bviMax = v; text = root.bviMax.toFixed(1) }
                        }
                        Item { Layout.fillWidth: true }
                        Item { Layout.preferredWidth: 1 }

                        Text { text: "Mean"; color: "#2ECC71"; font.pixelSize: 13; font.weight: Font.DemiBold; Layout.preferredWidth: 80 }
                        StyledNumberField {
                            text: root.meanMin.toFixed(0)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.meanMin = v; text = root.meanMin.toFixed(0) }
                        }
                        StyledNumberField {
                            text: root.meanMax.toFixed(0)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.meanMax = v; text = root.meanMax.toFixed(0) }
                        }
                        Item { Layout.fillWidth: true }
                        Item { Layout.preferredWidth: 1 }

                        Text { text: "Contrast"; color: "#9B59B6"; font.pixelSize: 13; font.weight: Font.DemiBold; Layout.preferredWidth: 80 }
                        StyledNumberField {
                            text: root.contrastMin.toFixed(2)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.contrastMin = v; text = root.contrastMin.toFixed(2) }
                        }
                        StyledNumberField {
                            text: root.contrastMax.toFixed(2)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.contrastMax = v; text = root.contrastMax.toFixed(2) }
                        }
                        Item { Layout.fillWidth: true }
                        Item { Layout.preferredWidth: 1 }
                    }
                }

                // ── Reduced Mode ─────────────────────────────────────────────
                SectionCard {
                    title: "Reduced Mode"

                    FieldRow {
                        label: "Enable"
                        PillSwitch {
                            checked: root.reducedMode
                            onCheckedChanged: root.reducedMode = checked
                        }
                        Text {
                            text: root.reducedMode ? "On" : "Off"
                            color: root.reducedMode ? root.colAccent : root.colTextMuted
                            font.pixelSize: 12
                        }
                        Item { Layout.fillWidth: true }
                    }
                    Text {
                        text: "Restart the app for Reduced Mode changes to take effect."
                        color: root.colTextMuted
                        font.pixelSize: 11
                        font.italic: true
                    }
                }

                // ── Developer ────────────────────────────────────────────────
                SectionCard {
                    title: "Developer"
                    visible: MOTIONInterface.appConfig.developerMode ? true : false

                    FieldRow {
                        label: "Console"
                        ActionButton {
                            text: "Soft Reset"
                            Layout.preferredWidth: 110
                            hoverColor: "#E67E22"
                            onClicked: MOTIONInterface.softResetSensor("CONSOLE")
                        }
                        Item { Layout.fillWidth: true }
                    }
                }

                // ── Version Info ─────────────────────────────────────────────
                SectionCard {
                    title: "About"

                    FieldRow {
                        label: "Application"
                        Text { text: appVersion; color: root.colTextPri; font.pixelSize: 13; font.family: "Consolas" }
                        Item { Layout.fillWidth: true }
                    }
                    FieldRow {
                        label: "SDK"
                        Text { text: MOTIONInterface.get_sdk_version(); color: root.colTextPri; font.pixelSize: 13; font.family: "Consolas" }
                        Item { Layout.fillWidth: true }
                    }
                }

                // Bottom padding
                Item { Layout.fillWidth: true; height: 14 }
            }
        }

        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape) { root.close(); event.accepted = true }
        }
    }
}
