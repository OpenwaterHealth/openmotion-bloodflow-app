import QtQuick 6.0
import QtQuick.Controls 6.0

Rectangle {
    width: 160
    height: 160
    color: "transparent"

    AppTheme { id: theme }

    // === Properties for IMU data ===
    property string imuLabel: "IMU Data"
    property string mode: "Accel"     // or "Gyro"
    property int xVal: 0
    property int yVal: 0
    property int zVal: 0

    // === Border Circle ===
    Rectangle {
        width: 140
        height: 140
        radius: 70
        anchors.centerIn: parent
        border.color: "#D0D3D4"
        border.width: 4
        color: "transparent"
    }

    // === IMU Values ===
    Column {
        anchors.centerIn: parent
        spacing: 4

        Text {
            text: mode
            font.pixelSize: 16
            font.bold: true
            color: "#2C3E50"
            horizontalAlignment: Text.AlignHCenter
            anchors.horizontalCenter: parent.horizontalCenter
        }

        Text {
            text: "X: " + xVal
            font.pixelSize: 14
            color: "#3498DB"
            anchors.horizontalCenter: parent.horizontalCenter
        }

        Text {
            text: "Y: " + yVal
            font.pixelSize: 14
            color: "#27AE60"
            anchors.horizontalCenter: parent.horizontalCenter
        }

        Text {
            text: "Z: " + zVal
            font.pixelSize: 14
            color: "#E67E22"
            anchors.horizontalCenter: parent.horizontalCenter
        }
    }

    // === Label Below Widget ===
    Text {
        text: imuLabel
        anchors {
            top: parent.bottom
            horizontalCenter: parent.horizontalCenter
            topMargin: 5
        }
        font.pixelSize: 16
        color: theme.textSecondary
        font.weight: Font.Medium
    }
}
