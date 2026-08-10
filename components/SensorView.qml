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

    // Custom-mask mode (issue #445): when true each camera circle acts
    // as a checkbox — an affordance ring appears around it and clicking
    // emits cameraToggled with the sensorActive index. The owner mutates
    // sensorActive; nothing toggles locally.
    property bool interactive: false
    signal cameraToggled(int index)

    // One camera circle of the module diagram. activeIndex is the
    // position in ``sensorActive``; sensorId the physical camera number
    // shown in the tooltip (the two run opposite directions per column).
    component CameraCircle: Rectangle {
        property int activeIndex
        property int sensorId
        width: circleSize; height: circleSize; radius: circleSize / 2
        color: root.sensorActive[activeIndex] && root.sensorConnected ? AppTheme.accentBlue : "#666666"
        border.color: "black"; border.width: 1

        // Checkbox affordance ring — outside the circle, Custom mode only.
        Rectangle {
            visible: root.interactive
            anchors.centerIn: parent
            width: parent.width + 6; height: parent.height + 6
            radius: width / 2
            color: "transparent"
            border.color: AppTheme.borderHover; border.width: 1
        }

        MouseArea {
            id: circleArea
            anchors.fill: parent
            // Cover the ring too so it's part of the click target.
            anchors.margins: root.interactive ? -3 : 0
            hoverEnabled: true
            acceptedButtons: root.interactive ? Qt.LeftButton : Qt.NoButton
            cursorShape: root.interactive ? Qt.PointingHandCursor : Qt.ArrowCursor
            onClicked: root.cameraToggled(activeIndex)
        }
        Controls.ToolTip.visible: circleArea.containsMouse
        Controls.ToolTip.text: "Sensor ID: " + sensorId
    }


    width: 150
    height: 195
    radius: 18
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
            CameraCircle { activeIndex: 7; sensorId: 1 }
            Item {}
            CameraCircle { activeIndex: 0; sensorId: 8 }

            // Row 2
            CameraCircle { activeIndex: 6; sensorId: 2 }
            Item {}
            CameraCircle { activeIndex: 1; sensorId: 7 }

            // Row 3
            CameraCircle { activeIndex: 5; sensorId: 3 }
            Item {}
            CameraCircle { activeIndex: 2; sensorId: 6 }

            // Row 4
            CameraCircle { activeIndex: 4; sensorId: 4 }
            Item {}
            CameraCircle { activeIndex: 3; sensorId: 5 }

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
