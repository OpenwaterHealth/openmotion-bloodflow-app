import QtQuick 6.0

/*  LaserDot — the yellow laser-output circle in the sensor-module
 *  diagrams, crossed with a light X: the optics ⊗ convention for a
 *  beam pointing into the screen (out of the sensor face, into the
 *  patient). Shared by SensorView and ContactQualityModal so the
 *  diagram language stays identical everywhere (#445 follow-up).
 */
Rectangle {
    property int size: 15

    width: size
    height: size
    radius: size / 2
    color: "#FFD700"
    border.color: "black"
    border.width: 1

    // The tiny radius keeps antialiasing on for the rotated strokes.
    Rectangle {
        anchors.centerIn: parent
        width: parent.width - 4; height: 1.5; radius: 0.75
        rotation: 45
        color: "#50000000"
    }
    Rectangle {
        anchors.centerIn: parent
        width: parent.width - 4; height: 1.5; radius: 0.75
        rotation: -45
        color: "#50000000"
    }
}
