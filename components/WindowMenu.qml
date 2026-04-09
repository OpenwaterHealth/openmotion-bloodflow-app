import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0

Rectangle {
    id: windowMenu
    AppTheme { id: theme }
    width: parent.width
    height: 60
    color: theme.bgContainer // Header background color
    radius: 20

    // Properties to configure the logo
    property string logoSource: "" // Default to no logo

    // Session bar state (bound from BloodFlow page)
    property string sessionId: ""
    property bool   scanning: false
    property bool   freeRun: false
    property bool   reducedMode: false
    property int    elapsedSec: 0
    property int    durationSec: 3600


    function formatSec(s) {
        var h = Math.floor(s / 3600)
        var m = Math.floor((s % 3600) / 60)
        var sec = s % 60
        return String(h).padStart(2, '0') + ":" +
               String(m).padStart(2, '0') + ":" +
               String(sec).padStart(2, '0')
    }

    // Drag functionality
    MouseArea {
        id: headerMouseArea
        anchors.fill: parent
        cursorShape: Qt.SizeAllCursor
        onPressed: function(mouse) {
            if (mouse.button === Qt.LeftButton) {
                window.startSystemMove(); // Allow window dragging
            }
        }
    }

    RowLayout {
        anchors.fill: parent
        anchors.margins: 10
        spacing: 10

        // Logo
        Rectangle {
            width: 185
            height: 42
            color: "transparent" // No background color
            radius: 6

            // Dark-mode: show the original white logo directly
            Image {
                source: windowMenu.logoSource
                anchors.fill: parent
                fillMode: Image.PreserveAspectFit
                smooth: true
                visible: theme.dark && windowMenu.logoSource !== ""
            }
            // Light-mode: paint a dark logo using the white image as an
            // opacity mask over a solid-colour Canvas.  No shaders needed.
            Item {
                anchors.fill: parent
                visible: !theme.dark && windowMenu.logoSource !== ""

                Canvas {
                    id: logoDarkCanvas
                    anchors.fill: parent
                    // Re-render whenever the theme changes or the image loads
                    property color tint: theme.textPrimary
                    onTintChanged: requestPaint()

                    Image {
                        id: logoSrc
                        source: windowMenu.logoSource
                        visible: false          // hidden; only used as a pixel source
                        onStatusChanged: if (status === Image.Ready) logoDarkCanvas.requestPaint()
                    }

                    onPaint: {
                        var ctx = getContext("2d")
                        ctx.reset()
                        if (logoSrc.status !== Image.Ready) return

                        // Scale image to fit, centered, preserving aspect ratio
                        var sx = width  / logoSrc.sourceSize.width
                        var sy = height / logoSrc.sourceSize.height
                        var s  = Math.min(sx, sy)
                        var dw = logoSrc.sourceSize.width  * s
                        var dh = logoSrc.sourceSize.height * s
                        var dx = (width  - dw) / 2
                        var dy = (height - dh) / 2

                        // Draw the original image (for its alpha)
                        ctx.drawImage(logoSrc, dx, dy, dw, dh)
                        // Composite: replace colour but keep alpha
                        ctx.globalCompositeOperation = "source-atop"
                        ctx.fillStyle = tint.toString()
                        ctx.fillRect(0, 0, width, height)
                    }
                }
            }
        }

        // Session info bar (replaces old title + version block)
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 34
            color: theme.bgElevated
            radius: 8
            border.color: theme.borderSubtle
            border.width: 1

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 14
                anchors.rightMargin: 14
                spacing: 10

                Text { text: "Session:"; color: theme.textTertiary; font.pixelSize: 13 }
                Text {
                    text: windowMenu.sessionId || "—"
                    color: theme.textLink
                    font.pixelSize: 13
                    font.weight: Font.DemiBold
                }

                Item { Layout.fillWidth: true }

                Text {
                    text: "OpenMotion BloodFlow"
                    color: theme.textPrimary
                    font.pixelSize: 14
                    font.weight: Font.Bold
                }

                Rectangle {
                    width: betaLabel.implicitWidth + 12
                    height: betaLabel.implicitHeight + 4
                    radius: 4
                    color: "#E67E22"
                    Text {
                        id: betaLabel
                        anchors.centerIn: parent
                        text: "BETA"
                        color: "#FFFFFF"
                        font.pixelSize: 10
                        font.weight: Font.Bold
                        font.letterSpacing: 1
                    }
                }

                Item { Layout.fillWidth: true }

                Text {
                    text: {
                        if (windowMenu.freeRun) {
                            if (windowMenu.reducedMode) {
                                return windowMenu.scanning
                                    ? windowMenu.formatSec(windowMenu.elapsedSec) : ""
                            }
                            return windowMenu.scanning
                                ? "Free Run  " + windowMenu.formatSec(windowMenu.elapsedSec)
                                : "Free Run"
                        }
                        return windowMenu.scanning
                            ? windowMenu.formatSec(windowMenu.elapsedSec) + " / " + windowMenu.formatSec(windowMenu.durationSec)
                            : windowMenu.formatSec(windowMenu.durationSec)
                    }
                    color: windowMenu.scanning ? theme.statusGreen : theme.textTertiary
                    font.pixelSize: 13
                    font.family: "Courier New"
                }
            }
        }

        // Window control buttons
        RowLayout {
            spacing: 10
            Layout.alignment: Qt.AlignRight

            // Minimize Button
            IconWindowButton {
                buttonIcon: "\ue9e4" // Minimize icon
                Layout.alignment: Qt.AlignHCenter
                onClicked: {
                    window.showMinimized(); // Minimize the window
                }
            }
            // Maximize/Restore Button
            IconWindowButton {
                buttonIcon: window.visibility === Window.Maximized ? "\uea47" : "\ueb18"
                Layout.alignment: Qt.AlignHCenter
                onClicked: {
                    if (window.visibility === Window.Maximized) {
                        window.showNormal();
                    } else {
                        window.showMaximized();
                    }
                }
            }
            // Exit Button
            IconWindowButton {
                buttonIcon: "\ue9b3" // Exit (close) icon
                Layout.alignment: Qt.AlignHCenter
                onClicked: {
                    console.log("User pressed quit button")
                    Qt.quit(); // Close the application
                }
            }
        }
    }
}
