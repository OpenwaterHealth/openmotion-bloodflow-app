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


    // Modal interface — see HistoryModal.qml for rationale.
    readonly property string label: "Settings"

    // Left-edge space to keep clear of the icon bar (BloodFlow's
    // ButtonPanel — 80px wide + 8px margin, pinned left at z:10000, which
    // is ABOVE this modal's z:9998). The card is centered in the region to
    // the RIGHT of this inset so it never slides under the bar on narrow
    // windows. Matches PlotViewer's left content edge (88 + 16 gutter).
    // See HistoryModal.qml for the original fix.
    readonly property int iconBarInset: 104

    // ── Settings values — initialised from live config on creation ──────────
    property int    defaultLeftMaskIndex:  4
    property int    defaultRightMaskIndex: 4
    property string dataOutputPath: MotionInterface.directory
    property bool   showBfiBvi:        true
    property bool   autoScale:         false
    property bool   autoScalePerPlot:  false
    // Live binding to the clinicalMode config flag. Read-only on purpose:
    // clinical selection is build-time/env-only (#233), so the modal never
    // edits or persists it.
    readonly property bool clinicalMode: MotionInterface.appConfig.clinicalMode === true
    property int    plotWindowSec:     15
    property color  bfiColor:          "#E74C3C"
    property color  bviColor:          "#3498DB"
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

    // ── App update state — driven by MotionInterface's auto-check on
    // launch (UpdateBanner.qml fires it ~3s after startup). "idle" until
    // that first check resolves; onUpdateCheckFailed is reused by both the
    // check and applyUpdate, so appUpdating disambiguates which failed.
    property string appUpdateStatus:       "idle"   // idle | uptodate | available | failed
    property string appLatestVersion:      ""
    property string appDownloadUrl:        ""
    property bool   appUpdating:           false
    property string appUpdateProgressText: "Update"

    // ── Theme tokens (aliased from AppTheme) ──────────────────────────────
    readonly property color colBgPanel:    AppTheme.sheetBg
    readonly property color colBgCard:     AppTheme.bgCard
    readonly property color colBgInput:    AppTheme.bgInput
    readonly property color colBorder:     AppTheme.borderStrong
    readonly property color colBorderSoft: AppTheme.borderSoft
    readonly property color colAccent:     AppTheme.accentInteractive
    readonly property color colTextPri:    AppTheme.textPrimary
    readonly property color colTextSec:    AppTheme.textSecondary
    readonly property color colTextMuted:  AppTheme.textTertiary

    signal settingsChanged()

    // Password gate for the Calibrate action.
    PasswordPromptModal {
        id: calibrationPasswordModal
        title: "Calibration"
        description: "Enter the password to start calibration."
        confirmLabel: "Calibrate"
        onAccepted: MotionInterface.runCalibration(
            calibrationTargetCombo.currentText.toLowerCase()
        )
    }

    // Emitted when the user enters the correct password for the audit log.
    // BloodFlow.qml opens the (ModalManager-governed) LogsModal in response.
    signal logsRequested()

    // Password gate for the audit Logs viewer.
    PasswordPromptModal {
        id: logsPasswordModal
        title: "Audit Log"
        description: "Enter the password to view the audit log."
        confirmLabel: "View Logs"
        onAccepted: root.logsRequested()
    }

    // ── Lifecycle ───────────────────────────────────────────────────────────
    function _loadFromConfig() {
        var cfg = MotionInterface.appConfig
        defaultLeftMaskIndex  = maskToIndex(cfg.leftMask  !== undefined ? cfg.leftMask  : 0x99)
        defaultRightMaskIndex = maskToIndex(cfg.rightMask !== undefined ? cfg.rightMask : 0x99)
        showBfiBvi         = clinicalMode ? true : (cfg.showBfiBvi !== undefined ? cfg.showBfiBvi : true)
        autoScale          = cfg.autoScale          !== undefined ? cfg.autoScale          : false
        autoScalePerPlot   = autoScale
        plotWindowSec      = cfg.plotWindowSec      !== undefined ? cfg.plotWindowSec      : 15
        bfiColor           = cfg.bfiColor           !== undefined ? cfg.bfiColor           : "#E74C3C"
        bviColor           = cfg.bviColor           !== undefined ? cfg.bviColor           : "#3498DB"
        // Persisted bounds are untrusted (#229) — sanitizeBoundPair
        // supplies the per-metric defaults for missing/garbage values
        // and re-clamps anything a hand-edited config smuggled in.
        var b = sanitizeBoundPair("bfi", cfg.bfiMin, cfg.bfiMax)
        bfiMin = b.min; bfiMax = b.max
        b = sanitizeBoundPair("bvi", cfg.bviMin, cfg.bviMax)
        bviMin = b.min; bviMax = b.max
        b = sanitizeBoundPair("mean", cfg.meanMin, cfg.meanMax)
        meanMin = b.min; meanMax = b.max
        b = sanitizeBoundPair("contrast", cfg.contrastMin, cfg.contrastMax)
        contrastMin = b.min; contrastMax = b.max
        writeRawCsv       = cfg.writeRawCsv       !== undefined ? cfg.writeRawCsv       : false
        rawCsvDurationSec = cfg.rawCsvDurationSec !== undefined ? cfg.rawCsvDurationSec : null
        // Theme selector (themeCombo) binds its currentIndex directly to
        // appConfig.darkMode/liquidGlass, so no manual sync is needed here.
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
        dataPathField.text = MotionInterface.directory
        root.visible = true
    }
    function close() {
        // Commit any in-progress text field edit before saving
        panel.forceActiveFocus()
        MotionInterface.directory = dataPathField.text
        MotionInterface.saveConfigs({
            "leftMask":           maskFromIndex(defaultLeftMaskIndex),
            "rightMask":          maskFromIndex(defaultRightMaskIndex),
            "showBfiBvi":         showBfiBvi,
            "autoScale":          autoScale,
            "autoScalePerPlot":   autoScalePerPlot,
            // clinicalMode is deliberately NOT saved here (#233): the
            // Clinical/Research split is build-time/env-only and the
            // config store refuses to persist it as a runtime override.
            "plotWindowSec":      plotWindowSec,
            "bfiColor":           "" + bfiColor,
            "bviColor":           "" + bviColor,
            "bfiMin":      bfiMin,
            "bfiMax":      bfiMax,
            "bviMin":      bviMin,
            "bviMax":      bviMax,
            "meanMin":     meanMin,
            "meanMax":     meanMax,
            "contrastMin": contrastMin,
            "contrastMax": contrastMax
        })
        MotionInterface.setWriteRawCsv(writeRawCsv)
        MotionInterface.setRawCsvDurationSec(rawCsvDurationSec)
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
        ListElement { name: "Far";       maskHex: "0xC3" }
        ListElement { name: "Outer";     maskHex: "0x99" }
        ListElement { name: "Left";      maskHex: "0x0F" }
        ListElement { name: "Right";     maskHex: "0xF0" }
        ListElement { name: "Third Row"; maskHex: "0x42" }
        ListElement { name: "All";       maskHex: "0xFF" }
    }

    // ── Reusable building blocks ────────────────────────────────────────────
    component SectionCard: Rectangle {
        id: sectionCard
        property string title: ""
        default property alias contentItem: cardContent.data
        // Optional item(s) right-aligned on the title row — e.g. the
        // About card's Send Debug Logs button (#227).
        property alias headerItem: headerSlot.data
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

            RowLayout {
                Layout.fillWidth: true
                spacing: 8

                Text {
                    text:           sectionCard.title
                    color:          root.colTextPri
                    font.pixelSize: 15
                    font.weight:    Font.DemiBold
                    font.letterSpacing: 0.3
                }
                Item { Layout.fillWidth: true }
                RowLayout { id: headerSlot; spacing: 8 }
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
            highlighted: styledComboCtrl.highlightedIndex === index
        }
    }

    component StyledNumberField: TextField {
        id: numFieldCtrl
        // Decimal places this field accepts and displays — typing more
        // is rejected by the validator, and commits round to match.
        property int decimals: 1
        Layout.preferredWidth: 84
        Layout.preferredHeight: 30
        font.pixelSize: 13
        color: root.colTextPri
        horizontalAlignment: Text.AlignHCenter
        inputMethodHints: Qt.ImhFormattedNumbersOnly
        validator: RegularExpressionValidator {
            regularExpression: new RegExp(
                "^-?\\d*" + (numFieldCtrl.decimals > 0
                             ? "(\\.\\d{0," + numFieldCtrl.decimals + "})?"
                             : "") + "$")
        }
        background: Rectangle {
            color: root.colBgInput
            radius: 4
            border.color: numFieldCtrl.activeFocus ? root.colAccent : root.colBorderSoft
            border.width: 1
        }
    }

    // Round to the field's decimal precision before storing, so the
    // persisted config value matches what the field displays.
    function _roundTo(v, decimals) {
        var f = Math.pow(10, decimals)
        return Math.round(v * f) / f
    }

    // ── Manual plot-bound clamping (issue #229) ─────────────────────────
    // Sane entry windows per metric. `decimals` matches the field's
    // display precision; one display unit (10^-decimals) doubles as the
    // enforced minimum min→max gap, so min < max always holds strictly
    // (PlotCell._drawTrace divides by max - min).
    //   bfi/bvi:  pipeline emits (1 - norm) * 10 — display range 0–10
    //             (matches the bfiClampLow/High display clamps).
    //   mean:     10-bit pixel data (1024 histogram bins) — 0–1024.
    //   contrast: speckle contrast — 0.00–1.00.
    readonly property var _boundPolicy: ({
        "bfi":      { lo: 0, hi: 10,   decimals: 1, defMin: 0.0, defMax: 10.0 },
        "bvi":      { lo: 0, hi: 10,   decimals: 1, defMin: 0.0, defMax: 10.0 },
        "mean":     { lo: 0, hi: 1024, decimals: 0, defMin: 0.0, defMax: 500.0 },
        "contrast": { lo: 0, hi: 1,    decimals: 2, defMin: 0.0, defMax: 1.0 }
    })

    // Coerce one edited bound: clamp into the metric's window, then keep
    // it one display-step clear of the opposing bound (`other`) so the
    // pair can never invert or collapse. Returns the value rounded to the
    // field's precision; callers re-display it so the correction is
    // visible to the user.
    function clampBound(metric, which, value, other) {
        var p = _boundPolicy[metric]
        var v = Number(value)
        if (p === undefined) return v
        if (!isFinite(v)) return which === "min" ? p.defMin : p.defMax
        var step = Math.pow(10, -p.decimals)
        if (which === "min") {
            v = Math.min(Math.max(v, p.lo), p.hi - step)
            if (isFinite(other) && v > other - step) v = other - step
        } else {
            v = Math.min(Math.max(v, p.lo + step), p.hi)
            if (isFinite(other) && v < other + step) v = other + step
        }
        return _roundTo(v, p.decimals)
    }

    // Sanitize a persisted min/max pair — config values are untrusted
    // (hand-edited or pre-#229 files can carry anything). Non-numeric or
    // missing → metric defaults; out-of-window → clamped; a pair still
    // inverted (or collapsed) after clamping → defaults.
    function sanitizeBoundPair(metric, minValue, maxValue) {
        var mn = Number(minValue)
        var mx = Number(maxValue)
        var p = _boundPolicy[metric]
        if (p === undefined) return { min: mn, max: mx }
        var step = Math.pow(10, -p.decimals)
        if (!isFinite(mn)) mn = p.defMin
        if (!isFinite(mx)) mx = p.defMax
        mn = _roundTo(Math.min(Math.max(mn, p.lo), p.hi - step), p.decimals)
        mx = _roundTo(Math.min(Math.max(mx, p.lo + step), p.hi), p.decimals)
        if (mn >= mx) { mn = p.defMin; mx = p.defMax }
        return { min: mn, max: mx }
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
        // Capture ALL pointer input so scroll/hover can't fall through to
        // the interactive plot viewer behind the modal (issue #214).
        MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            onClicked: root.close()
            onWheel: function(wheel) { wheel.accepted = true }
        }
    }

    // ── Modal panel ─────────────────────────────────────────────────────────
    Rectangle {
        id: panel
        width:  Math.min(parent.width - root.iconBarInset - 40, 680)
        height: Math.min(parent.height - 40, 800)
        radius: 14
        color:  root.colBgPanel
        border.color: root.colBorder
        border.width: 1
        // Center within [iconBarInset, parent.width] so the card clears the
        // icon bar instead of bleeding under it. horizontalCenterOffset
        // shifts the full-width center right by half the reserved inset.
        anchors.verticalCenter: parent.verticalCenter
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.horizontalCenterOffset: root.iconBarInset / 2

        // Absorb empty-space clicks inside the modal so they don't
        // propagate to the backdrop and close the modal (issue #106).
        MouseArea { anchors.fill: parent }

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
                text: root.label
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

                // ── Sensor Placement Instructions ────────────────────────────
                SectionCard {
                    title: "Sensor Placement Instructions"

                    Text {
                        text: "Place the Sensor Modules symmetrically about the midline on the patient's forehead while ensuring the sensors come into direct contact with the skin. Ensure they rest above the brow line and there are no obstructions or debris between the sensor modules and the skin."
                        color: root.colTextSec
                        font.pixelSize: 13
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                        lineHeight: 1.4
                    }
                }

                // ── Default Camera Configuration ─────────────────────────────
                SectionCard {
                    visible: !root.clinicalMode
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

                    // ── Raw histogram CSVs (research + engineering, #234) ────
                    // Research data — moved out of the Engineering card so
                    // Research (non-clinical) users get them without the
                    // engineering unlock; the engineering unlock also shows
                    // them on a clinical build. The connector re-checks the
                    // flags at scan start, so a plain Clinical build never
                    // writes raw CSVs even if a stale config left the
                    // toggle on (#43).
                    FieldRow {
                        visible: !root.clinicalMode
                                 || MotionInterface.appConfig.engineeringMode === true
                        label: "Save raw CSV"
                        PillSwitch {
                            // objectName for the unit suite; Accessible.name
                            // for the Windows a11y (UIA) tree — plain QML
                            // Text labels don't surface there, and the HIL
                            // suite (test_raw_csv_save.py) locates the
                            // toggle by this name.
                            objectName: "saveRawCsvSwitch"
                            Accessible.name: "Save raw CSV"
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
                        visible: !root.clinicalMode
                                 || MotionInterface.appConfig.engineeringMode === true
                        label: "Raw CSV duration"
                        opacity: root.writeRawCsv ? 1.0 : 0.4
                        TextField {
                            id: rawCsvDurationField
                            objectName: "rawCsvDurationField"
                            Accessible.name: "Raw CSV duration"
                            Layout.preferredWidth: 80
                            Layout.preferredHeight: 32
                            enabled: root.writeRawCsv
                            text: root.rawCsvDurationSec !== null && root.rawCsvDurationSec !== undefined
                                  ? root.rawCsvDurationSec.toString() : ""
                            placeholderText: ""
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

                // ── Realtime Plot Display ────────────────────────────────────
                SectionCard {
                    title: "Realtime Plot Display"

                    FieldRow {
                        visible: !root.clinicalMode
                        label: "Display mode"
                        Text {
                            text: "Mean / Contrast"
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
                        visible: !root.clinicalMode
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
                        visible: MotionInterface.appConfig.engineeringMode ? true : false
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
                        // Clinical mode hides the auto-scale toggle and always
                        // uses these bounds, so don't reference it there.
                        text: root.clinicalMode ? "Y-axis range for the plots."
                                               : "Used when auto-scale is off."
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

                        Text { text: "BFI"; color: AppTheme.readableInk(root.bfiColor); font.pixelSize: 13; font.weight: Font.DemiBold; Layout.preferredWidth: 80 }
                        StyledNumberField {
                            objectName: "bfiMinField"
                            Layout.preferredWidth: 90
                            decimals: 1
                            text: root.bfiMin.toFixed(1)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.bfiMin = root.clampBound("bfi", "min", v, root.bfiMax); text = root.bfiMin.toFixed(1) }
                        }
                        StyledNumberField {
                            objectName: "bfiMaxField"
                            Layout.preferredWidth: 90
                            decimals: 1
                            text: root.bfiMax.toFixed(1)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.bfiMax = root.clampBound("bfi", "max", v, root.bfiMin); text = root.bfiMax.toFixed(1) }
                        }
                        Item { Layout.fillWidth: true }

                        Text { text: "BVI"; color: AppTheme.readableInk(root.bviColor); font.pixelSize: 13; font.weight: Font.DemiBold; Layout.preferredWidth: 80 }
                        StyledNumberField {
                            objectName: "bviMinField"
                            Layout.preferredWidth: 90
                            decimals: 1
                            text: root.bviMin.toFixed(1)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.bviMin = root.clampBound("bvi", "min", v, root.bviMax); text = root.bviMin.toFixed(1) }
                        }
                        StyledNumberField {
                            objectName: "bviMaxField"
                            Layout.preferredWidth: 90
                            decimals: 1
                            text: root.bviMax.toFixed(1)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.bviMax = root.clampBound("bvi", "max", v, root.bviMin); text = root.bviMax.toFixed(1) }
                        }
                        Item { Layout.fillWidth: true }

                        // Mean / Contrast bounds only apply to the Mean/Contrast
                        // display mode, which clinical mode never shows — hide
                        // these two rows there. GridLayout skips invisible
                        // children, so the grid reflows to just BFI / BVI.
                        Text { visible: !root.clinicalMode; text: "Mean"; color: "#2ECC71"; font.pixelSize: 13; font.weight: Font.DemiBold; Layout.preferredWidth: 80 }
                        StyledNumberField {
                            objectName: "meanMinField"
                            visible: !root.clinicalMode
                            Layout.preferredWidth: 90
                            decimals: 0
                            text: root.meanMin.toFixed(0)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.meanMin = root.clampBound("mean", "min", v, root.meanMax); text = root.meanMin.toFixed(0) }
                        }
                        StyledNumberField {
                            objectName: "meanMaxField"
                            visible: !root.clinicalMode
                            Layout.preferredWidth: 90
                            decimals: 0
                            text: root.meanMax.toFixed(0)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.meanMax = root.clampBound("mean", "max", v, root.meanMin); text = root.meanMax.toFixed(0) }
                        }
                        Item { visible: !root.clinicalMode; Layout.fillWidth: true }

                        Text { visible: !root.clinicalMode; text: "Contrast"; color: "#9B59B6"; font.pixelSize: 13; font.weight: Font.DemiBold; Layout.preferredWidth: 80 }
                        StyledNumberField {
                            objectName: "contrastMinField"
                            visible: !root.clinicalMode
                            Layout.preferredWidth: 90
                            decimals: 2
                            text: root.contrastMin.toFixed(2)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.contrastMin = root.clampBound("contrast", "min", v, root.contrastMax); text = root.contrastMin.toFixed(2) }
                        }
                        StyledNumberField {
                            objectName: "contrastMaxField"
                            visible: !root.clinicalMode
                            Layout.preferredWidth: 90
                            decimals: 2
                            text: root.contrastMax.toFixed(2)
                            onEditingFinished: { var v = parseFloat(text); if (!isNaN(v)) root.contrastMax = root.clampBound("contrast", "max", v, root.contrastMin); text = root.contrastMax.toFixed(2) }
                        }
                        Item { visible: !root.clinicalMode; Layout.fillWidth: true }
                    }
                }

                // ── Appearance ───────────────────────────────────────────────
                SectionCard {
                    title: "Appearance"
                    ColumnLayout {
                        width: parent.width
                        spacing: 0
                        FieldRow {
                            label: "Theme"
                            // Single selector over the two underlying booleans
                            // (darkMode, liquidGlass). Liquid Glass is the
                            // dark-based glass — the two solid themes are Dark
                            // and Light (the warm-paper palette, #369). Written
                            // atomically via saveConfigs so it's one persist +
                            // one appConfigChanged + one audit entry.
                            StyledCombo {
                                id: themeCombo
                                Layout.preferredWidth: 150
                                model: ["Dark Mode", "Light Mode", "Liquid Glass"]
                                currentIndex: MotionInterface.appConfig.liquidGlass === true
                                              ? 2
                                              : (MotionInterface.appConfig.darkMode !== false ? 0 : 1)
                                onActivated: function(index) {
                                    if (index === 0)
                                        MotionInterface.saveConfigs({ "darkMode": true,  "liquidGlass": false })
                                    else if (index === 1)
                                        MotionInterface.saveConfigs({ "darkMode": false, "liquidGlass": false })
                                    else
                                        MotionInterface.saveConfigs({ "darkMode": true,  "liquidGlass": true })
                                }
                            }
                        }
                    }
                }

                // ── Audit Log ────────────────────────────────────────────────
                // Clinical record only — debug/diagnostic tooling lives in
                // the Support section below (#227).
                SectionCard {
                    title: "Audit Log"

                    FieldRow {
                        label: "Logs"
                        ActionButton {
                            text: "View Logs"
                            Layout.preferredWidth: 130
                            onClicked: logsPasswordModal.open()
                        }
                        Item { Layout.fillWidth: true }
                    }
                    Text {
                        text: "Password-protected, machine-readable record of system "
                              + "events for auditors. Open the viewer to browse entries "
                              + "or export them as CSV."
                        color: root.colTextMuted
                        font.pixelSize: 11
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                    }
                }

                // ── Engineering ──────────────────────────────────────────────
                SectionCard {
                    title: "Engineering"
                    visible: MotionInterface.appConfig.engineeringMode ? true : false

                    FieldRow {
                        label: "Console"
                        ActionButton {
                            text: "Soft Reset"
                            Layout.preferredWidth: 110
                            hoverColor: "#E67E22"
                            onClicked: MotionInterface.softResetSensor("console")
                        }
                        Item { Layout.fillWidth: true }
                    }

                    FieldRow {
                        label: "Console fans"
                        PillSwitch {
                            checked: MotionInterface.consoleFanOn
                            enabled: MotionInterface.consoleConnected
                            onToggled: MotionInterface.setConsoleFan(checked)
                        }
                        Text {
                            text: MotionInterface.consoleFanOn ? "On" : "Off"
                            color: MotionInterface.consoleFanOn ? root.colAccent : root.colTextMuted
                            font.pixelSize: 12
                        }
                        Item { Layout.fillWidth: true }
                    }

                    // Sensor firmware debug flags — persisted to config AND
                    // pushed live to connected sensors via setSensorDebugFlag.
                    // onToggled (not onCheckedChanged) so the appConfig rebind
                    // after appConfigChanged can't feed back into the slot.
                    FieldRow {
                        label: "Histogram compression"
                        PillSwitch {
                            checked: MotionInterface.appConfig.histoCmp === true
                            onToggled: MotionInterface.setSensorDebugFlag("histoCmp", checked)
                        }
                        Text {
                            text: MotionInterface.appConfig.histoCmp === true ? "On" : "Off"
                            color: MotionInterface.appConfig.histoCmp === true ? root.colAccent : root.colTextMuted
                            font.pixelSize: 12
                        }
                        Item { Layout.fillWidth: true }
                    }

                    FieldRow {
                        label: "Sensor debug log"
                        PillSwitch {
                            checked: MotionInterface.appConfig.sensorDebugLogging === true
                            onToggled: MotionInterface.setSensorDebugFlag("sensorDebugLogging", checked)
                        }
                        Text {
                            text: MotionInterface.appConfig.sensorDebugLogging === true ? "On" : "Off"
                            color: MotionInterface.appConfig.sensorDebugLogging === true ? root.colAccent : root.colTextMuted
                            font.pixelSize: 12
                        }
                        Item { Layout.fillWidth: true }
                    }

                    // Console USB-printf debug log — persisted to config AND
                    // pushed live to a connected console via
                    // setConsoleDebugLogging. Re-applied on connect (RAM-only
                    // firmware flag). onToggled (not onCheckedChanged) so the
                    // appConfig rebind can't feed back into the slot.
                    FieldRow {
                        label: "Console debug log"
                        PillSwitch {
                            checked: MotionInterface.appConfig.consoleDebugLogging === true
                            onToggled: MotionInterface.setConsoleDebugLogging(checked)
                        }
                        Text {
                            text: MotionInterface.appConfig.consoleDebugLogging === true ? "On" : "Off"
                            color: MotionInterface.appConfig.consoleDebugLogging === true ? root.colAccent : root.colTextMuted
                            font.pixelSize: 12
                        }
                        Item { Layout.fillWidth: true }
                    }

                    // ── Calibration / Test (moved here from the former
                    //    standalone Calibration card; now engineering-only) ──
                    Rectangle { Layout.fillWidth: true; height: 1; color: root.colBorderSoft }
                    Text {
                        text: "Calibration"
                        color: root.colTextPri
                        font.pixelSize: 13
                        font.weight: Font.DemiBold
                    }

                    // Row 1: Target | [Both ▾]
                    // Issue #117: test stations don't always have two
                    // static phantoms — let the operator calibrate one
                    // side at a time. "Both" preserves the prior default.
                    FieldRow {
                        label: "Target"
                        StyledCombo {
                            id: calibrationTargetCombo
                            Layout.preferredWidth: 130
                            model: ["Both", "Left", "Right"]
                            currentIndex: 0
                            enabled: !MotionInterface.calibrationRunning
                                  && !MotionInterface.testScanRunning
                        }
                        Item { Layout.fillWidth: true }
                    }

                    // Row 2: [Calibrate] ● status text  (aligned under Both combo)
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 12

                        // Spacer matching FieldRow label width to align with combo
                        Item {
                            Layout.preferredWidth: 140
                            Layout.minimumWidth: 140
                        }

                        ActionButton {
                            id: runCalibrationButton
                            text: "Calibrate"
                            Layout.preferredWidth: 130
                            Layout.preferredHeight: 34
                            enabled: MotionInterface.consoleConnected
                                  && !MotionInterface.calibrationRunning
                                  && !MotionInterface.testScanRunning
                            onClicked: calibrationPasswordModal.open()
                        }

                        Rectangle {
                            id: calibLight
                            width: 10
                            height: 10
                            radius: 5
                            Layout.alignment: Qt.AlignVCenter
                            border.width: 1
                            border.color: root.colBorderSoft
                            color: {
                                switch (MotionInterface.calibrationStatus) {
                                case "running": return "#2196F3"
                                case "passed":  return "#4CAF50"
                                case "failed":  return "#F44336"
                                case "aborted": return "#FF9800"
                                default:        return "#9E9E9E"
                                }
                            }
                        }

                        // Read-only TextArea (not Text/Label) so the
                        // status surfaces in the Windows UIA tree —
                        // test_calibration_ui polls for this string.
                        // TextArea over TextField so a long failure
                        // breakdown ("too much ambient light — L1:…; L2:…")
                        // wraps inside the section card instead of running
                        // off the right edge.
                        TextArea {
                            id: calibStatusLabel
                            Layout.fillWidth: true
                            Layout.preferredHeight: Math.max(34, implicitHeight)
                            readOnly: true
                            selectByMouse: false
                            activeFocusOnTab: false
                            background: null
                            padding: 0
                            wrapMode: TextEdit.Wrap
                            verticalAlignment: TextEdit.AlignVCenter
                            color: root.colTextPri
                            font.pixelSize: 12
                            text: {
                                switch (MotionInterface.calibrationStatus) {
                                case "running":
                                    return "Calibrating... (" + calibTimer.elapsedSec
                                           + "s / " + MotionInterface.maxCalibrationTimeSec + "s)"
                                case "passed":  return "Calibration Passed"
                                case "failed":
                                    var reason = MotionInterface.calibrationFailureReason
                                    return reason
                                        ? "Calibration Failed — " + reason
                                        : "Calibration Failed"
                                case "aborted": return "Calibration Aborted"
                                default:        return ""
                                }
                            }
                        }
                    }

                    // Row 3: [Test] aligned under Calibrate
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 12

                        Item {
                            Layout.preferredWidth: 140
                            Layout.minimumWidth: 140
                        }

                        ActionButton {
                            id: runTestButton
                            text: "Test"
                            Layout.preferredWidth: 130
                            Layout.preferredHeight: 34
                            enabled: MotionInterface.consoleConnected
                                  && !MotionInterface.calibrationRunning
                                  && !MotionInterface.testScanRunning
                            onClicked: MotionInterface.runTestScan(
                                calibrationTargetCombo.currentText.toLowerCase()
                            )
                        }

                        Item { Layout.fillWidth: true }
                    }

                    // 1 Hz tick driving the elapsed counter while running.
                    Timer {
                        id: calibTimer
                        property int elapsedSec: 0
                        interval: 1000
                        repeat: true
                        running: MotionInterface.calibrationRunning
                        onTriggered: elapsedSec += 1
                    }

                    Connections {
                        target: MotionInterface
                        function onCalibrationStateChanged() {
                            if (MotionInterface.calibrationStatus === "running") {
                                calibTimer.elapsedSec = 0
                            }
                        }
                    }

                    Connections {
                        target: MotionInterface
                        function onTestScanStateChanged() {
                            var s = MotionInterface.testScanStatus
                            if (s === "running" || s === "done"
                                || s === "failed" || s === "aborted") {
                                testResultsWindow.show()
                                testResultsWindow.raise()
                                testResultsWindow.requestActivate()
                            }
                        }
                    }

                    Rectangle { Layout.fillWidth: true; height: 1; color: root.colBorderSoft }
                    FieldRow {
                        label: "Engineering mode"
                        ActionButton {
                            text: "Disable engineering mode"
                            Layout.preferredWidth: 200
                            hoverColor: "#C0392B"
                            onClicked: {
                                MotionInterface.setConfig("engineeringMode", false)
                                MotionInterface.notify("Engineering mode disabled.", "info", 3000, false, "engineering-mode")
                            }
                        }
                        Item { Layout.fillWidth: true }
                    }
                }

                // ── About ─────────────────────────────────────────────────────
                SectionCard {
                    title: "About"

                    // Debug-log bundle for support, top-right of the About
                    // (firmware info) card per #227 — deliberately away from
                    // the Audit Log section: the audit log is the clinical
                    // record, the debug bundle is engineering diagnostics.
                    // Visible in ALL modes — it is the designated support
                    // path for clinical sites and the bundle contains no
                    // scan or patient data.
                    headerItem: ActionButton {
                        text: "Send Debug Logs"
                        Layout.preferredWidth: 150
                        // Direct action — zips the last 48h of app logs,
                        // reveals the file, and toasts the support address.
                        onClicked: MotionInterface.prepareDebugLogBundle()
                    }

                    // Small pill button reused for the Application row and
                    // each device firmware row.
                    component UpdateChip: Rectangle {
                        id: chip
                        signal chipClicked()
                        property string label: "Update"
                        property bool chipEnabled: true
                        width: chipText.implicitWidth + 18; height: 24; radius: 4
                        color: chipArea.containsMouse ? Qt.lighter(AppTheme.accentInteractive, 1.1) : AppTheme.accentInteractive
                        opacity: chipEnabled ? 1.0 : 0.6
                        Text {
                            id: chipText
                            anchors.centerIn: parent
                            text: chip.label
                            color: "#FFFFFF"
                            font.pixelSize: 12
                            font.weight: Font.DemiBold
                        }
                        MouseArea {
                            id: chipArea
                            anchors.fill: parent
                            enabled: chip.chipEnabled
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: chip.chipClicked()
                        }
                    }

                    // Auto-checked ~3s after launch by UpdateBanner.qml
                    // (Issue #96); this row just reflects that same
                    // check's result. Hidden entirely in clinical mode —
                    // clinical users shouldn't see update prompts.
                    FieldRow {
                        label: "Application"
                        Text { text: appVersion; color: root.colTextPri; font.pixelSize: 13; font.family: "Consolas" }
                        Item { Layout.fillWidth: true }
                        Text {
                            visible: !root.clinicalMode && root.appUpdateStatus === "uptodate"
                            text: "Up to date"
                            color: AppTheme.statusGreen
                            font.pixelSize: 12
                        }
                        Text {
                            visible: !root.clinicalMode && root.appUpdateStatus === "failed"
                            text: "Check failed"
                            color: AppTheme.accentRed
                            font.pixelSize: 12
                        }
                        UpdateChip {
                            visible: !root.clinicalMode && root.appUpdateStatus === "available"
                            label: root.appUpdating ? root.appUpdateProgressText : "Update"
                            chipEnabled: !root.appUpdating
                            onChipClicked: {
                                root.appUpdating = true
                                root.appUpdateProgressText = "Starting…"
                                MotionInterface.applyUpdate(root.appDownloadUrl)
                            }
                        }
                    }

                    Connections {
                        target: MotionInterface
                        enabled: !root.clinicalMode
                        function onUpdateAvailable(version, url) {
                            root.appUpdateStatus = "available"
                            root.appLatestVersion = version
                            root.appDownloadUrl = url
                        }
                        function onUpdateNotAvailable() {
                            root.appUpdateStatus = "uptodate"
                        }
                        function onUpdateProgress(message) {
                            root.appUpdateProgressText = message
                        }
                        // Shared by both the background check and applyUpdate —
                        // an in-progress update failing should fall back to a
                        // retryable "Update" chip, not a dead-end "Check failed".
                        function onUpdateCheckFailed(msg) {
                            if (root.appUpdating) {
                                root.appUpdating = false
                                root.appUpdateProgressText = "Update"
                            } else {
                                root.appUpdateStatus = "failed"
                            }
                        }
                    }

                    FieldRow {
                        label: "SDK"
                        Text { text: MotionInterface.get_sdk_version(); color: root.colTextPri; font.pixelSize: 13; font.family: "Consolas" }
                        Item { Layout.fillWidth: true }
                    }

                    // Firmware versions per device, cached on connect by the
                    // connector (_log_device_stats). Each row shows the live
                    // version when connected, or a muted "Not connected", plus
                    // an "Up to date" / "Update" indicator once the connector's
                    // connect-time availability check resolves.
                    Rectangle { Layout.fillWidth: true; height: 1; color: root.colBorderSoft }

                    component DeviceRow: FieldRow {
                        property string dev: ""
                        property bool connected: false
                        property string current: ""
                        property bool updateAvailable: false
                        Text {
                            text: connected ? (current || "—") : "Not connected"
                            color: connected ? root.colTextPri : root.colTextMuted
                            font.pixelSize: 13
                            font.family: "Consolas"
                        }
                        Item { Layout.fillWidth: true }
                        Text {
                            visible: connected && !updateAvailable
                            text: "Up to date"
                            color: AppTheme.statusGreen
                            font.pixelSize: 12
                        }
                        UpdateChip {
                            visible: connected && updateAvailable
                            onChipClicked: fwConfirm.openFor(label, dev)
                        }
                    }

                    DeviceRow {
                        label: "Console FW"; dev: "console"
                        connected: MotionInterface.consoleConnected
                        current: MotionInterface.consoleFirmwareVersion
                        updateAvailable: MotionInterface.consoleFirmwareUpdateAvailable
                    }
                    DeviceRow {
                        label: "Left Sensor FW"; dev: "left"
                        connected: MotionInterface.leftSensorConnected
                        current: MotionInterface.leftSensorFirmwareVersion
                        updateAvailable: MotionInterface.leftSensorFirmwareUpdateAvailable
                    }
                    DeviceRow {
                        label: "Right Sensor FW"; dev: "right"
                        connected: MotionInterface.rightSensorConnected
                        current: MotionInterface.rightSensorFirmwareVersion
                        updateAvailable: MotionInterface.rightSensorFirmwareUpdateAvailable
                    }

                    // Live flashing progress, driven by the connector. Shows a
                    // clean status line plus a real ProgressBar (determinate
                    // for the dfu erase/write phases, indeterminate for the
                    // check/download/DFU-entry phases that report no percent).
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 4
                        visible: fwStatus.text.length > 0
                        Text {
                            id: fwStatus
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            font.pixelSize: 12
                            color: root.colTextMuted
                        }
                        ProgressBar {
                            id: fwBar
                            Layout.fillWidth: true
                            from: 0; to: 100
                            value: 0
                            visible: false
                        }
                    }

                    // Beta firmware: when on, the autoupdater targets the
                    // most-recently-published release (incl. dev/rc), not just
                    // full releases. Plain config flag — the connector re-runs
                    // the firmware check when it toggles (setConfig hook).
                    // Engineering (engineering mode) only.
                    Rectangle {
                        Layout.fillWidth: true; height: 1; color: root.colBorderSoft
                        visible: MotionInterface.appConfig.engineeringMode === true
                    }
                    FieldRow {
                        label: "Beta Updates"
                        visible: MotionInterface.appConfig.engineeringMode === true
                        PillSwitch {
                            checked: MotionInterface.appConfig.downloadBetaFirmware === true
                            onToggled: MotionInterface.setConfig("downloadBetaFirmware", checked)
                        }
                        Text {
                            text: MotionInterface.appConfig.downloadBetaFirmware === true ? "On" : "Off"
                            color: MotionInterface.appConfig.downloadBetaFirmware === true ? root.colAccent : root.colTextMuted
                            font.pixelSize: 12
                        }
                        Item { Layout.fillWidth: true }
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

    // Confirm before flashing (device reboots into DFU).
    Dialog {
        id: fwConfirm
        modal: true
        anchors.centerIn: Overlay.overlay
        title: "Confirm Firmware Update"
        property string _dev: ""
        function openFor(lbl, dev) {
            _dev = dev
            fwConfirmText.text = lbl + " will reboot into DFU mode and be "
                + "re-flashed. Do not unplug it until this completes."
            open()
        }
        contentItem: Text {
            id: fwConfirmText
            wrapMode: Text.WordWrap
            color: root.colTextPri
            font.pixelSize: 13
            padding: 16
        }
        footer: DialogButtonBox {
            Button { text: "Cancel"; onClicked: fwConfirm.close() }
            Button {
                text: "Update"
                onClicked: {
                    fwConfirm.close()
                    MotionInterface.startFirmwareUpdate(fwConfirm._dev)
                }
            }
        }
    }

    Connections {
        target: MotionInterface
        function onFirmwareUpdateProgress(deviceKey, stage, percent, msg) {
            fwStatus.text = deviceKey + " — " + msg
                + (percent >= 0 ? "  " + percent + "%" : "")
            fwStatus.color = root.colTextMuted
            fwBar.visible = true
            fwBar.indeterminate = (percent < 0)
            fwBar.value = (percent < 0 ? 0 : percent)
        }
        function onFirmwareUpdateFinished(deviceKey, ok, msg) {
            fwStatus.text = deviceKey + " — " + msg
            fwStatus.color = ok ? AppTheme.accentGreen : AppTheme.accentRed
            fwBar.visible = false
        }
    }
}
