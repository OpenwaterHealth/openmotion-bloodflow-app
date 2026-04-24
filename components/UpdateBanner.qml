import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

/*  UpdateBanner — slides in below the header when a newer release is
 *  detected on GitHub.  Offers a "Download" button (opens browser) and
 *  a dismiss "✕".
 */
Rectangle {
    id: banner
    width: parent.width
    height: visible ? 36 : 0
    visible: false
    clip: true

    AppTheme { id: theme }

    property string latestVersion: ""
    property string downloadUrl: ""

    color: theme.accentBlue
    radius: 0

    Behavior on height { NumberAnimation { duration: 200; easing.type: Easing.OutQuad } }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 16
        anchors.rightMargin: 12
        spacing: 10

        Text {
            text: "\u26A0"   // warning triangle
            font.pixelSize: 14
            color: "#FFFFFF"
        }

        Text {
            text: "A new version is available: <b>" + banner.latestVersion + "</b>"
            color: "#FFFFFF"
            font.pixelSize: 13
            textFormat: Text.RichText
            Layout.fillWidth: true
        }

        Rectangle {
            width: downloadBtn.implicitWidth + 20
            height: 24
            radius: 4
            color: "#FFFFFF"

            Text {
                id: downloadBtn
                anchors.centerIn: parent
                text: "Download"
                color: theme.accentBlue
                font.pixelSize: 12
                font.weight: Font.DemiBold
            }

            MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                hoverEnabled: true
                onClicked: MOTIONInterface.openDownloadUrl(banner.downloadUrl)
                onContainsMouseChanged: parent.color = containsMouse ? "#E0E0E0" : "#FFFFFF"
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
                onClicked: banner.visible = false
            }
        }
    }

    Connections {
        target: MOTIONInterface
        function onUpdateAvailable(version, url) {
            banner.latestVersion = version
            banner.downloadUrl = url
            banner.visible = true
        }
    }

    // Auto-check on creation (after a brief delay to let the app settle)
    Timer {
        interval: 3000
        running: true
        repeat: false
        onTriggered: MOTIONInterface.checkForUpdates()
    }
}
