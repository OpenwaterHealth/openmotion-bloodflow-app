import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

// Startup warning when a unit is waiting in firmware update mode (USB DFU,
// #737). A bare-metal console or sensor only sits there at power-up if a
// firmware write did not finish, and a power cycle would start the
// half-written image and brick it. Presentation only: the connector's
// startup probe decides (research builds only, never clinical) and raises
// firmwareResumeOffered. With a record of the update it offers to finish it
// (retryFirmwareUpdate); without one it can only warn.
Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 9998

    // Modal interface — see HistoryModal.qml for rationale.
    readonly property string label: "Firmware Update"

    // Keep clear of the icon bar, as SampleScanOfferModal does.
    readonly property int iconBarInset: 104

    property string deviceKey: ""
    property string message: ""

    // The operator chose to finish the update; the host shows the Settings
    // firmware card, where the progress is.
    signal finishRequested()

    function openFor(dev, msg) {
        root.deviceKey = dev
        root.message = msg
        root.visible = true
        card.forceActiveFocus()
    }
    function open() { root.visible = true; card.forceActiveFocus() }
    function close() { root.visible = false }

    Rectangle {
        anchors.fill: parent
        color: "#000000AA"
        // Capture ALL pointer input so nothing falls through to the plot
        // viewer behind the modal (issue #214).
        MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            onClicked: root.close()
            onWheel: function(wheel) { wheel.accepted = true }
        }
    }

    Rectangle {
        id: card
        width: Math.min(parent.width - root.iconBarInset - 40, 480)
        height: contentCol.implicitHeight + 48
        radius: 12
        color: AppTheme.sheetBg
        border.color: AppTheme.accentRed
        border.width: 2
        anchors.verticalCenter: parent.verticalCenter
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.horizontalCenterOffset: root.iconBarInset / 2

        MouseArea { anchors.fill: parent }

        ColumnLayout {
            id: contentCol
            anchors.fill: parent
            anchors.margins: 24
            spacing: 16

            Text {
                text: root.deviceKey !== "" ? "Firmware update did not finish"
                                            : "Unit waiting in update mode"
                color: AppTheme.accentRed
                font.pixelSize: 18
                font.weight: Font.DemiBold
            }

            Text {
                text: root.message
                color: AppTheme.textSecondary
                font.pixelSize: 13
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 12
                Item { Layout.fillWidth: true }

                Button {
                    text: root.deviceKey !== "" ? "Not now" : "OK"
                    Layout.preferredHeight: 32
                    onClicked: root.close()
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13
                        color: AppTheme.textSecondary
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? AppTheme.bgHover : AppTheme.bgInput
                        radius: 4
                        border.color: AppTheme.borderSoft; border.width: 1
                    }
                }

                Button {
                    visible: root.deviceKey !== ""
                    text: "Finish update"
                    Layout.preferredHeight: 32
                    onClicked: {
                        var dev = root.deviceKey
                        root.close()
                        if (MotionInterface.retryFirmwareUpdate(dev))
                            root.finishRequested()
                    }
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13
                        color: "#FFFFFF"
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? Qt.lighter(AppTheme.accentInteractive, 1.1) : AppTheme.accentInteractive
                        radius: 4
                    }
                }
            }
        }

        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape) { root.close(); event.accepted = true }
        }
    }
}
