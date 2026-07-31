import QtQuick 6.0
import OpenMotion 1.0

/*  ThemedSurface — a Rectangle that also knows how to be *made of* something.
 *
 *  Colour and corner radius already come from AppTheme, but the themes that
 *  predate flat design are defined as much by their material as their palette:
 *  Windows Classic is chiselled, Aqua and Aero are glossy. Encoding that per
 *  component would mean the same bevel arithmetic in twenty files, so it lives
 *  here and each surface just declares what it is.
 *
 *  Drop-in for a plain Rectangle:
 *
 *      ThemedSurface {
 *          anchors.fill: parent
 *          color: AppTheme.bgCard
 *          radius: AppTheme.r(6)
 *          borderColor: AppTheme.borderSubtle
 *          borderWidth: 1
 *      }
 *
 *  On themes with neither bevel nor gloss (default, glass, Metro, Material)
 *  every extra layer is invisible and this renders exactly as the Rectangle
 *  it replaced — so adopting it can't change the shipped look.
 *
 *  `raised` flips the chisel: true for buttons and panels that stand proud,
 *  false for wells and pressed states (the Win95 "sunken" look).
 */
Item {
    id: surf

    property color color: "transparent"
    property real radius: 0
    property color borderColor: "transparent"
    property int borderWidth: 0
    property bool raised: true
    // Opt out of the sheen on surfaces where it would wash out content —
    // large panels and anything sitting behind live data.
    property bool glossy: true

    // Base fill. The flat border is suppressed under a bevel: the chisel IS
    // the border there, and drawing both gives a muddy double edge.
    Rectangle {
        anchors.fill: parent
        color: surf.color
        radius: surf.radius
        border.color: AppTheme.bevel ? "transparent" : surf.borderColor
        border.width: AppTheme.bevel ? 0 : surf.borderWidth
    }

    // ── Aqua / Aero gloss ──────────────────────────────────────────────
    // A specular highlight over the top half only. The hard stop at 50% is
    // what reads as "lozenge under glass" rather than a generic gradient.
    Rectangle {
        visible: AppTheme.gloss && surf.glossy
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        height: Math.max(1, parent.height * 0.5)
        radius: surf.radius
        gradient: Gradient {
            GradientStop { position: 0.0; color: AppTheme.glossTop }
            GradientStop { position: 1.0; color: AppTheme.glossBottom }
        }
    }

    // ── Windows Classic chisel ─────────────────────────────────────────
    // Two-tone 1px outset: light on the top/left faces, shadow on the
    // bottom/right, as if lit from the upper left. Inverted when raised is
    // false, which is exactly how Win95 drew a pressed button.
    Loader {
        anchors.fill: parent
        active: AppTheme.bevel
        sourceComponent: Item {
            readonly property color hi: surf.raised ? AppTheme.bevelLight : AppTheme.bevelDark
            readonly property color lo: surf.raised ? AppTheme.bevelDark : AppTheme.bevelLight

            Rectangle {   // top
                anchors.left: parent.left; anchors.right: parent.right
                anchors.top: parent.top; height: 1; color: parent.hi
            }
            Rectangle {   // left
                anchors.left: parent.left; anchors.top: parent.top
                anchors.bottom: parent.bottom; width: 1; color: parent.hi
            }
            Rectangle {   // bottom
                anchors.left: parent.left; anchors.right: parent.right
                anchors.bottom: parent.bottom; height: 1; color: parent.lo
            }
            Rectangle {   // right
                anchors.right: parent.right; anchors.top: parent.top
                anchors.bottom: parent.bottom; width: 1; color: parent.lo
            }
        }
    }
}
