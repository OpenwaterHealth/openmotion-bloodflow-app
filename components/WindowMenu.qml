import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

Rectangle {
    id: windowMenu
    width: parent.width
    height: 60
    color: AppTheme.bgContainer // Header background color
    radius: AppTheme.r(20)

    // The title bar is where Aero lived, so it gets the full glass
    // treatment: reflection, inner glow and rim. Invisible on every other
    // theme, which is why it can sit on the shared header unconditionally.
    ThemedSurface {
        anchors.fill: parent
        color: "transparent"
        radius: AppTheme.r(20)
    }

    // Emitted when the user clicks the exit (X) icon in the title
    // bar. main.qml owns the actual quit decision so it can show the
    // close-while-busy warning before tearing down (issue #75).
    signal closeRequested()

    // Emitted on double-click of the logo. main.qml owns the behavior
    // (opens the engineering-unlock prompt) — this component stays dumb.
    signal logoDoubleClicked()

    // Properties to configure the logo
    property string logoSource: "" // Default to no logo

    // Session bar state (bound from BloodFlow page)
    property string sessionId: ""
    property bool   scanning: false
    property bool   freeRun: false
    property bool   clinicalMode: false
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
            radius: AppTheme.r(6)

            // Dark-mode: show the original white logo directly
            Image {
                source: windowMenu.logoSource
                anchors.fill: parent
                fillMode: Image.PreserveAspectFit
                smooth: true
                visible: AppTheme.dark && windowMenu.logoSource !== ""
            }
            // Light-mode: paint a dark logo using the white image as an
            // opacity mask over a solid-colour Canvas.  No shaders needed.
            Item {
                anchors.fill: parent
                visible: !AppTheme.dark && windowMenu.logoSource !== ""

                Canvas {
                    id: logoDarkCanvas
                    anchors.fill: parent
                    // Re-render whenever the theme changes or the image loads
                    property color tint: AppTheme.textPrimary
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

            // Double-click → engineering-mode unlock prompt (eng gate).
            // Sits above the header drag MouseArea so only the logo area
            // captures the double-click; the rest of the bar still drags.
            MouseArea {
                anchors.fill: parent
                acceptedButtons: Qt.LeftButton
                onDoubleClicked: windowMenu.logoDoubleClicked()
            }
        }

        // Session info bar (replaces old title + version block)
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 34
            color: AppTheme.bgElevated
            radius: AppTheme.r(8)
            // The chisel replaces the flat border on bevelled themes.
            border.color: AppTheme.bevel ? "transparent" : AppTheme.borderSubtle
            border.width: AppTheme.bevel ? 0 : 1

            // The session bar is the one always-present chrome surface wide
            // enough to carry a material: Aqua/Aero put their sheen here, and
            // Windows Classic sinks it like a status well. Invisible on flat
            // themes. Declared before the row so it paints underneath.
            ThemedSurface {
                anchors.fill: parent
                color: "transparent"
                radius: AppTheme.r(8)
                raised: false      // a readout is recessed, not a button
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 14
                anchors.rightMargin: 14
                spacing: 10

                Text { text: "Session:"; color: AppTheme.textTertiary; font.pixelSize: 13 }
                Text {
                    text: windowMenu.sessionId || "—"
                    color: AppTheme.textLink
                    font.pixelSize: 13
                    font.weight: Font.DemiBold
                }

                Item { Layout.fillWidth: true }

                Item {
                    implicitWidth: appTitle.implicitWidth
                    implicitHeight: appTitle.implicitHeight

                    // Vista's title-bar text glow. Glass shows whatever is
                    // behind it, so Windows drew a soft halo behind the
                    // caption to hold contrast no matter what that was.
                    // Approximated by stamping the string in the glow colour
                    // at eight offsets — cheaper than a blur effect and, at
                    // this size, indistinguishable from one.
                    Repeater {
                        model: [[-1,-1],[0,-1],[1,-1],[-1,0],
                                [1,0],[-1,1],[0,1],[1,1]]
                        delegate: Text {
                            visible: AppTheme.aeroGlass
                            x: modelData[0]; y: modelData[1]
                            text: appTitle.text
                            color: AppTheme.textGlow
                            font: appTitle.font
                            opacity: 0.55
                        }
                    }

                    Text {
                        id: appTitle
                        text: "Open-Motion"
                        color: AppTheme.textPrimary
                        font.pixelSize: 14
                        font.weight: Font.Bold
                    }
                }

                Rectangle {
                    visible: !windowMenu.clinicalMode
                    width: betaLabel.implicitWidth + 12
                    height: betaLabel.implicitHeight + 4
                    radius: AppTheme.r(4)
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
                            if (windowMenu.clinicalMode) {
                                return windowMenu.scanning
                                    ? windowMenu.formatSec(windowMenu.elapsedSec) : ""
                            }
                            return windowMenu.scanning
                                ? "Continuous  " + windowMenu.formatSec(windowMenu.elapsedSec)
                                : "Continuous"
                        }
                        return windowMenu.scanning
                            ? windowMenu.formatSec(windowMenu.elapsedSec) + " / " + windowMenu.formatSec(windowMenu.durationSec)
                            : windowMenu.formatSec(windowMenu.durationSec)
                    }
                    color: windowMenu.scanning ? AppTheme.statusGreen : AppTheme.textTertiary
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
                squareGlyph: "–"   // en dash — the Win95 minimize bar
                Layout.alignment: Qt.AlignHCenter
                onClicked: {
                    window.showMinimized(); // Minimize the window
                }
            }
            // Maximize/Restore Button
            IconWindowButton {
                buttonIcon: window.visibility === Window.Maximized ? "\uea47" : "\ueb18"
                squareGlyph: window.visibility === Window.Maximized ? "❐" : "□"
                Layout.alignment: Qt.AlignHCenter
                onClicked: {
                    if (window.visibility === Window.Maximized) {
                        window.showNormal();
                    } else {
                        window.showMaximized();
                    }
                }
            }
            // Exit Button. Delegates to main.qml via closeRequested
            // so the close-while-busy warning (#75) can intercept.
            IconWindowButton {
                buttonIcon: "\ue9b3" // Exit (close) icon
                squareGlyph: "✕"
                isClose: true
                Layout.alignment: Qt.AlignHCenter
                onClicked: windowMenu.closeRequested()
            }
        }
    }
}
