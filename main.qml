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

    Rectangle {
        anchors.fill: parent
        color: "#1C1C1E"
        radius: 20
        border.color: "transparent"

        // Header Section (with drag functionality)
        WindowMenu {
            id: headerMenu
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: parent.right

            titleText: "OpenMotion BloodFlow"
            logoSource: "../assets/images/OpenwaterLogo.png"
            appVerText: "" + appVersion
            sdkVerText: "" + MOTIONInterface.get_sdk_version()
        }

        // Main Content: New BloodFlow page (no sidebar)
        Item {
            anchors.fill: parent
            anchors.topMargin: 65
            anchors.rightMargin: 8
            anchors.bottomMargin: 8
            anchors.leftMargin: 8

            BloodFlowNew {
                anchors.fill: parent
            }
        }
    }

    Connections {
        target: MOTIONInterface
    }
}
