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
            LaserDot { size: circleSize }
            Item {}
        }

    }
}
