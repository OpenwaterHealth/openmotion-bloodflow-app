import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

/*  FirmwareUpdateBanner — slides in below the header when newer device
 *  firmware is available. developerMode-gated (technician task). The "View"
 *  button asks the host to open the Settings overlay (firmware card).
 */
Rectangle {
    id: banner
    width: parent.width
    height: visible ? 36 : 0
    clip: true

    AppTheme { id: theme }

    signal viewRequested()

    readonly property bool _devMode: MotionInterface.appConfig.developerMode === true
    property bool _dismissed: false
    visible: _devMode && MotionInterface.anyFirmwareUpdateAvailable && !_dismissed

    color: theme.accentBlue
    radius: 0
    Behavior on height { NumberAnimation { duration: 200; easing.type: Easing.OutQuad } }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 16
        anchors.rightMargin: 12
        spacing: 10

        Text { text: "⚠"; font.pixelSize: 14; color: "#FFFFFF" }

        Text {
            text: "Device firmware update available"
            color: "#FFFFFF"
            font.pixelSize: 13
            Layout.fillWidth: true
        }

        Rectangle {
            width: viewBtn.implicitWidth + 20; height: 24; radius: 4; color: "#FFFFFF"
            Text {
                id: viewBtn
                anchors.centerIn: parent
                text: "View"
                color: theme.accentBlue
                font.pixelSize: 12
                font.weight: Font.DemiBold
            }
            MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                hoverEnabled: true
                onClicked: banner.viewRequested()
                onContainsMouseChanged: parent.color = containsMouse ? "#E0E0E0" : "#FFFFFF"
            }
        }

        Rectangle {
            width: 22; height: 22; radius: 11
            color: dismissArea.containsMouse ? "#FFFFFF30" : "transparent"
            Text { anchors.centerIn: parent; text: "✕"; color: "#FFFFFF"; font.pixelSize: 12 }
            MouseArea {
                id: dismissArea
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: banner._dismissed = true
            }
        }
    }

    // Re-show on a fresh detection after a prior dismiss.
    Connections {
        target: MotionInterface
        function onFirmwareUpdateAvailable(deviceKey, current, latest) {
            banner._dismissed = false
        }
    }
}
