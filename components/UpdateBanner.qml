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
    // Imperative latch: set true when an update is detected, false on
    // dismiss. Visibility is the AND of "has been shown" and "not
    // clinical mode" so a runtime flip of clinicalMode hides the banner
    // even after it was already shown. Issue #96 follow-up.
    property bool shown: false
    visible: shown && !_clinicalMode
    clip: true


    property string latestVersion: ""
    property string downloadUrl: ""
    // True from the moment Update is clicked until the download/install
    // either fails or the app quits for the in-place upgrade.
    property bool updating: false
    property string statusText: "Update"

    color: AppTheme.accentInteractive
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
                text: banner.updating ? banner.statusText : "Update"
                color: AppTheme.accentInteractive
                font.pixelSize: 12
                font.weight: Font.DemiBold
            }

            MouseArea {
                anchors.fill: parent
                enabled: !banner.updating
                cursorShape: banner.updating ? Qt.ArrowCursor : Qt.PointingHandCursor
                hoverEnabled: true
                // Guard against double-clicks spawning racing downloads; the
                // connector also guards re-entry, this just reflects it in UI.
                onClicked: {
                    banner.updating = true
                    banner.statusText = "Starting…"
                    MotionInterface.applyUpdate(banner.downloadUrl)
                }
                onContainsMouseChanged: parent.color =
                    (containsMouse && !banner.updating) ? "#E0E0E0" : "#FFFFFF"
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
                onClicked: banner.shown = false
            }
        }
    }

    // Clinical mode: clinical users shouldn't see update
    // prompts. Skip the auto-check on launch and refuse to show the
    // banner even if MotionInterface.checkForUpdates() is somehow
    // triggered (e.g. via engineering-mode-only Settings row that
    // shouldn't be reachable in clinical mode anyway). Issue #96.
    readonly property bool _clinicalMode: MotionInterface.appConfig.clinicalMode === true

    Connections {
        target: MotionInterface
        function onUpdateAvailable(version, url) {
            if (banner._clinicalMode) return
            banner.latestVersion = version
            banner.downloadUrl = url
            banner.shown = true
        }
        // Withdraw a stale offer: a re-check (e.g. after engineering mode is
        // turned off, dropping beta back to stable) that finds nothing hides
        // the banner. The initial up-to-date launch is a no-op — never shown.
        function onUpdateNotAvailable() {
            banner.shown = false
        }
        // Reflect download/install progress on the button.
        function onUpdateProgress(message) {
            banner.statusText = message
        }
        // Re-enable the button so a failed update can be retried.
        function onUpdateCheckFailed(error) {
            banner.updating = false
            banner.statusText = "Update"
        }
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
