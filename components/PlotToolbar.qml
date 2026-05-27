import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0

// Phase 2b-i — top toolbar for PlotViewer. Stateless: inputs are read from
// parent, outputs are signals the parent applies to its own state. Phase
// 2b-ii adds "Back to live" + Phase 2b-iii adds "Open scan..." buttons.
Item {
    id: toolbar
    implicitHeight: bar.implicitHeight + 8

    AppTheme { id: theme }

    // ── Inputs ─────────────────────────────────────────────────────────
    property var scanSource: null
    // displayMode pairs metrics: "bfi_bvi" overlays BFI + BVI on each
    // cell; "mean_contrast" overlays Mean + Contrast. Matches the
    // legacy plot's showBfiBvi toggle.
    property string displayMode: "bfi_bvi"
    property int windowSeconds: 15
    property bool autoScale: true

    // ── Outputs ────────────────────────────────────────────────────────
    signal displayModeRequested(string mode)
    signal windowSecondsRequested(int s)
    signal autoScaleToggled(bool enabled)

    readonly property var _displayModeOptions: [
        { value: "bfi_bvi",       label: "BFI / BVI" },
        { value: "mean_contrast", label: "Mean / Contrast" }
    ]
    readonly property var _windowOptions: [
        { value: 5,  label: "5 s" },
        { value: 15, label: "15 s" },
        { value: 30, label: "30 s" },
        { value: 60, label: "1 min" },
        { value: 300, label: "5 min" }
    ]

    RowLayout {
        id: bar
        anchors.fill: parent
        anchors.margins: 4
        spacing: 16

        Text {
            text: toolbar.scanSource
                ? "● Live · " + (toolbar.scanSource.live ? "LiveScanSource" : "PastScanSource")
                : "○ No active scan source"
            color: theme.textSecondary
            font.pixelSize: 12
            font.family: "Roboto Mono"
            Layout.preferredWidth: 260
        }

        Text {
            text: "Show:"
            color: theme.textTertiary
            font.pixelSize: 11
        }
        ComboBox {
            id: displayModeCombo
            Layout.preferredWidth: 160
            model: toolbar._displayModeOptions
            textRole: "label"
            valueRole: "value"
            currentIndex: {
                for (var i = 0; i < toolbar._displayModeOptions.length; i++) {
                    if (toolbar._displayModeOptions[i].value === toolbar.displayMode) return i
                }
                return 0  // default BFI/BVI
            }
            onActivated: toolbar.displayModeRequested(toolbar._displayModeOptions[currentIndex].value)
        }

        Text {
            text: "Window:"
            color: theme.textTertiary
            font.pixelSize: 11
        }
        ComboBox {
            id: windowCombo
            Layout.preferredWidth: 90
            model: toolbar._windowOptions
            textRole: "label"
            valueRole: "value"
            currentIndex: {
                for (var i = 0; i < toolbar._windowOptions.length; i++) {
                    if (toolbar._windowOptions[i].value === toolbar.windowSeconds) return i
                }
                return 1  // default 15s
            }
            onActivated: toolbar.windowSecondsRequested(toolbar._windowOptions[currentIndex].value)
        }

        CheckBox {
            id: autoScaleCheck
            text: "Autoscale"
            checked: toolbar.autoScale
            onToggled: toolbar.autoScaleToggled(checked)
        }

        Item { Layout.fillWidth: true }  // pushes everything left
    }
}
