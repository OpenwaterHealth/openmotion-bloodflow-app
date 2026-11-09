import QtQuick 6.0
import OpenMotion 1.0

// Compact themed checkbox for the History table.
//
// Replaces the old "☐ / ☑" text glyphs, whose size, baseline and weight
// depended on whichever system font supplied them (they rendered as mismatched
// boxes on most machines). Drawn from primitives plus the app's Keenicons font
// so it looks the same everywhere and follows AppTheme in light and dark mode.
//
// `partial` shows the indeterminate dash (the header's select-all box when some
// but not all rows are checked); it only takes effect while `checked` is false.
// With `text` set the box sits at the left with the label beside it, and the
// whole label is clickable; without it the box centres in its cell.
Item {
    id: box
    property bool checked: false
    property bool partial: false
    property string text: ""
    signal toggled()

    implicitWidth: text.length > 0 ? frame.width + 8 + label.implicitWidth : 34
    implicitHeight: 30

    readonly property bool filled: checked || partial

    FontLoader {
        id: iconFont
        source: "../assets/fonts/keenicons-outline.ttf"
    }

    Rectangle {
        id: frame
        anchors.verticalCenter: parent.verticalCenter
        x: box.text.length > 0 ? 0 : (box.width - width) / 2
        width: 16; height: 16; radius: 4
        color: box.filled ? AppTheme.accentInteractive
                          : (area.containsMouse ? AppTheme.bgHover : "transparent")
        border.width: box.filled ? 0 : 1.5
        border.color: area.containsMouse ? AppTheme.accentInteractive : AppTheme.textTertiary
        Behavior on color { ColorAnimation { duration: 120 } }

        Text {
            anchors.centerIn: parent
            visible: box.filled
            text: box.checked ? "\ue99e"    // keenicons "check"
                              : "\uea74"    // keenicons "minus"
            font.family: iconFont.name
            font.pixelSize: 12
            color: "#FFFFFF"
        }
    }

    Text {
        id: label
        visible: box.text.length > 0
        anchors.left: frame.right; anchors.leftMargin: 8
        anchors.verticalCenter: parent.verticalCenter
        text: box.text
        font.pixelSize: 13
        color: AppTheme.textSecondary
    }

    MouseArea {
        id: area
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: function(mouse) { mouse.accepted = true; box.toggled() }
    }
}
