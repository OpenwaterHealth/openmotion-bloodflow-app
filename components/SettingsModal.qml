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
    property bool   writeRawCsv:       false
    property var    rawCsvDurationSec: 60

    // ── Theme tokens (aliased from AppTheme) ──────────────────────────────
    readonly property color colBgPanel:    theme.bgContainer
    readonly property color colBgCard:     theme.bgCard
    readonly property color colBgInput:    theme.bgInput
    readonly property color colBorder:     theme.borderStrong
    readonly property color colBorderSoft: theme.borderSoft
    readonly property color colAccent:     theme.accentBlue
    readonly property color colTextPri:    theme.textPrimary
    readonly property color colTextSec:    theme.textSecondary
    readonly property color colTextMuted:  theme.textTertiary

    signal settingsChanged()

    // ── Lifecycle ───────────────────────────────────────────────────────────
    function _loadFromConfig() {
        var cfg = MOTIONInterface.appConfig
        defaultLeftMaskIndex  = maskToIndex(cfg.leftMask  !== undefined ? cfg.leftMask  : 0x99)
        defaultRightMaskIndex = maskToIndex(cfg.rightMask !== undefined ? cfg.rightMask : 0x99)
        reducedMode        = cfg.reducedMode        !== undefined ? cfg.reducedMode        : false
        showBfiBvi         = reducedMode ? true : (cfg.showBfiBvi !== undefined ? cfg.showBfiBvi : true)
        autoScale          = cfg.autoScale          !== undefined ? cfg.autoScale          : false
        autoScalePerPlot   = autoScale
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
        writeRawCsv       = cfg.writeRawCsv       !== undefined ? cfg.writeRawCsv       : false
        rawCsvDurationSec = cfg.rawCsvDurationSec !== undefined ? cfg.rawCsvDurationSec : null
        if (darkModeSwitch) darkModeSwitch.checked = cfg.darkMode !== false
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
        MOTIONInterface.setWriteRawCsv(writeRawCsv)
        MOTIONInterface.setRawCsvDurationSec(rawCsvDurationSec)
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
        Layout.leftMargin: 20
        Layout.rightMargin: 20
        color:        root.colBgCard
        radius:       10
        border.color: root.colBorderSoft
        border.width: 1
        implicitHeight: cardCol.implicitHeight + 36

        ColumnLayout {
            id: cardCol
            anchors.fill: parent
            anchors.margins: 18
            spacing: 14

            Text {
                text:           parent.parent.title
                color:          root.colTextPri
                font.pixelSize: 15
                font.weight:    Font.DemiBold
                font.letterSpacing: 0.3
            }

            Rectangle { Layout.fillWidth: true; height: 1; color: root.colBorderSoft }

            ColumnLayout {
                id: cardContent
                Layout.fillWidth: true
                spacing: 12
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
            Layout.preferredWidth: 140
            Layout.minimumWidth: 140
        }
    }

    component StyledCombo: ComboBox {
        id: styledComboCtrl
        Layout.preferredWidth: 180
        Layout.preferredHeight: 32
        font.pixelSize: 13
        contentItem: Text {
            leftPadding: 10
            text:  styledComboCtrl.displayText
            font:  styledComboCtrl.font
            color: root.colTextPri
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        background: Rectangle {
            color: root.colBgInput
            radius: 4
            border.color: styledComboCtrl.activeFocus ? root.colAccent : root.colBorderSoft
            border.width: 1
        }
        indicator: Text {
            x:    styledComboCtrl.width - width - 10
            y:    (styledComboCtrl.height - height) / 2
            text: "\u25BE"
            font.pixelSize: 14
            color: root.colTextSec
        }
        popup: Popup {
            y: styledComboCtrl.height
            width: styledComboCtrl.width
            implicitHeight: contentItem.implicitHeight + 2
            padding: 1
            contentItem: ListView {
                clip: true
                implicitHeight: contentHeight
                model: styledComboCtrl.delegateModel
                ScrollIndicator.vertical: ScrollIndicator {}
            }
            background: Rectangle {
                color: root.colBgCard
                radius: 4
                border.color: root.colBorderSoft
                border.width: 1
            }
        }
        delegate: ItemDelegate {
            width: styledComboCtrl.width
            height: 30
            contentItem: Text {
                text: modelData !== undefined ? modelData : (model.name !== undefined ? model.name : "")
                font.pixelSize: 13
                color: root.colTextPri
                verticalAlignment: Text.AlignVCenter
                leftPadding: 8
            }
            background: Rectangle {
                color: highlighted ? root.colAccent : "transparent"
            }
            highlighted: styledComboCtrl.currentIndex === index
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
        id: pillCtrl
        scale: 0.9
        indicator: Rectangle {
            x:      pillCtrl.leftPadding
            y:      (pillCtrl.height - height) / 2
            width:  44; height: 24; radius: 12
            color:  pillCtrl.checked ? root.colAccent : root.colBgInput
            border.color: pillCtrl.checked ? root.colAccent : root.colBorderSoft
            border.width: 1
            Behavior on color { ColorAnimation { duration: 120 } }

            Rectangle {
                x:      pillCtrl.checked ? parent.width - width - 3 : 3
                y:      3; width: 18; height: 18; radius: 9
                color:  "#FFFFFF"
                Behavior on x { NumberAnimation { duration: 120 } }
            }
        }
    }

    component ActionButton: Button {
        property color hoverColor: root.colAccent
        Layout.preferredHeight: 30
        hoverEnabled: true
        contentItem: Text {
            text:                parent.text
            font.pixelSize:      12
            color:               parent.hovered ? "#FFFFFF" : root.colTextSec
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment:   Text.AlignVCenter
        }
        background: Rectangle {
            color:        parent.hovered ? parent.hoverColor : root.colBgInput
            radius:       4
            border.color: parent.hovered ? root.colAccent : root.colBorderSoft
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
                color: xArea.containsMouse ? "#C0392B" : root.colBorder
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
                spacing: 12

                // Top padding
                Item { Layout.fillWidth: true; height: 8 }

                // ── Default Camera Configuration ─────────────────────────────
                SectionCard {
                    visible: !root.reducedMode
                    title: "Default Camera Configuration"

                    FieldRow {
                        label: "Left Sensor"
                        StyledCombo {
                            model: cameraPatterns
                            textRole: "name"
                            currentIndex: root.defaultLeftMaskIndex
                            onCurrentIndexChanged: root.defaultLeftMaskIndex = currentIndex
                        }
                        Item { Layout.fillWidth: true }
                    }
                    FieldRow {
                        label: "Right Sensor"
                        StyledCombo {
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
                        visible: !root.reducedMode
                        label: "Display mode"
                        Text {
                            text: "Mean / C"
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
                        StyledCombo {
                            id: windowCombo
                            Layout.preferredWidth: 110
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
                        visible: MOTIONInterface.appConfig.developerMode ? true : false
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
                        columns: 4
                        columnSpacing: 14
                        rowSpacing: 10
                        Layout.fillWidth: true

                        // Header row
                        Item { Layout.preferredWidth: 80 }
                        Text { text: "Min"; color: root.colTextMuted; font.pixelSize: 12; font.weight: Font.DemiBold; Layout.alignment: Qt.AlignHCenter; Layout.preferredWidth: 90 }
                        Text { text: "Max"; color: root.colTextMuted; font.pixelSize: 12; font.weight: Font.DemiBold; Layout.alignment: Qt.AlignHCenter; Layout.preferredWidth: 90 }
                        Item { Layout.fillWidth: true }

                        Text { text: "BFI"; color: "#E74C3C"; font.pixelSize: 13; font.weight: Font.DemiBold; Layout.preferredWidth: 80 }
                        StyledNumberField {
                            Layout.preferredWidth: 90
                            text: root.bfiMin.toFixed(1)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.bfiMin = v; text = root.bfiMin.toFixed(1) }
                        }
                        StyledNumberField {
                            Layout.preferredWidth: 90
                            text: root.bfiMax.toFixed(1)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.bfiMax = v; text = root.bfiMax.toFixed(1) }
                        }
                        Item { Layout.fillWidth: true }

                        Text { text: "BVI"; color: "#3498DB"; font.pixelSize: 13; font.weight: Font.DemiBold; Layout.preferredWidth: 80 }
                        StyledNumberField {
                            Layout.preferredWidth: 90
                            text: root.bviMin.toFixed(1)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.bviMin = v; text = root.bviMin.toFixed(1) }
                        }
                        StyledNumberField {
                            Layout.preferredWidth: 90
                            text: root.bviMax.toFixed(1)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.bviMax = v; text = root.bviMax.toFixed(1) }
                        }
                        Item { Layout.fillWidth: true }

                        Text { text: "Mean"; color: "#2ECC71"; font.pixelSize: 13; font.weight: Font.DemiBold; Layout.preferredWidth: 80 }
                        StyledNumberField {
                            Layout.preferredWidth: 90
                            text: root.meanMin.toFixed(0)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.meanMin = v; text = root.meanMin.toFixed(0) }
                        }
                        StyledNumberField {
                            Layout.preferredWidth: 90
                            text: root.meanMax.toFixed(0)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.meanMax = v; text = root.meanMax.toFixed(0) }
                        }
                        Item { Layout.fillWidth: true }

                        Text { text: "Contrast"; color: "#9B59B6"; font.pixelSize: 13; font.weight: Font.DemiBold; Layout.preferredWidth: 80 }
                        StyledNumberField {
                            Layout.preferredWidth: 90
                            text: root.contrastMin.toFixed(2)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.contrastMin = v; text = root.contrastMin.toFixed(2) }
                        }
                        StyledNumberField {
                            Layout.preferredWidth: 90
                            text: root.contrastMax.toFixed(2)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.contrastMax = v; text = root.contrastMax.toFixed(2) }
                        }
                        Item { Layout.fillWidth: true }
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
                        Item { Layout.fillWidth: true }
                    }
                    Text {
                        text: "Simplified clinical view: forces Middle camera configuration, enables free run mode, hides scan settings, and shows large left/right BFI and BVI panels."
                        color: root.colTextMuted
                        font.pixelSize: 11
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                    }
                }

                // ── Appearance ───────────────────────────────────────────────
                SectionCard {
                    title: "Appearance"
                    ColumnLayout {
                        width: parent.width
                        spacing: 0
                        FieldRow {
                            label: "Dark Mode"
                            PillSwitch {
                                id: darkModeSwitch
                                checked: MOTIONInterface.appConfig.darkMode !== false
                                onToggled: {
                                    MOTIONInterface.setConfig("darkMode", checked)
                                }
                            }
                        }
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

                    FieldRow {
                        label: "Save raw CSV"
                        PillSwitch {
                            checked: root.writeRawCsv
                            onCheckedChanged: root.writeRawCsv = checked
                        }
                        Text {
                            text: root.writeRawCsv ? "On" : "Off"
                            color: root.writeRawCsv ? root.colAccent : root.colTextMuted
                            font.pixelSize: 12
                        }
                        Item { Layout.fillWidth: true }
                    }

                    FieldRow {
                        label: "Raw CSV duration"
                        opacity: root.writeRawCsv ? 1.0 : 0.4
                        TextField {
                            id: rawCsvDurationField
                            Layout.preferredWidth: 80
                            Layout.preferredHeight: 32
                            enabled: root.writeRawCsv
                            text: root.rawCsvDurationSec !== null && root.rawCsvDurationSec !== undefined
                                  ? root.rawCsvDurationSec.toString() : ""
                            placeholderText: "unlimited"
                            inputMethodHints: Qt.ImhDigitsOnly
                            color: root.colTextPri
                            background: Rectangle {
                                color: root.colBgInput
                                border.color: rawCsvDurationField.activeFocus ? root.colAccent : root.colBorderSoft
                                radius: 4
                            }
                            onEditingFinished: {
                                var v = parseInt(text, 10)
                                root.rawCsvDurationSec = (text === "" || isNaN(v) || v <= 0) ? null : v
                            }
                        }
                        Text {
                            text: "seconds  (blank = full scan)"
                            color: root.colTextMuted
                            font.pixelSize: 11
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
                    FieldRow {
                        label: "Updates"
                        ActionButton {
                            id: updateCheckBtn
                            text: "Check for Updates"
                            Layout.preferredWidth: 150
                            onClicked: {
                                updateCheckBtn.text = "Checking..."
                                updateCheckBtn.enabled = false
                                MOTIONInterface.checkForUpdates()
                            }
                        }
                        Text {
                            id: updateStatusText
                            text: ""
                            color: root.colTextMuted
                            font.pixelSize: 12
                        }
                        Item { Layout.fillWidth: true }
                    }

                    Connections {
                        target: MOTIONInterface
                        function onUpdateAvailable(version, url) {
                            updateCheckBtn.text = "Check for Updates"
                            updateCheckBtn.enabled = true
                            updateStatusText.text = "v" + version + " available!"
                            updateStatusText.color = root.colAccent
                        }
                        function onUpdateNotAvailable() {
                            updateCheckBtn.text = "Check for Updates"
                            updateCheckBtn.enabled = true
                            updateStatusText.text = "Up to date"
                            updateStatusText.color = theme.statusGreen
                        }
                        function onUpdateCheckFailed(msg) {
                            updateCheckBtn.text = "Check for Updates"
                            updateCheckBtn.enabled = true
                            updateStatusText.text = "Check failed"
                            updateStatusText.color = theme.accentRed
                        }
                    }
                }

                // Bottom padding
                Item { Layout.fillWidth: true; height: 20 }
            }
        }

        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape) { root.close(); event.accepted = true }
        }
    }
}
