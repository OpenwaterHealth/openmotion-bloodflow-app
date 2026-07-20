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
 *
 *  Light mode is a warm "paper" palette (cream surfaces, warm-gray
 *  ink, terracotta interactive accent) modeled on the Claude desktop
 *  light theme. When tuning, keep each group's relative ordering —
 *  e.g. light-mode borders run darkest→lightest hover < subtle <
 *  strong < soft ("subtle"/"strong" are named for their dark-mode
 *  roles).
 */
QtObject {
    // ── convenience alias ──────────────────────────────────────────
    readonly property bool dark: MotionInterface.appConfig.darkMode !== false

    // ── backgrounds (lightest → darkest in dark mode) ─────────────
    readonly property color bgBase:       dark ? "#1C1C1E" : "#F0EEE6"
    readonly property color bgPanel:      dark ? "#1A1A1C" : "#E8E4D8"
    readonly property color bgContainer:  dark ? "#1E1E20" : "#F7F5EE"
    readonly property color bgElevated:   dark ? "#252528" : "#EBE7DB"
    readonly property color bgInput:      dark ? "#2E2E33" : "#FDFCF8"
    readonly property color bgPlot:       dark ? "#141417" : "#F5F3EB"
    readonly property color bgHover:      dark ? "#2E2E33" : "#E3DFD1"
    readonly property color bgCard:       dark ? "#262630" : "#FAF9F4"
    readonly property color bgCardAlt:    dark ? "#232329" : "#F1EEE5"

    // ── borders ───────────────────────────────────────────────────
    readonly property color borderStrong: dark ? "#2A2A2E" : "#CFC9BB"
    readonly property color borderSubtle: dark ? "#3E4E6F" : "#C6C0B0"
    readonly property color borderHover:  dark ? "#5A6B8C" : "#8F8877"
    readonly property color borderSoft:   dark ? "#333340" : "#D8D3C5"

    // ── text ──────────────────────────────────────────────────────
    readonly property color textPrimary:   dark ? "#FFFFFF" : "#1F1E1B"
    readonly property color textSecondary: dark ? "#BDC3C7" : "#57544A"
    readonly property color textTertiary:  dark ? "#7F8C8D" : "#8A867A"
    readonly property color textDisabled:  dark ? "#555555" : "#B6B1A4"
    readonly property color textLink:      dark ? "#4A90E2" : "#2060C0"

    // ── interactive accent ────────────────────────────────────────
    // Hover fills, focus rings, toggle-on, selected rows, CTA chips.
    // Terracotta on the light paper palette; unchanged blue in dark
    // mode. Semantic blues (info toasts, active-camera dots, status
    // chips) stay on accentBlue in both modes — do not swap those.
    readonly property color accentInteractive: dark ? "#4A90E2" : "#D97757"

    // ── semantic accent colours (same in both modes) ──────────────
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
    readonly property color statusGrey:   dark ? "#7F8C8D" : "#A8A399"

    // ── chart / plot specific ─────────────────────────────────────
    readonly property color plotGrid:     dark ? "#333333" : "#CFC9BA"
    readonly property color plotLabel:    dark ? "#999999" : "#6B675C"
    readonly property color plotText:     dark ? "#C9D1D9" : "#33312B"
    // Plot cell / scrubber track surface. In dark mode this matches the
    // old bgPanel value (no visual change); in light mode it is white so
    // cells read as raised paper on the recessed bgPlot surface instead
    // of the muddier panel gray.
    readonly property color plotCellBg:   dark ? "#1A1A1C" : "#FFFFFF"

    // ── floating overlays (pills, popups, tooltips over the plot) ──
    // Translucent in both modes; dark values match the previous
    // hard-coded Qt.rgba constants so dark mode is unchanged. Light
    // values are warm-tinted whites so overlays sit on the paper
    // palette without going clinical-white.
    readonly property color overlayBg:      dark ? Qt.rgba(0.10, 0.10, 0.12, 0.82)
                                                 : Qt.rgba(0.99, 0.98, 0.95, 0.90)
    readonly property color overlayBgSolid: dark ? Qt.rgba(0.12, 0.12, 0.14, 0.96)
                                                 : Qt.rgba(0.99, 0.985, 0.96, 0.97)

    // Guard a user-configured accent (e.g. trace colors) for legibility
    // against the current plot background: colors too light to read in
    // light mode (or too dark in dark mode) are replaced with a neutral
    // ink; everything else passes through untouched.
    function readableInk(c) {
        var col = (typeof c === "string") ? Qt.color(c) : c
        var lum = 0.299 * col.r + 0.587 * col.g + 0.114 * col.b
        if (!dark && lum > 0.62) return Qt.color("#2B2925")
        if (dark && lum < 0.16) return Qt.color("#E8E8EC")
        return col
    }
}
