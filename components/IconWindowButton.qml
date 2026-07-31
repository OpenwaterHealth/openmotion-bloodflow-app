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

    // Marks the close button so themes that tint it (Vista red, Metro's
    // red-on-hover) can find it without WindowMenu hard-coding any colour.
    property bool isClose: false

    // True when the active theme wants a drawn button frame rather than the
    // icon font's own rounded box. Aero is included: its glyphs sit on a glass
    // pane with its own rim, so the font's built-in box would double it up.
    readonly property bool framed: (AppTheme.bevel || AppTheme.squareCorners
                                    || AppTheme.aeroGlass)
                                   && squareGlyph !== ""

    // Themes that paint the close button at rest (Aero) vs only on hover
    // (Metro). Alpha 0 is how "transparent" reads back, so test that.
    readonly property bool closeTinted: isClose && AppTheme.hasCloseTint
    readonly property bool closeAtRest: closeTinted && AppTheme.closeBg.a > 0
    readonly property bool closeOnHover: closeTinted && AppTheme.closeHoverBg.a > 0

    // Background
    ThemedSurface {
        id: background
        anchors.fill: parent
        // A framed button needs a visible face to chisel; the default themes
        // keep the original transparent-until-hovered behaviour.
        color: {
            if (closeOnHover && mouseArea.containsMouse)
                return AppTheme.closeHoverBg
            if (closeAtRest)
                return mouseArea.pressed ? Qt.darker(AppTheme.closeBg, 1.2)
                                         : (mouseArea.containsMouse ? AppTheme.closeHoverBg
                                                                    : AppTheme.closeBg)
            if (mouseArea.pressed) return activeBackground
            if (mouseArea.containsMouse) return hoverBackground
            return framed ? AppTheme.bgPanel : backgroundColor
        }
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
        color: {
            // A tinted close button owns its glyph colour — white on red,
            // regardless of what the theme's text tokens say.
            if (closeAtRest || (closeOnHover && mouseArea.containsMouse))
                return AppTheme.closeGlyph
            if (mouseArea.pressed) return activeIconColor
            if (mouseArea.containsMouse) return hoverIconColor
            return iconColor
        }
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
