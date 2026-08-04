import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 9998


    // Modal interface — see HistoryModal.qml for rationale.
    readonly property string label: "Session Notes"

    // Left-edge space to keep clear of the icon bar (BloodFlow's
    // ButtonPanel — 80px wide + 8px margin, pinned left at z:10000, which
    // is ABOVE this modal's z:9998). The card is centered in the region to
    // the RIGHT of this inset so it never slides under the bar on narrow
    // windows. Matches PlotViewer's left content edge (88 + 16 gutter).
    // See HistoryModal.qml for the original fix.
    readonly property int iconBarInset: 104

    function open() {
        notesArea.text = MotionInterface.scanNotes
        root.visible = true
        notesArea.forceActiveFocus()
    }
    // Open with a timestamped entry pre-inserted. `stamp` is the bracketed
    // contents (e.g. "00:04:32 / 14:32:05"). Existing notes get a newline
    // before the new entry; an empty note gets no leading blank line. Cursor
    // is parked after the timestamp, ready for the operator to type.
    function openWithTimestamp(stamp) {
        var existing = MotionInterface.scanNotes
        var prefix = existing.length > 0 ? existing.replace(/\s+$/, "") + "\n" : ""
        notesArea.text = prefix + "[" + stamp + "] - "
        root.visible = true
        notesArea.forceActiveFocus()
        notesArea.cursorPosition = notesArea.text.length
    }
    function close() {
        MotionInterface.scanNotes = notesArea.text
        MotionInterface.notify("Note saved.", "success", 4000, true)
        root.visible = false
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
        width: Math.min(parent.width - root.iconBarInset - 40, 600)
        height: 450
        radius: 12
        color: AppTheme.sheetBg
        border.color: AppTheme.borderSubtle
        border.width: 2
        // Center within [iconBarInset, parent.width] so the card clears the
        // icon bar instead of bleeding under it. horizontalCenterOffset
        // shifts the full-width center right by half the reserved inset.
        anchors.verticalCenter: parent.verticalCenter
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.horizontalCenterOffset: root.iconBarInset / 2

        // Absorb empty-space clicks inside the modal so they don't
        // propagate to the backdrop and close the modal (issue #106).
        MouseArea { anchors.fill: parent }

        // X close button
        Rectangle {
            width: 28; height: 28; radius: 14
            color: xArea.containsMouse ? "#C0392B" : AppTheme.borderStrong
            border.color: AppTheme.borderHover; border.width: 1
            anchors.top: parent.top; anchors.right: parent.right
            anchors.topMargin: 10; anchors.rightMargin: 10
            z: 10
            Behavior on color { ColorAnimation { duration: 120 } }
            Text { anchors.centerIn: parent; text: "✕"; color: AppTheme.textPrimary; font.pixelSize: 13 }
            MouseArea {
                id: xArea; anchors.fill: parent; hoverEnabled: true
                cursorShape: Qt.PointingHandCursor; onClicked: root.close()
            }
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 24
            spacing: 16

            Text {
                text: root.label
                color: AppTheme.textPrimary
                font.pixelSize: 20
                font.weight: Font.Bold
                Layout.alignment: Qt.AlignHCenter
            }

            Rectangle {
                color: AppTheme.bgInput
                radius: 6
                border.color: AppTheme.borderSubtle
                border.width: 1
                Layout.fillWidth: true
                Layout.fillHeight: true

                ScrollView {
                    anchors.fill: parent
                    anchors.margins: 8

                    TextArea {
                        id: notesArea
                        font.pixelSize: 14
                        color: AppTheme.textPrimary
                        wrapMode: Text.Wrap
                        placeholderText: "Enter notes for this session..."
                        placeholderTextColor: AppTheme.textTertiary
                        background: null
                    }
                }
            }

            // Session notes ride along into scans.db and CSV exports, so
            // remind the operator not to make this a PHI sink. Shown in
            // every build variant — research exports leave the app too.
            Text {
                text: "Do not enter any patient identifiable information in these notes."
                color: AppTheme.textSecondary
                font.pixelSize: 12
                font.italic: true
                wrapMode: Text.Wrap
                horizontalAlignment: Text.AlignHCenter
                Layout.fillWidth: true
            }

        }

        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape) { root.close(); event.accepted = true }
        }
    }
}
