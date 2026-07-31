import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

// Offer to open the bundled sample scan when the startup connection
// watchdog finds no device (#314 follow-up). Presentation only — the
// connector owns every gate: it never emits sampleScanOfferRequested in a
// clinical build, with any device connected, or with a source already
// bound. Nothing auto-loads in either mode, and declining is final for
// the launch (relaunch to be offered again).
Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 9998

    // Modal interface — see HistoryModal.qml for rationale.
    readonly property string label: "Sample Dataset"

    // Left-edge space to keep clear of the icon bar (ButtonPanel is 80px
    // + 8px margin at z:10000, ABOVE this modal's z:9998), so the card
    // never slides under it on narrow windows. See HistoryModal.qml.
    readonly property int iconBarInset: 104

    function open() {
        root.visible = true
        card.forceActiveFocus()
    }
    function close() { root.visible = false }

    // Dimmed backdrop
    Rectangle {
        anchors.fill: parent
        color: "#000000AA"
        // Capture ALL pointer input so scroll/hover can't fall through to
        // the interactive plot viewer behind the modal (issue #214).
        MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            onClicked: root.close()
            onWheel: function(wheel) { wheel.accepted = true }
        }
    }

    Rectangle {
        id: card
        width: Math.min(parent.width - root.iconBarInset - 40, 460)
        height: contentCol.implicitHeight + 48
        radius: AppTheme.r(12)
        color: AppTheme.sheetBg
        border.color: AppTheme.borderSubtle
        border.width: 2
        // Center within [iconBarInset, parent.width] so the card clears the
        // icon bar instead of bleeding under it.
        anchors.verticalCenter: parent.verticalCenter
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.horizontalCenterOffset: root.iconBarInset / 2

        // Absorb empty-space clicks so they don't reach the backdrop.
        MouseArea { anchors.fill: parent }

        ColumnLayout {
            id: contentCol
            anchors.fill: parent
            anchors.margins: 24
            spacing: 16

            Text {
                text: "No console detected"
                color: AppTheme.textPrimary
                font.pixelSize: 18
                font.weight: Font.DemiBold
            }

            Text {
                text: "Would you like to open a sample dataset? You can " +
                      "explore real BFI/BVI traces without any hardware " +
                      "connected."
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
                    text: "Not now"
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
                        radius: AppTheme.r(4)
                        border.color: AppTheme.borderSoft; border.width: 1
                    }
                }

                Button {
                    text: "Open sample dataset"
                    Layout.preferredHeight: 32
                    onClicked: {
                        MotionInterface.loadSampleScan()
                        root.close()
                    }
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13
                        color: "#FFFFFF"
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? Qt.lighter(AppTheme.accentInteractive, 1.1) : AppTheme.accentInteractive
                        radius: AppTheme.r(4)
                    }
                }
            }
        }

        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape) { root.close(); event.accepted = true }
        }
    }
}
