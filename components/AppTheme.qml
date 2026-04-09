import QtQuick 6.0
import OpenMotion 1.0

/*  AppTheme — lightweight colour-token provider.
 *
 *  Instantiate once per file that needs themed colours:
 *      AppTheme { id: theme }
 *  Then reference: theme.bgBase, theme.textPrimary, etc.
 *
 *  All tokens react to MOTIONInterface.appConfig.darkMode so the
 *  entire UI flips live when the toggle is changed.
 */
QtObject {
    // ── convenience alias ──────────────────────────────────────────
    readonly property bool dark: MOTIONInterface.appConfig.darkMode !== false

    // ── backgrounds (lightest → darkest in dark mode) ─────────────
    readonly property color bgBase:       dark ? "#1C1C1E" : "#D5D5DA"
    readonly property color bgPanel:      dark ? "#1A1A1C" : "#C5C5CB"
    readonly property color bgContainer:  dark ? "#1E1E20" : "#E0E0E4"
    readonly property color bgElevated:   dark ? "#252528" : "#C8C8CE"
    readonly property color bgInput:      dark ? "#2E2E33" : "#C0C0C6"
    readonly property color bgPlot:       dark ? "#141417" : "#F0F0F3"
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
    readonly property color accentBlue:   "#4A90E2"
    readonly property color accentGreen:  "#2ECC71"
    readonly property color accentRed:    "#E74C3C"
    readonly property color accentYellow: "#F1C40F"
    readonly property color accentOrange: "#E67E22"

    // ── status / indicators ───────────────────────────────────────
    readonly property color statusGreen:  "#2ECC71"
    readonly property color statusBlue:   "#3498DB"
    readonly property color statusYellow: "#F1C40F"
    readonly property color statusGrey:   dark ? "#7F8C8D" : "#AEAEB2"

    // ── chart / plot specific ─────────────────────────────────────
    readonly property color plotGrid:     dark ? "#333333" : "#C0C0C5"
    readonly property color plotLabel:    dark ? "#999999" : "#555555"
    readonly property color plotText:     dark ? "#C9D1D9" : "#2A2A2A"
}
