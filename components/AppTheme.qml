pragma Singleton
import QtQuick 6.0
import OpenMotion 1.0

/*  AppTheme — colour-token provider, registered as a QML SINGLETON
 *  (main.py: qmlRegisterSingletonType into the OpenMotion module).
 *
 *  Do NOT instantiate — `import OpenMotion 1.0` and reference the
 *  tokens directly: AppTheme.bgBase, AppTheme.textPrimary, etc.
 *  One instance serves the whole UI; the old per-file
 *  `AppTheme { id: theme }` pattern re-evaluated every token binding
 *  once per instantiating file on each darkMode flip.
 *
 *  All tokens react to MotionInterface.appConfig.darkMode so the
 *  entire UI flips live when the toggle is changed.
 */
QtObject {
    // ── convenience alias ──────────────────────────────────────────
    readonly property bool dark: MotionInterface.appConfig.darkMode !== false

    // ── backgrounds (lightest → darkest in dark mode) ─────────────
    readonly property color bgBase:       dark ? "#1C1C1E" : "#D5D5DA"
    readonly property color bgPanel:      dark ? "#1A1A1C" : "#C5C5CB"
    readonly property color bgContainer:  dark ? "#1E1E20" : "#E0E0E4"
    readonly property color bgElevated:   dark ? "#252528" : "#C8C8CE"
    readonly property color bgInput:      dark ? "#2E2E33" : "#C0C0C6"
    readonly property color bgPlot:       dark ? "#141417" : "#E7E7EC"
    readonly property color bgHover:      dark ? "#2E2E33" : "#BABAC0"
    readonly property color bgCard:       dark ? "#262630" : "#E4E4E8"
    readonly property color bgCardAlt:    dark ? "#232329" : "#DADADE"

    // ── borders ───────────────────────────────────────────────────
    readonly property color borderStrong: dark ? "#2A2A2E" : "#AAAAB0"
    readonly property color borderSubtle: dark ? "#3E4E6F" : "#9AA2B2"
    readonly property color borderHover:  dark ? "#5A6B8C" : "#687890"
    readonly property color borderSoft:   dark ? "#333340" : "#B8B8C0"

    // ── text ──────────────────────────────────────────────────────
    readonly property color textPrimary:   dark ? "#FFFFFF" : "#1C1C1E"
    readonly property color textSecondary: dark ? "#BDC3C7" : "#48484A"
    readonly property color textTertiary:  dark ? "#7F8C8D" : "#8E8E93"
    readonly property color textDisabled:  dark ? "#555555" : "#AEAEB2"
    readonly property color textLink:      dark ? "#4A90E2" : "#2060C0"

    // ── accent colours (same in both modes) ───────────────────────
    readonly property color accentBlue:          "#4A90E2"
    readonly property color accentGreen:         "#2ECC71"
    readonly property color accentRed:           "#E74C3C"
    readonly property color accentYellow:        "#F1C40F"
    readonly property color accentOrange:        "#E67E22"
    readonly property color accentOrangeAmbient: "#9A4012"
    readonly property color accentOrangeContact: "#F4A460"

    // ── status / indicators ───────────────────────────────────────
    readonly property color statusGreen:  "#2ECC71"
    readonly property color statusBlue:   "#3498DB"
    readonly property color statusYellow: "#F1C40F"
    readonly property color statusGrey:   dark ? "#7F8C8D" : "#AEAEB2"

    // ── chart / plot specific ─────────────────────────────────────
    readonly property color plotGrid:     dark ? "#333333" : "#C0C0C5"
    readonly property color plotLabel:    dark ? "#999999" : "#555555"
    readonly property color plotText:     dark ? "#C9D1D9" : "#2A2A2A"
    // Plot cell / scrubber track surface. In dark mode this matches the
    // old bgPanel value (no visual change); in light mode it is white so
    // cells read as raised paper on the recessed bgPlot surface instead
    // of the muddier panel gray.
    readonly property color plotCellBg:   dark ? "#1A1A1C" : "#FFFFFF"

    // ── floating overlays (pills, popups, tooltips over the plot) ──
    // Translucent in both modes; dark values match the previous
    // hard-coded Qt.rgba constants so dark mode is unchanged.
    readonly property color overlayBg:      dark ? Qt.rgba(0.10, 0.10, 0.12, 0.82)
                                                 : Qt.rgba(1.0, 1.0, 1.0, 0.90)
    readonly property color overlayBgSolid: dark ? Qt.rgba(0.12, 0.12, 0.14, 0.96)
                                                 : Qt.rgba(1.0, 1.0, 1.0, 0.97)

    // Guard a user-configured accent (e.g. trace colors) for legibility
    // against the current plot background: colors too light to read in
    // light mode (or too dark in dark mode) are replaced with a neutral
    // ink; everything else passes through untouched.
    function readableInk(c) {
        var col = (typeof c === "string") ? Qt.color(c) : c
        var lum = 0.299 * col.r + 0.587 * col.g + 0.114 * col.b
        if (!dark && lum > 0.62) return Qt.color("#26262B")
        if (dark && lum < 0.16) return Qt.color("#E8E8EC")
        return col
    }
}
