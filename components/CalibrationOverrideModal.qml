import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

// Issue #426 — the calibration pre-write gate. Raised between the
// calibration scan and the console EEPROM write, when the scan's own
// means/contrast miss threshold. Some units simply ship with a dim laser;
// their calibration is still usable, and permanently lowering
// ft_min_mean_per_camera would drop the bar for every system rather than
// this one run.
//
// Nothing has been written to the console when this appears, and nothing
// will be unless the operator approves — the SDK worker is blocked on the
// answer. Declining leaves the existing calibration exactly as it was.
// Approving resumes the normal sequence: write, validation scan, verdict.
//
// The decision is deliberately made against the measured numbers rather
// than a bare yes/no prompt, so the operator can see how far below the bar
// each camera actually is.
//
// Nested inside SettingsModal like PasswordPromptModal — the Calibrate
// flow lives there, and SettingsModal is itself the ModalManager entry.
Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 10000

    // Same clearance the other modals reserve so the card never slides
    // under the left icon bar on a narrow window.
    readonly property int iconBarInset: 104

    function open() { root.visible = true }

    // The connector clears the pending override when a new run starts or
    // when accept/discard lands. Follow that so a stale prompt can never
    // sit on top of the Calibrate row blocking the next run — the HIL
    // suites loop calibrations back-to-back and would deadlock on it.
    Connections {
        target: MotionInterface
        function onCalibrationStateChanged() {
            if (!MotionInterface.calibrationOverridePending)
                root.visible = false
        }
    }

    function _accept() {
        MotionInterface.acceptCalibrationOverride()
        root.visible = false
    }
    function _discard() {
        MotionInterface.dismissCalibrationOverride()
        root.visible = false
    }

    readonly property var rows: MotionInterface.calibrationOverrideRows

    // Backdrop. No click-outside dismissal: this prompt decides what ends
    // up on the console, so it must be answered explicitly. Pointer input
    // is still absorbed so scroll can't reach the plot viewer (#214).
    Rectangle {
        anchors.fill: parent
        color: "#000000B0"
        MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            onWheel: function(wheel) { wheel.accepted = true }
        }
    }

    Rectangle {
        width: Math.min(parent.width - root.iconBarInset - 40, 620)
        height: Math.min(contentCol.implicitHeight + 48, parent.height - 48)
        radius: 14
        color: AppTheme.sheetBg
        border.color: AppTheme.borderStrong
        border.width: 1
        // Center within [iconBarInset, parent.width] so the card clears the
        // icon bar instead of centering across the whole window.
        anchors.verticalCenter: parent.verticalCenter
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.horizontalCenterOffset: root.iconBarInset / 2

        MouseArea { anchors.fill: parent }

        ColumnLayout {
            id: contentCol
            anchors.fill: parent
            anchors.margins: 24
            spacing: 14

            Text {
                text: "Calibration Scan Below Threshold"
                color: AppTheme.textPrimary
                font.pixelSize: 18
                font.weight: Font.DemiBold
            }

            Text {
                text: "Nothing has been written to the console yet. The "
                    + "calibration scan's image means or contrast are below "
                    + "threshold on the cameras marked below. If this "
                    + "unit's laser is simply dim, the calibration is still "
                    + "usable — approving overwrites the console "
                    + "calibration and then runs the validation scan."
                color: AppTheme.textSecondary
                font.pixelSize: 13
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            // Header
            RowLayout {
                Layout.fillWidth: true
                spacing: 0
                Text {
                    text: "Cam"; color: AppTheme.textSecondary
                    font.pixelSize: 11; font.weight: Font.DemiBold
                    Layout.preferredWidth: 46
                }
                Repeater {
                    model: ["Mean", "Contrast", "BFI", "BVI"]
                    Text {
                        text: modelData
                        color: AppTheme.textSecondary
                        font.pixelSize: 11
                        font.weight: Font.DemiBold
                        horizontalAlignment: Text.AlignHCenter
                        // preferredWidth 0 so the four columns split the
                        // row evenly. With fillWidth alone the leftover
                        // space is distributed *on top of* each label's
                        // implicit width, so "Contrast" would claim a wider
                        // column than "BFI" and the header would sit out of
                        // line with the data rows below.
                        Layout.preferredWidth: 0
                        Layout.fillWidth: true
                    }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                height: 1
                color: AppTheme.borderSoft
            }

            // Per-camera measured value over its accept band. Failing cells
            // are red; the limit line underneath says what was required.
            // Height is derived from the row count, NOT from the panel —
            // sizing it off root.height while the panel sizes itself off
            // contentCol.implicitHeight would be a binding loop. Taller
            // sets (16 cameras) scroll.
            ListView {
                id: rowList
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(root.rows.length * 38, 304)
                clip: true
                model: root.rows
                boundsBehavior: Flickable.StopAtBounds

                delegate: RowLayout {
                    id: rowDelegate
                    // Aliased so the inner Repeater's own modelData (the
                    // per-metric cell) doesn't shadow the row.
                    required property var modelData
                    readonly property var row: modelData

                    width: rowList.width
                    height: 38
                    spacing: 0

                    Text {
                        text: rowDelegate.row.side + rowDelegate.row.cam
                        color: AppTheme.textPrimary
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                        Layout.preferredWidth: 46
                    }

                    Repeater {
                        model: [
                            { v: rowDelegate.row.mean,     l: rowDelegate.row.meanLimit,     f: rowDelegate.row.meanFail },
                            { v: rowDelegate.row.contrast, l: rowDelegate.row.contrastLimit, f: rowDelegate.row.contrastFail },
                            { v: rowDelegate.row.bfi,      l: rowDelegate.row.bfiLimit,      f: rowDelegate.row.bfiFail },
                            { v: rowDelegate.row.bvi,      l: rowDelegate.row.bviLimit,      f: rowDelegate.row.bviFail }
                        ]
                        ColumnLayout {
                            id: cell
                            required property var modelData
                            // Matches the header's even split — see there.
                            Layout.preferredWidth: 0
                            Layout.fillWidth: true
                            spacing: 0
                            Text {
                                text: cell.modelData.v
                                color: cell.modelData.f ? AppTheme.accentRed
                                                        : AppTheme.textPrimary
                                font.pixelSize: 12
                                font.weight: cell.modelData.f ? Font.DemiBold
                                                              : Font.Normal
                                horizontalAlignment: Text.AlignHCenter
                                Layout.fillWidth: true
                            }
                            Text {
                                text: cell.modelData.l
                                color: AppTheme.textSecondary
                                font.pixelSize: 10
                                horizontalAlignment: Text.AlignHCenter
                                Layout.fillWidth: true
                            }
                        }
                    }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                height: 1
                color: AppTheme.borderSoft
            }

            Text {
                text: "Approving is recorded in the audit log. The run will "
                    + "still report its honest verdict — expect the image "
                    + "means to fail again in validation — and the status "
                    + "will read \"Accepted (Below Threshold)\" rather than "
                    + "Passed."
                color: AppTheme.textSecondary
                font.pixelSize: 11
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 12
                Item { Layout.fillWidth: true }

                // Default action — leaves the console untouched.
                Button {
                    text: "Don't Overwrite"
                    Layout.preferredHeight: 32
                    onClicked: root._discard()
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
                    text: "Overwrite Anyway"
                    Layout.preferredHeight: 32
                    onClicked: root._accept()
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13
                        color: "#FFFFFF"
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? Qt.lighter(AppTheme.accentOrange, 1.1)
                                              : AppTheme.accentOrange
                        radius: 4
                    }
                }
            }
        }

        // Escape discards — the safe default, same as the Discard button.
        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape) {
                root._discard(); event.accepted = true
            }
        }
    }
}
