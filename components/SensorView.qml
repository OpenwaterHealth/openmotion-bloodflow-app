import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import QtQuick.Controls as Controls

Rectangle {
    id: root
    property string title: "Sensor"
    property int circleSize: 20
    property var sensorActive: [false, false, false, false, false, false, false, false]
    property bool fanOn: false
    property bool showFanControl: true
    property string sensorSide: "left"  // "left" or "right"
    property var connector

    AppTheme { id: theme }

    width: 200
    height: 260
    radius: 10
    color: theme.bgContainer
    border.color: sensorConnected ? theme.borderSubtle : "#6E3E3F"
    border.width: 2
    opacity: sensorConnected ? 1.0 : 0.4
    enabled: sensorConnected

    property bool sensorConnected: (sensorSide === "left" && connector && connector.leftSensorConnected) || 
                                   (sensorSide === "right" && connector && connector.rightSensorConnected)

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 10
        spacing: 10

        Text {
            text: root.title
            font.pixelSize: 16
            color: root.sensorConnected ? theme.textSecondary : "#8B8B8D"
            horizontalAlignment: Text.AlignHCenter
            Layout.alignment: Qt.AlignHCenter
        }

        GridLayout {
            columns: 3
            columnSpacing: 20
            rowSpacing: 10
            Layout.alignment: Qt.AlignHCenter

            // Row 1
            Rectangle { width: circleSize; height: circleSize; radius: circleSize/2
                color: sensorActive[7] && root.sensorConnected ? theme.accentBlue : "#666666"; border.color: "black"; border.width: 1
                MouseArea { id: sensor1HoverArea; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                Controls.ToolTip.visible: sensor1HoverArea.containsMouse
                Controls.ToolTip.text: "Sensor ID: 1"
            }
            Item {}
            Rectangle { width: circleSize; height: circleSize; radius: circleSize/2
                color: sensorActive[0] && root.sensorConnected ? theme.accentBlue : "#666666"; border.color: "black"; border.width: 1
                MouseArea { id: sensor2HoverArea; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                Controls.ToolTip.visible: sensor2HoverArea.containsMouse
                Controls.ToolTip.text: "Sensor ID: 8"
            }

            // Row 2
            Rectangle { width: circleSize; height: circleSize; radius: circleSize/2
                color: sensorActive[6] && root.sensorConnected ? theme.accentBlue : "#666666"; border.color: "black"; border.width: 1
                MouseArea { id: sensor3HoverArea; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                Controls.ToolTip.visible: sensor3HoverArea.containsMouse
                Controls.ToolTip.text: "Sensor ID: 2"
            }
            Item {}
            Rectangle { width: circleSize; height: circleSize; radius: circleSize/2
                color: sensorActive[1] && root.sensorConnected ? theme.accentBlue : "#666666"; border.color: "black"; border.width: 1
                MouseArea { id: sensor4HoverArea; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                Controls.ToolTip.visible: sensor4HoverArea.containsMouse
                Controls.ToolTip.text: "Sensor ID: 7"
            }

            // Row 3
            Rectangle { width: circleSize; height: circleSize; radius: circleSize/2
                color: sensorActive[5] && root.sensorConnected ? theme.accentBlue : "#666666"; border.color: "black"; border.width: 1
                MouseArea { id: sensor5HoverArea; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                Controls.ToolTip.visible: sensor5HoverArea.containsMouse
                Controls.ToolTip.text: "Sensor ID: 3"
            }
            Item {}
            Rectangle { width: circleSize; height: circleSize; radius: circleSize/2
                color: sensorActive[2] && root.sensorConnected ? theme.accentBlue : "#666666"; border.color: "black"; border.width: 1
                MouseArea { id: sensor6HoverArea; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                Controls.ToolTip.visible: sensor6HoverArea.containsMouse
                Controls.ToolTip.text: "Sensor ID: 6"
            }

            // Row 4
            Rectangle { width: circleSize; height: circleSize; radius: circleSize/2
                color: sensorActive[4] && root.sensorConnected ? theme.accentBlue : "#666666"; border.color: "black"; border.width: 1
                MouseArea { id: sensor7HoverArea; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                Controls.ToolTip.visible: sensor7HoverArea.containsMouse
                Controls.ToolTip.text: "Sensor ID: 4"
            }
            Item {}
            Rectangle { width: circleSize; height: circleSize; radius: circleSize/2
                color: sensorActive[3] && root.sensorConnected ? theme.accentBlue : "#666666"; border.color: "black"; border.width: 1
                MouseArea { id: sensor8HoverArea; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                Controls.ToolTip.visible: sensor8HoverArea.containsMouse
                Controls.ToolTip.text: "Sensor ID: 5"
            }

            // Row 5 - Laser
            Item {}
            Rectangle {
                width: circleSize; height: circleSize; radius: circleSize/2
                color: "#FFD700"  // Yellow laser
                border.color: "black"; border.width: 1
            }
            Item {}
        }

    }
    
    // Fan Control CheckBox - positioned in top right corner
    CheckBox {
        id: fanButton
        visible: root.showFanControl
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.topMargin: 12
        anchors.rightMargin: 0
        checked: root.fanOn
        hoverEnabled: true
        
        indicator: Image {
            source: "../assets/images/icons8-fan-30.png"
            width: 30
            height: 30
            opacity: parent.checked ? 1.0 : 0.5
            anchors.left: parent.left
            anchors.leftMargin: 10
        }
        
        onToggled: {
            // Toggle fan state
            var newFanState = checked
            if (connector) {
                var success = connector.setFanControl(root.sensorSide, newFanState)
                if (success) {
                    root.fanOn = newFanState
                } else {
                    console.log("Failed to toggle fan for", root.sensorSide, "sensor")
                }
            } else {
                console.log("MotionInterface not available")
            }
        }
    }
    
    // Initialize fan status when component loads
    Component.onCompleted: {
        updateFanStatus()
        resetCamerasWhenDisconnected()
    }
    
    // Update fan status when connection changes
    Connections {
        target: connector
        function onConnectionStatusChanged() {
            updateFanStatus()
            resetCamerasWhenDisconnected()
        }
    }
    
    // Helper function to update fan status
    function updateFanStatus() {
        if (connector && sensorConnected) {
            root.fanOn = connector.getFanControlStatus(root.sensorSide)
        } else {
            root.fanOn = false
        }
    }
    
    // Helper function to reset cameras when sensor disconnects
    function resetCamerasWhenDisconnected() {
        if (!sensorConnected) {
            root.sensorActive = [false, false, false, false, false, false, false, false]
        }
    }
}
