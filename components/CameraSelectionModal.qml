import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 9998

    signal selectionChanged(int leftMask, int rightMask)

    property var leftSensorActive: [false, false, false, false, false, false, false, false]
    property var rightSensorActive: [false, false, false, false, false, false, false, false]

    ListModel {
        id: sensorPatterns
        ListElement { name: "None"; maskHex: "0x00" }
        ListElement { name: "Near"; maskHex: "0x5A" }
        ListElement { name: "Middle"; maskHex: "0x66" }
        ListElement { name: "Far"; maskHex: "0x55" }
        ListElement { name: "Outer"; maskHex: "0x99" }
        ListElement { name: "Left"; maskHex: "0x0F" }
        ListElement { name: "Right"; maskHex: "0xF0" }
        ListElement { name: "Third Row"; maskHex: "0x42" }
        ListElement { name: "All"; maskHex: "0xFF" }
    }

    function maskFromArray(arr) {
        if (!arr || arr.length !== 8) return 0;
        const bitMap = [7, 6, 5, 4, 3, 2, 1, 0];
        var m = 0;
        for (var i = 0; i < 8; i++) {
            if (arr[i]) m |= (1 << bitMap[i]);
        }
        return m;
    }

    function applyPatternToSensor(index, side) {
        var pattern;
        switch (index) {
            case 0: pattern = [false,false,false,false,false,false,false,false]; break;
            case 1: pattern = [false,true,false,true,true,false,true,false]; break;
            case 2: pattern = [false,true,true,false,false,true,true,false]; break;
            case 3: pattern = [true,false,true,false,false,true,false,true]; break;
            case 4: pattern = [true,false,false,true,true,false,false,true]; break;
            case 5: pattern = [false,false,false,false,true,true,true,true]; break;
            case 6: pattern = [true,true,true,true,false,false,false,false]; break;
            case 7: pattern = [false,true,false,false,false,false,true,false]; break;
            case 8: pattern = [true,true,true,true,true,true,true,true]; break;
            default: return;
        }
        if (side === "left") {
            leftSensorActive = pattern;
            leftSensorView.sensorActive = pattern;
        } else {
            rightSensorActive = pattern;
            rightSensorView.sensorActive = pattern;
        }
    }

    function open() { root.visible = true }
    function close() {
        selectionChanged(maskFromArray(leftSensorActive), maskFromArray(rightSensorActive))
        root.visible = false
    }

    function setInitialSelection(leftArr, rightArr) {
        leftSensorActive = leftArr
        rightSensorActive = rightArr
        leftSensorView.sensorActive = leftArr
        rightSensorView.sensorActive = rightArr
    }

    // Dimmed backdrop
    Rectangle {
        anchors.fill: parent
        color: "#000000AA"
        MouseArea { anchors.fill: parent; onClicked: {} }
    }

    // Dialog panel
    Rectangle {
        width: 520
        height: 500
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
            anchors.margins: 20
            spacing: 16

            Text {
                text: "Camera Selection"
                color: "#FFFFFF"
                font.pixelSize: 20
                font.weight: Font.Bold
                Layout.alignment: Qt.AlignHCenter
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 30
                Layout.alignment: Qt.AlignHCenter

                // Left Sensor
                ColumnLayout {
                    spacing: 10
                    Layout.alignment: Qt.AlignHCenter

                    SensorView {
                        id: leftSensorView
                        title: "Left Sensor"
                        sensorSide: "left"
                        connector: MOTIONInterface
                    }

                    ComboBox {
                        id: leftSelector
                        Layout.preferredWidth: 200
                        Layout.preferredHeight: 36
                        model: sensorPatterns
                        textRole: "name"
                        enabled: MOTIONInterface.leftSensorConnected
                        opacity: enabled ? 1.0 : 0.4
                        onCurrentIndexChanged: applyPatternToSensor(currentIndex, "left")

                        Component.onCompleted: currentIndex = 4
                    }
                }

                // Right Sensor
                ColumnLayout {
                    spacing: 10
                    Layout.alignment: Qt.AlignHCenter

                    SensorView {
                        id: rightSensorView
                        title: "Right Sensor"
                        sensorSide: "right"
                        connector: MOTIONInterface
                    }

                    ComboBox {
                        id: rightSelector
                        Layout.preferredWidth: 200
                        Layout.preferredHeight: 36
                        model: sensorPatterns
                        textRole: "name"
                        enabled: MOTIONInterface.rightSensorConnected
                        opacity: enabled ? 1.0 : 0.4
                        onCurrentIndexChanged: applyPatternToSensor(currentIndex, "right")

                        Component.onCompleted: currentIndex = 0
                    }
                }
            }

            Item { Layout.preferredHeight: 8 }
        }

        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape) {
                root.close()
                event.accepted = true
            }
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
