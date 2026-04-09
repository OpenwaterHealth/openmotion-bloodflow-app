import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Shapes 6.0

Rectangle {
    width: 160
    height: 160
    color: "transparent"

    AppTheme { id: theme }

    property real temperature: 0    // Starting temperature
    property string tempName: "TEMP #1"  // Temperature Name

    // Dynamic Color Selection Logic
    property color gaugeColor: temperature <= 40 ? "#3498DB" :
                               (temperature <= 70 ? "#F1C40F" : "#E74C3C")

    // Temperature Arc
    Canvas {
        id: arcCanvas
        anchors.centerIn: parent
        width: 140
        height: 140

        onPaint: {
            var ctx = getContext("2d")
            ctx.reset()

            // Background Arc (Light Gray)
            ctx.beginPath()
            ctx.arc(width / 2, height / 2, 60, Math.PI * 0.75, Math.PI * 2.25, false)
            ctx.lineWidth = 10
            ctx.strokeStyle = "#D0D3D4"
            ctx.stroke()

            // Foreground Arc (Dynamic Color)
            var angle = (temperature / 70) * 270  // Correct scaling for 0-70°C range
            ctx.beginPath()
            ctx.arc(width / 2, height / 2, 60, Math.PI * 0.75, Math.PI * (0.75 + (angle / 180)), false)
            ctx.lineWidth = 10
            ctx.strokeStyle = gaugeColor
            ctx.stroke()
        }
    }

    // Redraw the Canvas when temperature changes
    onTemperatureChanged: {
        arcCanvas.requestPaint()  // Force the Canvas to redraw
    }

    // Temperature Value Text
    Text {
        text: temperature.toFixed(0) + "°C"
        anchors.centerIn: parent
        font.pixelSize: 24
        color: theme.textTertiary
        font.weight: Font.Bold
    }

    // Temperature Name Below
    Text {
        text: tempName
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
