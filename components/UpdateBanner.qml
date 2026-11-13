import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

/*  UpdateBanner — slides in below the header whenever anything has an
 *  update: the application (GitHub release check) and/or device firmware
 *  (connect-time check). One banner for both (#514); "Review" asks the
 *  host to open UpdatesModal, where each item can be updated on its own or
 *  all at once. Research builds only — clinical builds never check (#96,
 *  #386) and never show it.
 */
Rectangle {
    id: banner
    width: parent.width
    height: visible ? 36 : 0
    clip: true

    signal reviewRequested()

    readonly property bool _clinicalMode: MotionInterface.appConfig.clinicalMode === true
    readonly property bool _appAvail: MotionInterface.appUpdateAvailable
    readonly property bool _fwAvail: MotionInterface.anyFirmwareUpdateAvailable
    // Dismiss hides the current offer only; a fresh detection re-shows it.
    property bool _dismissed: false
    visible: !_clinicalMode && (_appAvail || _fwAvail) && !_dismissed

    readonly property string summary: {
        var parts = []
        if (_appAvail) parts.push("Application " + MotionInterface.appUpdateLatest)
        var fw = []
        if (MotionInterface.consoleFirmwareUpdateAvailable) fw.push("console")
        if (MotionInterface.leftSensorFirmwareUpdateAvailable) fw.push("left sensor")
        if (MotionInterface.rightSensorFirmwareUpdateAvailable) fw.push("right sensor")
        if (fw.length) parts.push("firmware (" + fw.join(", ") + ")")
        return parts.join(" · ")
    }

    color: AppTheme.accentInteractive
    radius: 0

    Behavior on height { NumberAnimation { duration: 200; easing.type: Easing.OutQuad } }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 16
        anchors.rightMargin: 12
        spacing: 10

        Text {
            text: "⚠"   // warning triangle
            font.pixelSize: 14
            color: "#FFFFFF"
        }

        Text {
            text: "<b>Updates available:</b> " + banner.summary
            color: "#FFFFFF"
            font.pixelSize: 13
            textFormat: Text.RichText
            elide: Text.ElideRight
            Layout.fillWidth: true
        }

        Rectangle {
            width: reviewBtn.implicitWidth + 20
            height: 24
            radius: 4
            color: reviewArea.containsMouse ? "#E0E0E0" : "#FFFFFF"

            Text {
                id: reviewBtn
                anchors.centerIn: parent
                text: "Review"
                color: AppTheme.accentInteractive
                font.pixelSize: 12
                font.weight: Font.DemiBold
            }

            MouseArea {
                id: reviewArea
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                hoverEnabled: true
                onClicked: banner.reviewRequested()
            }
        }

        Rectangle {
            width: 22; height: 22; radius: 11
            color: dismissArea.containsMouse ? "#FFFFFF30" : "transparent"

            Text {
                anchors.centerIn: parent
                text: "✕"
                color: "#FFFFFF"
                font.pixelSize: 12
            }

            MouseArea {
                id: dismissArea
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: banner._dismissed = true
            }
        }
    }

    Connections {
        target: MotionInterface
        function onUpdateAvailable(version, url) { banner._dismissed = false }
        function onFirmwareUpdateAvailable(deviceKey, current, latest) { banner._dismissed = false }
    }

    // Auto-check on creation (after a brief delay to let the app settle).
    // Disabled in clinical mode so clinical sessions don't make outbound
    // GitHub API calls or surface upgrade prompts.
    Timer {
        interval: 3000
        running: !banner._clinicalMode
        repeat: false
        onTriggered: MotionInterface.checkForUpdates()
    }
}
