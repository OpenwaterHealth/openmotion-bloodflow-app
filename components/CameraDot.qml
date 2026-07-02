import QtQuick 6.0
import QtQuick.Controls as Controls
import OpenMotion 1.0

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
 *                   and to read engineeringMode)
 *      size       : pixel size of the dot (default 18).
 */
Item {
    id: root

    property string side
    property int    camIndex1
    required property var modal
    property int    size: 18

    width: size
    height: size


    readonly property string status: modal.cameraStatus(side, camIndex1)
    readonly property var    types: status === "bad" && modal.engineeringMode
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
        if (!modal.engineeringMode)  return "#E67E22"
        if (isSplit)                return AppTheme.accentOrangeAmbient  // unused at render time (splitFrame handles it); kept so singleColor is never undefined
        if (hasAmbient)             return AppTheme.accentOrangeAmbient
        if (hasContact)             return AppTheme.accentOrangeContact
        // Unknown / future typeKey — fall back to dark orange and warn.
        console.warn("CameraDot: unknown typeKey(s)", types, "for", side, camIndex1)
        return AppTheme.accentOrangeAmbient
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

    // Split case — horizontal sharp-transition gradient. Qt 6's
    // Rectangle.gradient respects radius, so the fill is naturally
    // clipped to the circular shape. (Two clipped child rects don't
    // work — Rectangle's clip:true uses rectangular bounds, not the
    // rounded outline.)
    Rectangle {
        id: splitFrame
        anchors.fill: parent
        visible: root.isSplit
        radius: parent.width / 2
        border.color: "black"
        border.width: 1
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0.0;    color: AppTheme.accentOrangeAmbient }
            GradientStop { position: 0.4999; color: AppTheme.accentOrangeAmbient }
            GradientStop { position: 0.5001; color: AppTheme.accentOrangeContact }
            GradientStop { position: 1.0;    color: AppTheme.accentOrangeContact }
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
