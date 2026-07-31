import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

Item {
    id: iconWindowButton
    width: 40
    height: 40


    // IconWindowButton properties
    property string buttonIcon: "\ue900"         // Icon Unicode
    // The keenicons glyphs draw their own rounded frame, which fights every
    // square-cornered theme \u2014 a rounded box inside a Win95 title bar reads as
    // a rendering bug. Square themes fall back to a plain typographic glyph
    // and let the themed surface supply the frame instead.
    property string squareGlyph: ""
    property color iconColor: AppTheme.textSecondary         // Default icon color
    property color hoverBackground: AppTheme.bgHover   // Background color on hover
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

    // True when the active theme wants a drawn button frame rather than the
    // icon font's own rounded box.
    readonly property bool framed: (AppTheme.bevel || AppTheme.squareCorners)
                                   && squareGlyph !== ""

    // Background
    ThemedSurface {
        id: background
        anchors.fill: parent
        // A framed button needs a visible face to chisel; the default themes
        // keep the original transparent-until-hovered behaviour.
        color: mouseArea.pressed ? activeBackground
                                 : (mouseArea.containsMouse ? hoverBackground
                                                            : (framed ? AppTheme.bgPanel : backgroundColor))
        radius: AppTheme.r(6)
        // Win95 buttons invert their chisel while held down.
        raised: !mouseArea.pressed
        glossy: false
    }

    // Icon
    Text {
        id: icon
        text: framed ? squareGlyph : buttonIcon
        font.family: framed ? "Helvetica" : iconFont.name
        font.pixelSize: framed ? 15 : 24
        font.bold: framed
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
