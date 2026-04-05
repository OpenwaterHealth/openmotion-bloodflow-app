import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0

Item {
    id: iconWindowButton
    width: 40
    height: 40

    // IconWindowButton properties
    property string buttonIcon: "\ue900"         // Icon Unicode
    property color iconColor: "#BDC3C7"         // Default icon color
    property color hoverBackground: "#3C3C3C"   // Background color on hover
    property color hoverIconColor: "white"      // Icon color on hover
    property color backgroundColor: "transparent" // Default background color
    property color activeBackground: "#374774"      // Background color when clicked
    property color activeIconColor: "white"     // Icon color when clicked

    // Signal for click handling
    signal clicked()

    FontLoader {
        id: iconFont
        source: "../assets/fonts/keenicons-outline.ttf"
    }

    // Background
    Rectangle {
        id: background
        width: parent.width
        height: parent.height
        color: mouseArea.pressed ? activeBackground : (mouseArea.containsMouse ? hoverBackground : backgroundColor)
        radius: 6
        border.color: "transparent"
    }

    // Icon
    Text {
        id: icon
        text: buttonIcon
        font.family: iconFont.name
        font.pixelSize: 24 // Icon size
        color: mouseArea.pressed ? activeIconColor : (mouseArea.containsMouse ? hoverIconColor : iconColor)
        anchors.centerIn: parent
    }

    // Mouse Area for hover and click
    MouseArea {
        id: mouseArea
        anchors.fill: parent
        hoverEnabled: true

        onClicked: {
            iconWindowButton.clicked() // Emit the clicked signal
        }
    }
}
