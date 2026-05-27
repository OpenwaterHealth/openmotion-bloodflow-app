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
    property string metric: "bvi"
    property int windowSeconds: 15
    property bool autoScale: true

    // ── Outputs ────────────────────────────────────────────────────────
    signal metricRequested(string m)
    signal windowSecondsRequested(int s)
    signal autoScaleToggled(bool enabled)

    readonly property var _metricOptions: [
        { value: "bfi",      label: "BFI" },
        { value: "bvi",      label: "BVI" },
        { value: "mean",     label: "Mean" },
        { value: "contrast", label: "Contrast" }
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
            text: "Metric:"
            color: theme.textTertiary
            font.pixelSize: 11
        }
        ComboBox {
            id: metricCombo
            Layout.preferredWidth: 110
            model: toolbar._metricOptions
            textRole: "label"
            valueRole: "value"
            currentIndex: {
                for (var i = 0; i < toolbar._metricOptions.length; i++) {
                    if (toolbar._metricOptions[i].value === toolbar.metric) return i
                }
                return 1  // default BVI
            }
            onActivated: toolbar.metricRequested(toolbar._metricOptions[currentIndex].value)
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
