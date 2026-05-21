import QtQuick 6.0
import QtQuick.Controls as Controls

/*  CameraDot — single per-camera contact-quality indicator.
 *
 *  Used in ContactQualityModal sensor diagrams. Encapsulates the
 *  status/type lookup and renders either a solid circular dot or
 *  a vertical-split dot (dark left, light right) for the dev-mode
 *  "ambient + contact both fired" case (#128).
 *
 *  API:
 *      side       : "left" | "right"
 *      camIndex1  : 1..8
 *      modal      : the ContactQualityModal root (required — used to
 *                   call cameraStatus / cameraWarningTypes / cameraTooltip
 *                   and to read developerMode)
 *      size       : pixel size of the dot (default 18). Must be even —
 *                   the split-render geometry assumes an even width so
 *                   the two halves meet exactly at the centerline.
 */
Item {
    id: root

    property string side
    property int    camIndex1
    required property var modal
    property int    size: 18

    width: size
    height: size

    AppTheme { id: theme }

    readonly property string status: modal.cameraStatus(side, camIndex1)
    readonly property var    types: status === "bad" && modal.developerMode
                                    ? modal.cameraWarningTypes(side, camIndex1)
                                    : []
    readonly property bool   hasAmbient: types.indexOf("ambient_light") >= 0
    readonly property bool   hasContact: types.indexOf("poor_contact") >= 0
    readonly property bool   isSplit:    hasAmbient && hasContact

    readonly property color singleColor: {
        if (status === "good")     return "#A3E4A1"
        if (status === "checking") return "#666666"
        if (status === "inactive") return "#666666"
        // status === "bad" past this point
        if (!modal.developerMode)  return "#E67E22"
        if (isSplit)                return theme.accentOrangeAmbient  // unused at render time (splitFrame handles it); kept so singleColor is never undefined
        if (hasAmbient)             return theme.accentOrangeAmbient
        if (hasContact)             return theme.accentOrangeContact
        // Unknown / future typeKey — fall back to dark orange.
        return theme.accentOrangeAmbient
    }

    // Solid case (no split) — single coloured circle.
    Rectangle {
        anchors.fill: parent
        visible: !root.isSplit
        radius: parent.width / 2
        color: root.singleColor
        border.color: "black"
        border.width: 1
    }

    // Split case — two clipped half-rects inset 1px on every edge so
    // they don't overdraw the outer Rectangle's 1px border. The outer
    // Rectangle owns the rounded border and clips its children to the
    // circular shape.
    Rectangle {
        id: splitFrame
        anchors.fill: parent
        visible: root.isSplit
        radius: parent.width / 2
        border.color: "black"
        border.width: 1
        color: "transparent"
        clip: true

        Rectangle {
            x: 1; y: 1
            width: (parent.width - 2) / 2
            height: parent.height - 2
            color: theme.accentOrangeAmbient
        }
        Rectangle {
            x: parent.width / 2
            y: 1
            width: (parent.width - 2) / 2
            height: parent.height - 2
            color: theme.accentOrangeContact
        }
    }

    MouseArea {
        id: hover
        anchors.fill: parent
        hoverEnabled: true
        acceptedButtons: Qt.NoButton
    }
    Controls.ToolTip.visible: hover.containsMouse
    Controls.ToolTip.text: modal.cameraTooltip(side, camIndex1)
}
