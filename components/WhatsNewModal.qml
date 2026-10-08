import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

// "What's new" after a new release is installed (#597). Presentation only:
// the connector decides what is pending (pendingWhatsNew) and records the
// version when this closes (markWhatsNewSeen), so dismissing it any way —
// button, Escape or backdrop click — counts as seen. Also opened from
// Settings → Application → "What's new" with the current release's notes.
Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 9998

    // Modal interface — see HistoryModal.qml for rationale.
    readonly property string label: "What's New"

    // Left-edge space to keep clear of the icon bar. See HistoryModal.qml.
    readonly property int iconBarInset: 104

    property string notes: ""

    function openWith(markdown) {
        if (!markdown) return
        root.notes = markdown
        scroller.ScrollBar.vertical.position = 0
        root.visible = true
        card.forceActiveFocus()
    }
    function open() { openWith(MotionInterface.currentWhatsNew()) }
    function close() {
        if (!root.visible) return
        root.visible = false
        MotionInterface.markWhatsNewSeen()
    }

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
        width: Math.min(parent.width - root.iconBarInset - 40, 560)
        height: Math.min(parent.height - 80,
                         header.implicitHeight + notesText.implicitHeight + footer.implicitHeight + 96)
        radius: 12
        color: AppTheme.sheetBg
        border.color: AppTheme.borderSubtle
        border.width: 2
        anchors.verticalCenter: parent.verticalCenter
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.horizontalCenterOffset: root.iconBarInset / 2

        // Absorb empty-space clicks so they don't reach the backdrop.
        MouseArea { anchors.fill: parent }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 24
            spacing: 16

            Text {
                id: header
                text: "What's new in Open-Motion"
                color: AppTheme.textPrimary
                font.pixelSize: 18
                font.weight: Font.DemiBold
            }

            ScrollView {
                id: scroller
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                contentWidth: availableWidth

                Text {
                    id: notesText
                    width: scroller.availableWidth
                    text: root.notes
                    textFormat: Text.MarkdownText
                    wrapMode: Text.WordWrap
                    color: AppTheme.textSecondary
                    linkColor: AppTheme.accentInteractive
                    font.pixelSize: 13
                    onLinkActivated: function(link) { Qt.openUrlExternally(link) }
                }
            }

            RowLayout {
                id: footer
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }

                Button {
                    text: "Got it"
                    Layout.preferredHeight: 32
                    Layout.preferredWidth: 96
                    onClicked: root.close()
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
