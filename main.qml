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

    // Issue #75: aggregate 'something is happening' state used by the
    // close-while-busy warning. Anything that the user would not want
    // to interrupt mid-flight by accident counts.
    readonly property bool _anyInProgress:
        bloodFlowPage.scanning ||
        bloodFlowPage.configuring ||
        bloodFlowPage.checkRunning ||
        MotionInterface.calibrationRunning ||
        MotionInterface.testScanRunning

    // Most-specific in-progress label for the warn-toast text. First
    // match wins — calibration is rarest + costliest to interrupt,
    // scan next, configuration / contact-quality check after.
    //
    // If a non-dismissable modal is on screen at click time
    // (ContactQualityModal mid-check, etc.), prefer its declared
    // ``label`` — that's the most user-meaningful description of
    // what's blocking the close. Modals expose ``label`` as part of
    // their formal interface; see HistoryModal.qml for the contract.
    function _inProgressLabel() {
        var m = bloodFlowPage.modalManager.current
        if (m && m.dismissable === false && m.label) return m.label
        if (MotionInterface.calibrationRunning) return "Calibration"
        if (MotionInterface.testScanRunning)    return "Test scan"
        if (bloodFlowPage.scanning)             return "Scan"
        if (bloodFlowPage.configuring)          return "Camera configuration"
        if (bloodFlowPage.checkRunning)         return "Contact-quality check"
        return ""
    }

    // Two-click exit while busy: the first close attempt arms this
    // flag and shows a toast; a second click within the toast's
    // lifetime quits. The timer auto-disarms after the toast goes
    // away so the warning has to be re-shown if the user comes back
    // later.
    property bool _exitArmed: false
    readonly property int _exitArmDurationMs: 5000

    Timer {
        id: exitDisarmTimer
        interval: window._exitArmDurationMs
        repeat: false
        onTriggered: window._exitArmed = false
    }

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

            // Issue #75: warn-then-quit on close-while-busy.
            //   * Always: dismiss the open modal first (if any +
            //     dismissable) so its close() saves before we proceed.
            //   * not busy → quit immediately
            //   * busy + not armed → fire toast, arm; auto-disarm when
            //     the toast naturally expires
            //   * busy + armed → quit (existing handle_exit path in
            //     main.py runs connector.shutdown() / motion_interface
            //     .stop() which tears down whatever was in flight)
            onCloseRequested: {
                bloodFlowPage.modalManager.closeCurrent()
                if (!window._anyInProgress) {
                    Qt.quit()
                    return
                }
                if (window._exitArmed) {
                    Qt.quit()
                    return
                }
                MotionInterface.notify(
                    window._inProgressLabel()
                        + " in progress.\nClick X again to cancel and exit.",
                    "warning",
                    window._exitArmDurationMs,
                    true,
                    "exit-while-busy",
                )
                window._exitArmed = true
                exitDisarmTimer.restart()
            }
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
        target: MotionInterface
    }

    TestResultsWindow {
        id: testResultsWindow
    }
}
