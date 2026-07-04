import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 9998


    // Modal interface — see HistoryModal.qml for rationale.
    readonly property string label: "Scan Settings"

    // Left-edge space to keep clear of the icon bar (BloodFlow's
    // ButtonPanel — 80px wide + 8px margin, pinned left at z:10000, which
    // is ABOVE this modal's z:9998). The card is centered in the region to
    // the RIGHT of this inset so it never slides under the bar on narrow
    // windows. Matches PlotViewer's left content edge (88 + 16 gutter).
    // See HistoryModal.qml for the original fix.
    readonly property int iconBarInset: 104

    // Camera selection
    signal selectionChanged(int leftMask, int rightMask)

    property var leftSensorActive: [false, false, false, false, false, false, false, false]
    property var rightSensorActive: [false, false, false, false, false, false, false, false]

    // Clinical (FDA) mode hides the research-only Viewer Mode picker. Falls
    // back to appConfig so the gate holds even if the caller doesn't bind it.
    property bool clinicalMode: MotionInterface.appConfig.clinicalMode === true

    // Scan duration
    property bool freeRun: false
    property int hours: 1
    property int minutes: 0
    property int seconds: 0
    property int durationSec: freeRun ? 0 : (hours * 3600 + minutes * 60 + seconds)

    ListModel {
        id: sensorPatterns
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

    function maskFromArray(arr) {
        if (!arr || arr.length !== 8) return 0
        const bitMap = [7, 6, 5, 4, 3, 2, 1, 0]
        var m = 0
        for (var i = 0; i < 8; i++) {
            if (arr[i]) m |= (1 << bitMap[i])
        }
        return m
    }

    function maskToPatternIndex(mask) {
        for (var i = 0; i < sensorPatterns.count; i++) {
            if (parseInt(sensorPatterns.get(i).maskHex, 16) === mask) return i
        }
        return -1
    }

    function applyPatternToSensor(index, side) {
        var pattern
        switch (index) {
            case 0: pattern = [false,false,false,false,false,false,false,false]; break
            case 1: pattern = [false,true,false,true,true,false,true,false]; break
            case 2: pattern = [false,true,true,false,false,true,true,false]; break
            case 3: pattern = [true,true,false,false,false,false,true,true]; break
            case 4: pattern = [true,false,false,true,true,false,false,true]; break
            case 5: pattern = [false,false,false,false,true,true,true,true]; break
            case 6: pattern = [true,true,true,true,false,false,false,false]; break
            case 7: pattern = [false,true,false,false,false,false,true,false]; break
            case 8: pattern = [true,true,true,true,true,true,true,true]; break
            default: return
        }
        if (side === "left") {
            leftSensorActive = pattern
            leftSensorView.sensorActive = pattern
        } else {
            rightSensorActive = pattern
            rightSensorView.sensorActive = pattern
        }
    }

    function open() {
        userLabelField.text = MotionInterface.userLabel
        root.visible = true
    }
    function close() {
        commitDurationFields()
        selectionChanged(maskFromArray(leftSensorActive), maskFromArray(rightSensorActive))
        root.visible = false
    }

    function commitDurationFields() {
        var h = parseInt(hoursField.text);   if (isNaN(h)) h = 0
        var m = parseInt(minutesField.text); if (isNaN(m)) m = 0
        var s = parseInt(secondsField.text); if (isNaN(s)) s = 0
        h = Math.max(0, Math.min(99, h))
        m = Math.max(0, Math.min(59, m))
        s = Math.max(0, Math.min(59, s))

        // Reject 0:00:00 — a zero-second scan can't acquire any data.
        // Reset to 1 minute and fire a warning toast so the user sees
        // why their input was overridden. (Issue #82.) The 'tag'
        // dedupes repeated rejections into a single visible toast.
        if (!root.freeRun && h === 0 && m === 0 && s === 0) {
            h = 0; m = 1; s = 0
            MotionInterface.notify(
                "Scan duration cannot be 0 seconds — reset to 1 minute.",
                "warning", 5000, true, "scan-duration-zero"
            )
        }

        root.hours   = h
        root.minutes = m
        root.seconds = s
        // Sync field text back so the modal reflects the saved value
        // (otherwise the rejected 00:00:00 would still show on next open).
        hoursField.text   = String(h)
        minutesField.text = String(m).padStart(2, '0')
        secondsField.text = String(s).padStart(2, '0')
    }

    function setInitialSelection(leftArr, rightArr) {
        leftSensorActive = leftArr
        rightSensorActive = rightArr
        leftSensorView.sensorActive = leftArr
        rightSensorView.sensorActive = rightArr
        var li = maskToPatternIndex(maskFromArray(leftArr))
        var ri = maskToPatternIndex(maskFromArray(rightArr))
        if (li >= 0) leftSelector.currentIndex = li
        if (ri >= 0) rightSelector.currentIndex = ri
    }

    // Dimmed backdrop
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
        id: card
        width: Math.min(parent.width - root.iconBarInset - 40, 520)
        // Grow to fit the content (Viewer Mode + Camera + Duration), capped to
        // the window so it never overflows; the body scrolls past the cap.
        height: Math.min(parent.height - 60, contentCol.implicitHeight + 40)
        radius: 14
        color: AppTheme.bgContainer
        border.color: AppTheme.borderSubtle
        border.width: 2
        // Center within [iconBarInset, parent.width] so the card clears the
        // icon bar instead of bleeding under it. horizontalCenterOffset
        // shifts the full-width center right by half the reserved inset.
        anchors.verticalCenter: parent.verticalCenter
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.horizontalCenterOffset: root.iconBarInset / 2

        // Absorb empty-space clicks inside the modal so they don't
        // propagate to the backdrop and close the modal (issue #106).
        // Declared first → lowest in declaration z-order, so the X
        // close button and every other interactive child still gets
        // its events first.
        MouseArea { anchors.fill: parent }

        // X close button
        Rectangle {
            width: 28; height: 28; radius: 14
            color: xArea.containsMouse ? "#C0392B" : AppTheme.borderStrong
            border.color: AppTheme.borderHover; border.width: 1
            anchors.top: parent.top; anchors.right: parent.right
            anchors.topMargin: 10; anchors.rightMargin: 10
            z: 10
            Behavior on color { ColorAnimation { duration: 120 } }
            Text { anchors.centerIn: parent; text: "✕"; color: AppTheme.textPrimary; font.pixelSize: 13 }
            MouseArea {
                id: xArea; anchors.fill: parent; hoverEnabled: true
                cursorShape: Qt.PointingHandCursor; onClicked: root.close()
            }
        }

        // Scrollable body: the card auto-sizes to fit this content, but on a
        // short window the content exceeds the cap and this Flickable scrolls
        // instead of clipping the bottom (Scan Duration) off the card.
        Flickable {
            id: contentFlick
            anchors.fill: parent
            anchors.margins: 20
            contentWidth: width
            contentHeight: contentCol.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

            ColumnLayout {
            id: contentCol
            width: contentFlick.width
            spacing: 8

            // Title
            Text {
                text: root.label
                color: AppTheme.textPrimary
                font.pixelSize: 20
                font.weight: Font.Bold
                Layout.alignment: Qt.AlignHCenter
            }

            // ── Session ──────────────────────────────────────────────────
            Rectangle { Layout.fillWidth: true; height: 1; color: AppTheme.borderSubtle }

            Text {
                text: "Session"
                color: AppTheme.textSecondary
                font.pixelSize: 15
                font.weight: Font.DemiBold
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 10

                // Read-only TextField (not Text) so the label surfaces in
                // the Windows UIA tree — test_scan_settings.test_03 polls
                // for this string.
                TextField {
                    text: "User Label:"
                    readOnly: true
                    selectByMouse: false
                    activeFocusOnTab: false
                    background: null
                    padding: 0
                    color: AppTheme.textSecondary
                    font.pixelSize: 14
                    Layout.alignment: Qt.AlignVCenter
                }

                TextField {
                    id: userLabelField
                    Layout.fillWidth: true
                    Layout.preferredHeight: 30
                    font.pixelSize: 14
                    color: AppTheme.textPrimary
                    background: Rectangle {
                        color: AppTheme.bgInput; radius: 4
                        border.color: userLabelField.activeFocus ? AppTheme.accentBlue : AppTheme.borderSubtle
                        border.width: 1
                    }
                    onEditingFinished: {
                        if (text !== MotionInterface.userLabel) {
                            MotionInterface.userLabel = text
                            text = MotionInterface.userLabel  // reflect normalization
                        }
                    }
                }
            }

            // ── Viewer Mode (Research only — hidden in clinical/FDA mode) ─
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 10
                visible: !root.clinicalMode

                Rectangle { Layout.fillWidth: true; height: 1; color: AppTheme.borderSubtle }

                Text {
                    text: "Viewer Mode"
                    color: AppTheme.textSecondary
                    font.pixelSize: 15
                    font.weight: Font.DemiBold
                }

                RowLayout {
                    Layout.alignment: Qt.AlignHCenter
                    spacing: 10
                    Repeater {
                        model: [["Default", "default"], ["Pulse", "pulse"]]
                        delegate: Button {
                            required property var modelData
                            Layout.preferredWidth: 130
                            Layout.preferredHeight: 34
                            onClicked: MotionInterface.setConfig("viewerMode", modelData[1])
                            background: Rectangle {
                                radius: 6
                                color: (MotionInterface.appConfig.viewerMode || "default") === modelData[1]
                                       ? AppTheme.accentBlue : AppTheme.bgInput
                                border.color: AppTheme.borderSubtle; border.width: 1
                            }
                            contentItem: Text {
                                text: modelData[0]
                                color: (MotionInterface.appConfig.viewerMode || "default") === modelData[1]
                                       ? "#FFFFFF" : AppTheme.textPrimary
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                                font.pixelSize: 14
                            }
                        }
                    }
                }
                Text {
                    text: (MotionInterface.appConfig.viewerMode || "default") === "pulse"
                          ? "Main view shows cardiac pulse waveforms."
                          : "Main view shows the default BFI/BVI plots."
                    color: AppTheme.textTertiary
                    font.pixelSize: 12
                    Layout.alignment: Qt.AlignHCenter
                }
            }

            // ── Camera Configuration ──────────────────────────────────────
            Rectangle { Layout.fillWidth: true; height: 1; color: AppTheme.borderSubtle }

            Text {
                text: "Camera Configuration"
                color: AppTheme.textSecondary
                font.pixelSize: 15
                font.weight: Font.DemiBold
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 20
                Layout.alignment: Qt.AlignHCenter

                // Left Sensor
                ColumnLayout {
                    spacing: 6
                    Layout.alignment: Qt.AlignHCenter

                    SensorView {
                        id: leftSensorView
                        title: "Left Sensor"
                        sensorSide: "left"
                        connector: MotionInterface
                        showFanControl: MotionInterface.appConfig.engineeringMode ? true : false
                    }

                    ComboBox {
                        id: leftSelector
                        Layout.preferredWidth: 150
                        Layout.preferredHeight: 32
                        Layout.alignment: Qt.AlignHCenter
                        model: sensorPatterns
                        textRole: "name"
                        font.pixelSize: 13
                        enabled: MotionInterface.leftSensorConnected
                        opacity: enabled ? 1.0 : 0.4
                        onCurrentIndexChanged: applyPatternToSensor(currentIndex, "left")
                        contentItem: Text {
                            leftPadding: 10; text: leftSelector.displayText; font: leftSelector.font
                            color: AppTheme.textPrimary; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight
                        }
                        background: Rectangle { color: AppTheme.bgInput; radius: 4; border.color: AppTheme.borderSubtle; border.width: 1 }
                        indicator: Text { x: leftSelector.width - width - 10; y: (leftSelector.height - height) / 2; text: "\u25BE"; font.pixelSize: 14; color: AppTheme.textSecondary }
                        delegate: ItemDelegate {
                            width: leftSelector.width; height: 32
                            contentItem: Text { text: model.name; font.pixelSize: 13; color: AppTheme.textPrimary; verticalAlignment: Text.AlignVCenter; leftPadding: 8 }
                            background: Rectangle { color: highlighted ? AppTheme.accentBlue : "transparent" }
                            highlighted: leftSelector.highlightedIndex === index
                        }
                        popup: Popup {
                            y: leftSelector.height; width: leftSelector.width; implicitHeight: contentItem.implicitHeight + 2; padding: 1
                            contentItem: ListView { clip: true; implicitHeight: contentHeight; model: leftSelector.delegateModel; ScrollIndicator.vertical: ScrollIndicator {} }
                            background: Rectangle { color: AppTheme.bgCard; radius: 4; border.color: AppTheme.borderSubtle; border.width: 1 }
                        }
                        Component.onCompleted: {
                            var defMask = MotionInterface.appConfig.leftMask !== undefined
                                          ? MotionInterface.appConfig.leftMask : 0x99
                            var idx = maskToPatternIndex(defMask)
                            currentIndex = (idx >= 0) ? idx : 4
                        }
                    }
                }

                // Right Sensor
                ColumnLayout {
                    spacing: 6
                    Layout.alignment: Qt.AlignHCenter

                    SensorView {
                        id: rightSensorView
                        title: "Right Sensor"
                        sensorSide: "right"
                        connector: MotionInterface
                        showFanControl: MotionInterface.appConfig.engineeringMode ? true : false
                    }

                    ComboBox {
                        id: rightSelector
                        Layout.preferredWidth: 150
                        Layout.preferredHeight: 32
                        Layout.alignment: Qt.AlignHCenter
                        model: sensorPatterns
                        textRole: "name"
                        font.pixelSize: 13
                        enabled: MotionInterface.rightSensorConnected
                        opacity: enabled ? 1.0 : 0.4
                        onCurrentIndexChanged: applyPatternToSensor(currentIndex, "right")
                        contentItem: Text {
                            leftPadding: 10; text: rightSelector.displayText; font: rightSelector.font
                            color: AppTheme.textPrimary; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight
                        }
                        background: Rectangle { color: AppTheme.bgInput; radius: 4; border.color: AppTheme.borderSubtle; border.width: 1 }
                        indicator: Text { x: rightSelector.width - width - 10; y: (rightSelector.height - height) / 2; text: "\u25BE"; font.pixelSize: 14; color: AppTheme.textSecondary }
                        delegate: ItemDelegate {
                            width: rightSelector.width; height: 32
                            contentItem: Text { text: model.name; font.pixelSize: 13; color: AppTheme.textPrimary; verticalAlignment: Text.AlignVCenter; leftPadding: 8 }
                            background: Rectangle { color: highlighted ? AppTheme.accentBlue : "transparent" }
                            highlighted: rightSelector.highlightedIndex === index
                        }
                        popup: Popup {
                            y: rightSelector.height; width: rightSelector.width; implicitHeight: contentItem.implicitHeight + 2; padding: 1
                            contentItem: ListView { clip: true; implicitHeight: contentHeight; model: rightSelector.delegateModel; ScrollIndicator.vertical: ScrollIndicator {} }
                            background: Rectangle { color: AppTheme.bgCard; radius: 4; border.color: AppTheme.borderSubtle; border.width: 1 }
                        }
                        Component.onCompleted: {
                            var defMask = MotionInterface.appConfig.rightMask !== undefined
                                          ? MotionInterface.appConfig.rightMask : 0x99
                            var idx = maskToPatternIndex(defMask)
                            currentIndex = (idx >= 0) ? idx : 0
                        }
                    }
                }
            }

            // ── Scan Duration ─────────────────────────────────────────────
            Rectangle { Layout.fillWidth: true; height: 1; color: AppTheme.borderSubtle }

            Text {
                text: "Scan Duration"
                color: AppTheme.textSecondary
                font.pixelSize: 15
                font.weight: Font.DemiBold
            }

            // Timed / Free Run toggle
            RowLayout {
                Layout.alignment: Qt.AlignHCenter
                spacing: 16

                Text {
                    text: "Timed"
                    color: !root.freeRun ? AppTheme.accentBlue : AppTheme.textSecondary
                    font.pixelSize: 14
                    font.weight: !root.freeRun ? Font.Bold : Font.Normal
                }

                Switch {
                    id: modeSwitch
                    checked: root.freeRun
                    onCheckedChanged: root.freeRun = checked
                    indicator: Rectangle {
                        x: modeSwitch.leftPadding; y: (modeSwitch.height - height) / 2
                        width: 44; height: 24; radius: 12
                        color: modeSwitch.checked ? AppTheme.accentBlue : AppTheme.bgInput
                        border.color: modeSwitch.checked ? AppTheme.accentBlue : AppTheme.borderSubtle; border.width: 1
                        Behavior on color { ColorAnimation { duration: 120 } }
                        Rectangle {
                            x: modeSwitch.checked ? parent.width - width - 3 : 3
                            y: 3; width: 18; height: 18; radius: 9; color: "#FFFFFF"
                            Behavior on x { NumberAnimation { duration: 120 } }
                        }
                    }
                }

                Text {
                    text: "Continuous"
                    color: root.freeRun ? AppTheme.accentBlue : AppTheme.textSecondary
                    font.pixelSize: 14
                    font.weight: root.freeRun ? Font.Bold : Font.Normal
                }
            }

            // H : M : S fields (timed mode)
            RowLayout {
                Layout.alignment: Qt.AlignHCenter
                spacing: 8
                visible: !root.freeRun

                TextField {
                    id: hoursField
                    text: String(root.hours)
                    inputMethodHints: Qt.ImhDigitsOnly
                    validator: IntValidator { bottom: 0; top: 99 }
                    font.pixelSize: 20; color: AppTheme.textPrimary
                    horizontalAlignment: Text.AlignHCenter
                    Layout.preferredWidth: 54; Layout.preferredHeight: 40
                    background: Rectangle { color: AppTheme.bgInput; radius: 6; border.color: AppTheme.borderSubtle; border.width: 1 }
                    onEditingFinished: {
                        var v = parseInt(text); if (isNaN(v)) v = 0
                        root.hours = Math.max(0, Math.min(99, v)); text = String(root.hours)
                    }
                }
                Text { text: ":"; color: AppTheme.textSecondary; font.pixelSize: 22 }
                TextField {
                    id: minutesField
                    text: String(root.minutes).padStart(2, '0')
                    inputMethodHints: Qt.ImhDigitsOnly
                    validator: IntValidator { bottom: 0; top: 59 }
                    font.pixelSize: 20; color: AppTheme.textPrimary
                    horizontalAlignment: Text.AlignHCenter
                    Layout.preferredWidth: 54; Layout.preferredHeight: 40
                    background: Rectangle { color: AppTheme.bgInput; radius: 6; border.color: AppTheme.borderSubtle; border.width: 1 }
                    onEditingFinished: {
                        var v = parseInt(text); if (isNaN(v)) v = 0
                        root.minutes = Math.max(0, Math.min(59, v)); text = String(root.minutes).padStart(2, '0')
                    }
                }
                Text { text: ":"; color: AppTheme.textSecondary; font.pixelSize: 22 }
                TextField {
                    id: secondsField
                    text: String(root.seconds).padStart(2, '0')
                    inputMethodHints: Qt.ImhDigitsOnly
                    validator: IntValidator { bottom: 0; top: 59 }
                    font.pixelSize: 20; color: AppTheme.textPrimary
                    horizontalAlignment: Text.AlignHCenter
                    Layout.preferredWidth: 54; Layout.preferredHeight: 40
                    background: Rectangle { color: AppTheme.bgInput; radius: 6; border.color: AppTheme.borderSubtle; border.width: 1 }
                    onEditingFinished: {
                        var v = parseInt(text); if (isNaN(v)) v = 0
                        root.seconds = Math.max(0, Math.min(59, v)); text = String(root.seconds).padStart(2, '0')
                    }
                }
                Text { text: "H : M : S"; color: AppTheme.textTertiary; font.pixelSize: 11; Layout.alignment: Qt.AlignBottom }
            }

            // Free run hint
            Text {
                visible: root.freeRun
                text: "Scan will run indefinitely until stopped."
                color: AppTheme.textTertiary
                font.pixelSize: 13
                Layout.alignment: Qt.AlignHCenter
            }
            }
        }

        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape) { root.close(); event.accepted = true }
        }
    }

    // Connection status is reflected purely by the selectors' ``enabled:``
    // bindings (greyed out at 0.4 opacity when the side is disconnected).
    // The modal does NOT reset its internal selection on disconnect:
    // issue #40 — a transient USB drop while the modal is open used to
    // zero out leftSelector/rightSelector here, which then propagated to
    // bloodFlow.leftMask/rightMask via close() → selectionChanged → the
    // plot ended up with an empty series order and rendered the
    // "Press Start to begin scanning" placeholder instead of the live
    // plot. Preserving the selection lets it ride through the reconnect.
}
