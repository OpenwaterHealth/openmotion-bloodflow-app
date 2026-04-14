import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 9998

    AppTheme { id: theme }

    // Camera selection
    signal selectionChanged(int leftMask, int rightMask)

    property var leftSensorActive: [false, false, false, false, false, false, false, false]
    property var rightSensorActive: [false, false, false, false, false, false, false, false]

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
        ListElement { name: "Far";       maskHex: "0x55" }
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
            case 3: pattern = [true,false,true,false,false,true,false,true]; break
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
        userLabelField.text = MOTIONInterface.userLabel
        root.visible = true
    }
    function close() {
        selectionChanged(maskFromArray(leftSensorActive), maskFromArray(rightSensorActive))
        root.visible = false
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
        MouseArea { anchors.fill: parent; onClicked: {} }
    }

    Rectangle {
        width: Math.min(parent.width - 80, 560)
        height: Math.min(parent.height - 60, 680)
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
            spacing: 14

            // Title
            Text {
                text: "Scan Settings"
                color: theme.textPrimary
                font.pixelSize: 20
                font.weight: Font.Bold
                Layout.alignment: Qt.AlignHCenter
            }

            // ── Session ──────────────────────────────────────────────────
            Rectangle { Layout.fillWidth: true; height: 1; color: theme.borderSubtle }

            Text {
                text: "Session"
                color: theme.textSecondary
                font.pixelSize: 15
                font.weight: Font.DemiBold
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 10

                Text {
                    text: "User Label:"
                    color: theme.textSecondary
                    font.pixelSize: 14
                    Layout.alignment: Qt.AlignVCenter
                }

                TextField {
                    id: userLabelField
                    Layout.fillWidth: true
                    Layout.preferredHeight: 30
                    font.pixelSize: 14
                    color: theme.textPrimary
                    background: Rectangle {
                        color: theme.bgInput; radius: 4
                        border.color: userLabelField.activeFocus ? theme.accentBlue : theme.borderSubtle
                        border.width: 1
                    }
                    onEditingFinished: {
                        if (text !== MOTIONInterface.userLabel) {
                            MOTIONInterface.userLabel = text
                            text = MOTIONInterface.userLabel  // reflect normalization
                        }
                    }
                }
            }

            // ── Camera Configuration ──────────────────────────────────────
            Rectangle { Layout.fillWidth: true; height: 1; color: theme.borderSubtle }

            Text {
                text: "Camera Configuration"
                color: theme.textSecondary
                font.pixelSize: 15
                font.weight: Font.DemiBold
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 24
                Layout.alignment: Qt.AlignHCenter

                // Left Sensor
                ColumnLayout {
                    spacing: 8
                    Layout.alignment: Qt.AlignHCenter

                    SensorView {
                        id: leftSensorView
                        title: "Left Sensor"
                        sensorSide: "left"
                        connector: MOTIONInterface
                        showFanControl: MOTIONInterface.appConfig.developerMode ? true : false
                    }

                    ComboBox {
                        id: leftSelector
                        Layout.preferredWidth: 190
                        Layout.preferredHeight: 34
                        model: sensorPatterns
                        textRole: "name"
                        font.pixelSize: 13
                        enabled: MOTIONInterface.leftSensorConnected
                        opacity: enabled ? 1.0 : 0.4
                        onCurrentIndexChanged: applyPatternToSensor(currentIndex, "left")
                        contentItem: Text {
                            leftPadding: 10; text: leftSelector.displayText; font: leftSelector.font
                            color: theme.textPrimary; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight
                        }
                        background: Rectangle { color: theme.bgInput; radius: 4; border.color: theme.borderSubtle; border.width: 1 }
                        indicator: Text { x: leftSelector.width - width - 10; y: (leftSelector.height - height) / 2; text: "\u25BE"; font.pixelSize: 14; color: theme.textSecondary }
                        delegate: ItemDelegate {
                            width: leftSelector.width; height: 32
                            contentItem: Text { text: model.name; font.pixelSize: 13; color: theme.textPrimary; verticalAlignment: Text.AlignVCenter; leftPadding: 8 }
                            background: Rectangle { color: highlighted ? theme.accentBlue : "transparent" }
                            highlighted: leftSelector.highlightedIndex === index
                        }
                        popup: Popup {
                            y: leftSelector.height; width: leftSelector.width; implicitHeight: contentItem.implicitHeight + 2; padding: 1
                            contentItem: ListView { clip: true; implicitHeight: contentHeight; model: leftSelector.delegateModel; ScrollIndicator.vertical: ScrollIndicator {} }
                            background: Rectangle { color: theme.bgCard; radius: 4; border.color: theme.borderSubtle; border.width: 1 }
                        }
                        Component.onCompleted: {
                            var defMask = MOTIONInterface.appConfig.leftMask !== undefined
                                          ? MOTIONInterface.appConfig.leftMask : 0x99
                            var idx = maskToPatternIndex(defMask)
                            currentIndex = (idx >= 0) ? idx : 4
                        }
                    }
                }

                // Right Sensor
                ColumnLayout {
                    spacing: 8
                    Layout.alignment: Qt.AlignHCenter

                    SensorView {
                        id: rightSensorView
                        title: "Right Sensor"
                        sensorSide: "right"
                        connector: MOTIONInterface
                        showFanControl: MOTIONInterface.appConfig.developerMode ? true : false
                    }

                    ComboBox {
                        id: rightSelector
                        Layout.preferredWidth: 190
                        Layout.preferredHeight: 34
                        model: sensorPatterns
                        textRole: "name"
                        font.pixelSize: 13
                        enabled: MOTIONInterface.rightSensorConnected
                        opacity: enabled ? 1.0 : 0.4
                        onCurrentIndexChanged: applyPatternToSensor(currentIndex, "right")
                        contentItem: Text {
                            leftPadding: 10; text: rightSelector.displayText; font: rightSelector.font
                            color: theme.textPrimary; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight
                        }
                        background: Rectangle { color: theme.bgInput; radius: 4; border.color: theme.borderSubtle; border.width: 1 }
                        indicator: Text { x: rightSelector.width - width - 10; y: (rightSelector.height - height) / 2; text: "\u25BE"; font.pixelSize: 14; color: theme.textSecondary }
                        delegate: ItemDelegate {
                            width: rightSelector.width; height: 32
                            contentItem: Text { text: model.name; font.pixelSize: 13; color: theme.textPrimary; verticalAlignment: Text.AlignVCenter; leftPadding: 8 }
                            background: Rectangle { color: highlighted ? theme.accentBlue : "transparent" }
                            highlighted: rightSelector.highlightedIndex === index
                        }
                        popup: Popup {
                            y: rightSelector.height; width: rightSelector.width; implicitHeight: contentItem.implicitHeight + 2; padding: 1
                            contentItem: ListView { clip: true; implicitHeight: contentHeight; model: rightSelector.delegateModel; ScrollIndicator.vertical: ScrollIndicator {} }
                            background: Rectangle { color: theme.bgCard; radius: 4; border.color: theme.borderSubtle; border.width: 1 }
                        }
                        Component.onCompleted: {
                            var defMask = MOTIONInterface.appConfig.rightMask !== undefined
                                          ? MOTIONInterface.appConfig.rightMask : 0x99
                            var idx = maskToPatternIndex(defMask)
                            currentIndex = (idx >= 0) ? idx : 0
                        }
                    }
                }
            }

            // ── Scan Duration ─────────────────────────────────────────────
            Rectangle { Layout.fillWidth: true; height: 1; color: theme.borderSubtle }

            Text {
                text: "Scan Duration"
                color: theme.textSecondary
                font.pixelSize: 15
                font.weight: Font.DemiBold
            }

            // Timed / Free Run toggle
            RowLayout {
                Layout.alignment: Qt.AlignHCenter
                spacing: 16

                Text {
                    text: "Timed"
                    color: !root.freeRun ? theme.accentBlue : theme.textSecondary
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
                        color: modeSwitch.checked ? theme.accentBlue : theme.bgInput
                        border.color: modeSwitch.checked ? theme.accentBlue : theme.borderSubtle; border.width: 1
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
                    color: root.freeRun ? theme.accentBlue : theme.textSecondary
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
                    font.pixelSize: 22; color: theme.textPrimary
                    horizontalAlignment: Text.AlignHCenter
                    Layout.preferredWidth: 58; Layout.preferredHeight: 44
                    background: Rectangle { color: theme.bgInput; radius: 6; border.color: theme.borderSubtle; border.width: 1 }
                    onEditingFinished: {
                        var v = parseInt(text); if (isNaN(v)) v = 0
                        root.hours = Math.max(0, Math.min(99, v)); text = String(root.hours)
                    }
                }
                Text { text: ":"; color: theme.textSecondary; font.pixelSize: 22 }
                TextField {
                    id: minutesField
                    text: String(root.minutes).padStart(2, '0')
                    inputMethodHints: Qt.ImhDigitsOnly
                    validator: IntValidator { bottom: 0; top: 59 }
                    font.pixelSize: 22; color: theme.textPrimary
                    horizontalAlignment: Text.AlignHCenter
                    Layout.preferredWidth: 58; Layout.preferredHeight: 44
                    background: Rectangle { color: theme.bgInput; radius: 6; border.color: theme.borderSubtle; border.width: 1 }
                    onEditingFinished: {
                        var v = parseInt(text); if (isNaN(v)) v = 0
                        root.minutes = Math.max(0, Math.min(59, v)); text = String(root.minutes).padStart(2, '0')
                    }
                }
                Text { text: ":"; color: theme.textSecondary; font.pixelSize: 22 }
                TextField {
                    id: secondsField
                    text: String(root.seconds).padStart(2, '0')
                    inputMethodHints: Qt.ImhDigitsOnly
                    validator: IntValidator { bottom: 0; top: 59 }
                    font.pixelSize: 22; color: theme.textPrimary
                    horizontalAlignment: Text.AlignHCenter
                    Layout.preferredWidth: 58; Layout.preferredHeight: 44
                    background: Rectangle { color: theme.bgInput; radius: 6; border.color: theme.borderSubtle; border.width: 1 }
                    onEditingFinished: {
                        var v = parseInt(text); if (isNaN(v)) v = 0
                        root.seconds = Math.max(0, Math.min(59, v)); text = String(root.seconds).padStart(2, '0')
                    }
                }
                Text { text: "H : M : S"; color: theme.textTertiary; font.pixelSize: 11; Layout.alignment: Qt.AlignBottom }
            }

            // Free run hint
            Text {
                visible: root.freeRun
                text: "Scan will run indefinitely until stopped."
                color: theme.textTertiary
                font.pixelSize: 13
                Layout.alignment: Qt.AlignHCenter
            }

            Item { Layout.fillHeight: true }
        }

        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape) { root.close(); event.accepted = true }
        }
    }

    Connections {
        target: MOTIONInterface
        function onConnectionStatusChanged() {
            if (!MOTIONInterface.leftSensorConnected) {
                leftSelector.currentIndex = 0
                leftSensorView.resetCamerasWhenDisconnected()
            }
            if (!MOTIONInterface.rightSensorConnected) {
                rightSelector.currentIndex = 0
                rightSensorView.resetCamerasWhenDisconnected()
            }
        }
    }
}
