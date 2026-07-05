import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

/*  CooldownBanner — slides in below the header while the thermal cooldown
 *  gate (issue #102) is locking out scan starts. Shows the hottest camera
 *  die temperature against the configured start ceiling, a countdown when
 *  the cooling model (cooldownTauSec) is configured, and an
 *  engineering-only Override (the Python slot re-checks engineeringMode).
 */
Rectangle {
    id: banner
    width: parent.width
    height: visible ? 36 : 0
    clip: true
    visible: MotionInterface.cooldownLockout
    color: "#3498DB"
    Behavior on height { NumberAnimation { duration: 200; easing.type: Easing.OutQuad } }

    readonly property bool _devMode: MotionInterface.appConfig.engineeringMode === true

    // 1 Hz repaint driver so the ETA countdown ticks between poll updates.
    property int _tick: 0
    Timer { running: banner.visible; interval: 1000; repeat: true; onTriggered: banner._tick++ }

    function _label() {
        void banner._tick   // bind to the ticker
        var s
        if (MotionInterface.cooldownReason === "timer") {
            s = "Cameras cooling down after scan"
        } else {
            s = "Cameras cooling — Start available at ≤"
                + MotionInterface.cooldownThresholdC.toFixed(0) + "°C"
            if (MotionInterface.cooldownHottestTempC >= 0)
                s += " (hottest " + MotionInterface.cooldownHottestTempC.toFixed(1) + "°C)"
        }
        var eta = MotionInterface.cooldownEtaSec
        if (eta > 0) {
            var m = Math.floor(eta / 60), sec = eta % 60
            s += " — ready in ~" + m + ":" + (sec < 10 ? "0" : "") + sec
        }
        return s
    }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 16
        anchors.rightMargin: 12
        spacing: 10

        Text { text: "❄"; font.pixelSize: 14; color: "#FFFFFF" }

        Text {
            text: banner.visible ? banner._label() : ""
            color: "#FFFFFF"
            font.pixelSize: 13
            Layout.fillWidth: true
            elide: Text.ElideRight
        }

        Rectangle {
            visible: banner._devMode
            width: overrideLbl.implicitWidth + 20; height: 24; radius: 4; color: "#FFFFFF"
            Text {
                id: overrideLbl
                anchors.centerIn: parent
                text: "Override"
                color: "#3498DB"
                font.pixelSize: 12
                font.weight: Font.DemiBold
            }
            MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                hoverEnabled: true
                onClicked: MotionInterface.cooldownOverride()
                onContainsMouseChanged: parent.color = containsMouse ? "#E0E0E0" : "#FFFFFF"
            }
        }
    }
}
