import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0

Rectangle {
    id: windowMenu
    width: parent.width
    height: 60
    color: "#1E1E20" // Header background color
    radius: 20

    // Properties to configure the logo
    property string logoSource: "" // Default to no logo

    // Session bar state (bound from BloodFlow page)
    property string sessionId: ""
    property bool   scanning: false
    property bool   freeRun: false
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

            Image {
                source: windowMenu.logoSource // Use the configurable logo source
                anchors.fill: parent
                fillMode: Image.PreserveAspectFit
                smooth: true
                visible: windowMenu.logoSource !== "" // Show only if a logo is provided
            }
        }

        // Session info bar (replaces old title + version block)
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 34
            color: "#252528"
            radius: 8
            border.color: "#3E4E6F"
            border.width: 1

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 14
                anchors.rightMargin: 14
                spacing: 10

                Text { text: "Session:"; color: "#7F8C8D"; font.pixelSize: 13 }
                Text {
                    text: windowMenu.sessionId || "—"
                    color: "#4A90E2"
                    font.pixelSize: 13
                    font.weight: Font.DemiBold
                }

                Item { Layout.fillWidth: true }

                Text {
                    text: "OpenMotion BloodFlow"
                    color: "#FFFFFF"
                    font.pixelSize: 14
                    font.weight: Font.Bold
                }

                Item { Layout.fillWidth: true }

                Text {
                    text: {
                        if (windowMenu.freeRun) {
                            return windowMenu.scanning
                                ? "Free Run  " + windowMenu.formatSec(windowMenu.elapsedSec)
                                : "Free Run"
                        }
                        return windowMenu.scanning
                            ? windowMenu.formatSec(windowMenu.elapsedSec) + " / " + windowMenu.formatSec(windowMenu.durationSec)
                            : windowMenu.formatSec(windowMenu.durationSec)
                    }
                    color: windowMenu.scanning ? "#2ECC71" : "#7F8C8D"
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
