import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import QtQuick.Controls as Controls
import OpenMotion 1.0

Rectangle {
    id: root
    property string title: "Sensor"
    property int circleSize: 15
    property var sensorActive: [false, false, false, false, false, false, false, false]
    property bool fanOn: false
    property bool showFanControl: true
    property string sensorSide: "left"  // "left" or "right"
    property var connector


    width: 150
    height: 195
    radius: AppTheme.r(18)
    color: AppTheme.bgContainer
    border.color: sensorConnected ? AppTheme.borderSubtle : "#6E3E3F"
    border.width: 2
    opacity: sensorConnected ? 1.0 : 0.4
    enabled: sensorConnected

    property bool sensorConnected: (sensorSide === "left" && connector && connector.leftSensorConnected) || 
                                   (sensorSide === "right" && connector && connector.rightSensorConnected)

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 8
        spacing: 6

        Text {
            text: root.title
            font.pixelSize: 14
            color: root.sensorConnected ? AppTheme.textSecondary : "#8B8B8D"
            horizontalAlignment: Text.AlignHCenter
            Layout.alignment: Qt.AlignHCenter
        }

        GridLayout {
            columns: 3
            columnSpacing: 16
            rowSpacing: 8
            Layout.alignment: Qt.AlignHCenter

            // Row 1
            Rectangle { width: circleSize; height: circleSize; radius: circleSize/2
                color: sensorActive[7] && root.sensorConnected ? AppTheme.accentBlue : "#666666"; border.color: "black"; border.width: 1
                MouseArea { id: sensor1HoverArea; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                Controls.ToolTip.visible: sensor1HoverArea.containsMouse
                Controls.ToolTip.text: "Sensor ID: 1"
            }
            Item {}
            Rectangle { width: circleSize; height: circleSize; radius: circleSize/2
                color: sensorActive[0] && root.sensorConnected ? AppTheme.accentBlue : "#666666"; border.color: "black"; border.width: 1
                MouseArea { id: sensor2HoverArea; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                Controls.ToolTip.visible: sensor2HoverArea.containsMouse
                Controls.ToolTip.text: "Sensor ID: 8"
            }

            // Row 2
            Rectangle { width: circleSize; height: circleSize; radius: circleSize/2
                color: sensorActive[6] && root.sensorConnected ? AppTheme.accentBlue : "#666666"; border.color: "black"; border.width: 1
                MouseArea { id: sensor3HoverArea; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                Controls.ToolTip.visible: sensor3HoverArea.containsMouse
                Controls.ToolTip.text: "Sensor ID: 2"
            }
            Item {}
            Rectangle { width: circleSize; height: circleSize; radius: circleSize/2
                color: sensorActive[1] && root.sensorConnected ? AppTheme.accentBlue : "#666666"; border.color: "black"; border.width: 1
                MouseArea { id: sensor4HoverArea; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                Controls.ToolTip.visible: sensor4HoverArea.containsMouse
                Controls.ToolTip.text: "Sensor ID: 7"
            }

            // Row 3
            Rectangle { width: circleSize; height: circleSize; radius: circleSize/2
                color: sensorActive[5] && root.sensorConnected ? AppTheme.accentBlue : "#666666"; border.color: "black"; border.width: 1
                MouseArea { id: sensor5HoverArea; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                Controls.ToolTip.visible: sensor5HoverArea.containsMouse
                Controls.ToolTip.text: "Sensor ID: 3"
            }
            Item {}
            Rectangle { width: circleSize; height: circleSize; radius: circleSize/2
                color: sensorActive[2] && root.sensorConnected ? AppTheme.accentBlue : "#666666"; border.color: "black"; border.width: 1
                MouseArea { id: sensor6HoverArea; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                Controls.ToolTip.visible: sensor6HoverArea.containsMouse
                Controls.ToolTip.text: "Sensor ID: 6"
            }

            // Row 4
            Rectangle { width: circleSize; height: circleSize; radius: circleSize/2
                color: sensorActive[4] && root.sensorConnected ? AppTheme.accentBlue : "#666666"; border.color: "black"; border.width: 1
                MouseArea { id: sensor7HoverArea; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                Controls.ToolTip.visible: sensor7HoverArea.containsMouse
                Controls.ToolTip.text: "Sensor ID: 4"
            }
            Item {}
            Rectangle { width: circleSize; height: circleSize; radius: circleSize/2
                color: sensorActive[3] && root.sensorConnected ? AppTheme.accentBlue : "#666666"; border.color: "black"; border.width: 1
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
            width: 22
            height: 22
            opacity: parent.checked ? 1.0 : 0.5
            anchors.left: parent.left
            anchors.leftMargin: 8
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
    }

    // Update fan status only when THIS side's connected flag actually
    // toggles. Listening to the connector's connectionStatusChanged
    // would fire on every state transition of every handle, causing
    // each SensorView to re-poll its fan whenever the OTHER side moved.
    //
    // Do NOT zero ``sensorActive`` on disconnect — the per-circle color
    // bindings above already AND with ``root.sensorConnected``, so the
    // circles gray themselves out automatically while disconnected. The
    // old reset destroyed the source-of-truth pattern, and since no one
    // restores it on reconnect, the circles stayed gray forever after
    // a console power cycle even though the dropdown still showed the
    // selected pattern (issue #127).
    onSensorConnectedChanged: {
        updateFanStatus()
    }

    // Helper function to update fan status
    function updateFanStatus() {
        if (connector && sensorConnected) {
            root.fanOn = connector.getFanControlStatus(root.sensorSide)
        } else {
            root.fanOn = false
        }
    }
}
