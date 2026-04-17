import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

import "components"
import "pages"

ApplicationWindow {
    id: window
    visible: true
    width: 1200
    height: 800
    flags: Qt.FramelessWindowHint | Qt.Window | Qt.CustomizeWindowHint | Qt.WindowTitleHint
    color: "transparent"

    AppTheme { id: theme }

    Rectangle {
        anchors.fill: parent
        color: theme.bgBase
        radius: 20
        border.color: "transparent"

        // Header Section (with drag functionality)
        WindowMenu {
            id: headerMenu
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: parent.right

            logoSource: "../assets/images/OpenwaterLogo.png"

            // Bind session bar state from BloodFlow page
            sessionId:   bloodFlowPage.sessionId
            scanning:    bloodFlowPage.scanning
            freeRun:     bloodFlowPage.freeRun
            reducedMode:     bloodFlowPage.reducedMode
            elapsedSec:  bloodFlowPage.elapsedSec
            durationSec: bloodFlowPage.durationSec
        }

        // Update available banner (slides in below header)
        UpdateBanner {
            id: updateBanner
            anchors.top: headerMenu.bottom
            anchors.left: parent.left
            anchors.right: parent.right
        }

        // Toast notification overlay — fills the window, positions toasts in its own bottom-right corner
        NotificationCenter {
            id: notificationCenter
            anchors.fill: parent
            z: 99999
        }

        Item {
            anchors.fill: parent
            anchors.topMargin: 65 + (updateBanner.visible ? updateBanner.height : 0)
            anchors.rightMargin: 8
            anchors.bottomMargin: 8
            anchors.leftMargin: 8

            BloodFlow {
                id: bloodFlowPage
                anchors.fill: parent
            }
        }
    }

    // Bottom-right resize handle (hidden when maximized)
    Item {
        id: resizeHandle
        width: 18
        height: 18
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        visible: window.visibility !== Window.Maximized

        // Diagonal grip lines
        Canvas {
            anchors.fill: parent
            anchors.margins: 3
            onPaint: {
                var ctx = getContext("2d")
                ctx.strokeStyle = theme.borderHover
                ctx.lineWidth = 1
                var s = width
                for (var i = 0; i < 3; i++) {
                    var off = i * 4
                    ctx.beginPath()
                    ctx.moveTo(s - off, s)
                    ctx.lineTo(s, s - off)
                    ctx.stroke()
                }
            }
        }

        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.SizeFDiagCursor
            property point clickPos
            onPressed: function(mouse) {
                clickPos = Qt.point(mouse.x, mouse.y)
            }
            onPositionChanged: function(mouse) {
                var dx = mouse.x - clickPos.x
                var dy = mouse.y - clickPos.y
                var newW = Math.max(800, window.width + dx)
                var newH = Math.max(600, window.height + dy)
                window.width = newW
                window.height = newH
            }
        }
    }

    Connections {
        target: MOTIONInterface
    }

    // TEMP-NOTIF-DEBUG: remove in Task 6.
    // Press Ctrl+Shift+1..4 to fire one of each type.
    Shortcut {
        sequence: "Ctrl+Shift+1"
        onActivated: MOTIONInterface.notify("Info: this is an informational message.", "info", 4000, true)
    }
    Shortcut {
        sequence: "Ctrl+Shift+2"
        onActivated: MOTIONInterface.notify("Note saved.", "success", 4000, true)
    }
    Shortcut {
        sequence: "Ctrl+Shift+3"
        onActivated: MOTIONInterface.notify("Calibration drift exceeds threshold.", "warning", 4000, true)
    }
    Shortcut {
        sequence: "Ctrl+Shift+4"
        onActivated: MOTIONInterface.notify("Lost connection to console.", "error", 0, true)
    }
}
