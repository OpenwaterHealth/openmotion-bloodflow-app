.pragma library

/*  themePalettes.js — period-accurate palettes for the named themes.
 *
 *  Pure data, no QML context (hence .pragma library). AppTheme.qml owns the
 *  token *contract*; this file owns the colour values for the themes that
 *  replace the built-in palette wholesale.
 *
 *  "default" and "glass" are deliberately ABSENT: those two stay expressed as
 *  the original inline ternaries in AppTheme.qml so the shipped look is
 *  byte-identical and a theme regression can never silently alter it.
 *
 *  Every theme supplies BOTH a light and a dark variant, because light/dark is
 *  an axis the user drives independently of theme choice. Where a design
 *  language never actually had a dark mode (Windows Classic, Aqua, Aero), the
 *  dark variant is an honest extrapolation: same hues and the same structural
 *  cues (bevel, gloss, glow), re-grounded on dark surfaces.
 *
 *  Geometry keys — read by AppTheme's shape tokens, not colours:
 *    radiusScale  multiplier applied to every `radius:` in the UI
 *    squareCorners  hard 0 radius regardless of scale
 *    bevel   raised 3D chisel (Windows Classic)
 *    gloss   top-half specular sheen (Aqua, Aero)
 *    elevation  drop-shadow depth (Material)
 */

// ── Windows Classic ────────────────────────────────────────────────────
// Win95/98 "3D" chrome: #C0C0C0 face, chiselled bevels, navy selection,
// pure-black text, and not one rounded corner anywhere.
var winClassic = {
    squareCorners: true, radiusScale: 0, bevel: true, gloss: false, elevation: 0,
    light: {
        bgBase: "#008080",          // the famous teal desktop
        bgPanel: "#C0C0C0", bgContainer: "#C0C0C0", bgElevated: "#D4D0C8",
        bgInput: "#FFFFFF", bgPlot: "#C0C0C0", bgHover: "#D4D0C8",
        bgCard: "#C0C0C0", bgCardAlt: "#BCBCBC",
        borderStrong: "#000000", borderSubtle: "#808080",
        borderHover: "#000080", borderSoft: "#DFDFDF",
        textPrimary: "#000000", textSecondary: "#000000",
        textTertiary: "#404040", textDisabled: "#808080", textLink: "#0000EE",
        accentInteractive: "#000080",
        statusGrey: "#808080",
        plotGrid: "#808080", plotLabel: "#000000", plotText: "#000000",
        plotCellBg: "#FFFFFF",
        overlayBg: "#C0C0C0", overlayBgSolid: "#C0C0C0",
        toastBg: "#C0C0C0", menuBg: "#C0C0C0", sheetBg: "#C0C0C0",
        bevelLight: "#FFFFFF", bevelDark: "#404040",
        titleBar: "#000080", titleBarText: "#FFFFFF",
        ambientTop: "#008080", ambientBottom: "#008080",
        ambientBlobA: "#008080", ambientBlobB: "#008080", ambientBlobC: "#008080"
    },
    dark: {
        bgBase: "#004040",
        bgPanel: "#3A3A3A", bgContainer: "#3A3A3A", bgElevated: "#4A4A4A",
        bgInput: "#1E1E1E", bgPlot: "#2E2E2E", bgHover: "#4A4A4A",
        bgCard: "#3A3A3A", bgCardAlt: "#333333",
        borderStrong: "#000000", borderSubtle: "#5A5A5A",
        borderHover: "#5A7BC8", borderSoft: "#6A6A6A",
        textPrimary: "#FFFFFF", textSecondary: "#E0E0E0",
        textTertiary: "#B0B0B0", textDisabled: "#707070", textLink: "#7AA5FF",
        accentInteractive: "#3A5FCD",
        statusGrey: "#909090",
        plotGrid: "#5A5A5A", plotLabel: "#C0C0C0", plotText: "#E8E8E8",
        plotCellBg: "#1E1E1E",
        overlayBg: "#3A3A3A", overlayBgSolid: "#3A3A3A",
        toastBg: "#3A3A3A", menuBg: "#3A3A3A", sheetBg: "#3A3A3A",
        bevelLight: "#6A6A6A", bevelDark: "#101010",
        titleBar: "#1A1A6E", titleBarText: "#FFFFFF",
        ambientTop: "#004040", ambientBottom: "#004040",
        ambientBlobA: "#004040", ambientBlobB: "#004040", ambientBlobC: "#004040"
    }
};

// ── Aqua ───────────────────────────────────────────────────────────────
// Mac OS X 10.0–10.4: lozenge buttons, candy blue, glossy highlights,
// pinstriped near-white surfaces.
var aqua = {
    squareCorners: false, radiusScale: 1.8, bevel: false, gloss: true, elevation: 1,
    light: {
        bgBase: "#ECECEC",
        // bgElevated is deliberately tinted rather than white: the gloss
        // highlight is white, so a white base would make the lozenge
        // invisible. The blue cast is what the sheen sits on.
        bgPanel: "#E4E8ED", bgContainer: "#F2F4F7", bgElevated: "#C8D9EF",
        bgInput: "#FFFFFF", bgPlot: "#F7F8FA", bgHover: "#D6E4F7",
        bgCard: "#FBFCFD", bgCardAlt: "#EFF2F6",
        borderStrong: "#A9B3BF", borderSubtle: "#B9C3CE",
        borderHover: "#3B7CEB", borderSoft: "#D2D9E2",
        textPrimary: "#1A1A1A", textSecondary: "#4A4F57",
        textTertiary: "#7C838C", textDisabled: "#AEB4BC", textLink: "#1E6FD9",
        accentInteractive: "#3B7CEB",
        statusGrey: "#9AA1AA",
        plotGrid: "#CBD3DD", plotLabel: "#5B626B", plotText: "#22262B",
        plotCellBg: "#FFFFFF",
        overlayBg: Qt_rgba(1, 1, 1, 0.88), overlayBgSolid: Qt_rgba(1, 1, 1, 0.96),
        toastBg: "#F4F7FA", menuBg: "#FBFCFD", sheetBg: "#F2F4F7",
        glossTop: Qt_rgba(1, 1, 1, 0.85), glossBottom: Qt_rgba(1, 1, 1, 0.08),
        titleBar: "#DCE2E9", titleBarText: "#1A1A1A",
        ambientTop: "#DCE6F2", ambientBottom: "#EFF3F8",
        ambientBlobA: "#BBD3F0", ambientBlobB: "#D8E4F4", ambientBlobC: "#C9E2EC"
    },
    dark: {
        bgBase: "#22262B",
        bgPanel: "#2A2F35", bgContainer: "#2F343B", bgElevated: "#3A4048",
        bgInput: "#1B1F24", bgPlot: "#1E2227", bgHover: "#3C4A5E",
        bgCard: "#31373E", bgCardAlt: "#2A2F35",
        borderStrong: "#454C55", borderSubtle: "#525A64",
        borderHover: "#5C9CFF", borderSoft: "#3B424A",
        textPrimary: "#F2F4F7", textSecondary: "#C3C9D1",
        textTertiary: "#8C949E", textDisabled: "#606871", textLink: "#6FAFFF",
        accentInteractive: "#4A8CF0",
        statusGrey: "#8C949E",
        plotGrid: "#3E454E", plotLabel: "#9BA3AD", plotText: "#E4E8ED",
        plotCellBg: "#1B1F24",
        overlayBg: Qt_rgba(0.13, 0.15, 0.17, 0.88),
        overlayBgSolid: Qt_rgba(0.13, 0.15, 0.17, 0.97),
        toastBg: "#3A4048", menuBg: "#31373E", sheetBg: "#2F343B",
        glossTop: Qt_rgba(1, 1, 1, 0.20), glossBottom: Qt_rgba(1, 1, 1, 0.02),
        titleBar: "#2A2F35", titleBarText: "#F2F4F7",
        ambientTop: "#1A1E24", ambientBottom: "#23282F",
        ambientBlobA: "#2B4C8C", ambientBlobB: "#33507A", ambientBlobC: "#28545E"
    }
};

// ── Aero ───────────────────────────────────────────────────────────────
// Windows Vista/7: blue-tinted translucent glass, outer glow, soft
// gradients. The one new theme that leans on the existing glass machinery.
var aero = {
    squareCorners: false, radiusScale: 1.1, bevel: false, gloss: true, elevation: 1,
    light: {
        bgBase: "#D6E5F5",
        bgPanel: Qt_rgba(1, 1, 1, 0.55), bgContainer: Qt_rgba(1, 1, 1, 0.68),
        bgElevated: Qt_rgba(1, 1, 1, 0.80), bgInput: Qt_rgba(1, 1, 1, 0.94),
        bgPlot: Qt_rgba(1, 1, 1, 0.45), bgHover: Qt_rgba(0.60, 0.80, 1.0, 0.45),
        bgCard: Qt_rgba(1, 1, 1, 0.72), bgCardAlt: Qt_rgba(1, 1, 1, 0.55),
        borderStrong: Qt_rgba(1, 1, 1, 0.85), borderSubtle: "#8FB8DE",
        borderHover: "#1E90FF", borderSoft: Qt_rgba(1, 1, 1, 0.65),
        textPrimary: "#10243A", textSecondary: "#2E4E6E",
        textTertiary: "#5C7A96", textDisabled: "#9AB0C4", textLink: "#0B60C4",
        accentInteractive: "#1E90FF",
        statusGrey: "#7F97AC",
        plotGrid: "#A9C6E0", plotLabel: "#3C5C7A", plotText: "#12283F",
        plotCellBg: Qt_rgba(1, 1, 1, 0.88),
        overlayBg: Qt_rgba(1, 1, 1, 0.70), overlayBgSolid: Qt_rgba(1, 1, 1, 0.94),
        toastBg: Qt_rgba(1, 1, 1, 0.92), menuBg: Qt_rgba(1, 1, 1, 0.94),
        sheetBg: Qt_rgba(1, 1, 1, 0.93),
        glossTop: Qt_rgba(1, 1, 1, 0.75), glossBottom: Qt_rgba(1, 1, 1, 0.10),
        glowColor: Qt_rgba(0.45, 0.72, 1.0, 0.55),
        titleBar: Qt_rgba(1, 1, 1, 0.60), titleBarText: "#10243A",
        ambientTop: "#BFD9F2", ambientBottom: "#E4F0FB",
        ambientBlobA: "#8FC0EE", ambientBlobB: "#B9D8F5", ambientBlobC: "#9FD3E8"
    },
    dark: {
        bgBase: "#0E1A28",
        bgPanel: Qt_rgba(0.30, 0.50, 0.72, 0.22),
        bgContainer: Qt_rgba(0.32, 0.52, 0.74, 0.28),
        bgElevated: Qt_rgba(0.38, 0.58, 0.80, 0.34),
        bgInput: Qt_rgba(0.05, 0.09, 0.14, 0.88),
        bgPlot: Qt_rgba(0.06, 0.11, 0.17, 0.55),
        bgHover: Qt_rgba(0.40, 0.65, 0.95, 0.38),
        bgCard: Qt_rgba(0.34, 0.54, 0.76, 0.26),
        bgCardAlt: Qt_rgba(0.28, 0.46, 0.68, 0.20),
        borderStrong: Qt_rgba(0.62, 0.82, 1.0, 0.35),
        borderSubtle: Qt_rgba(0.62, 0.82, 1.0, 0.45),
        borderHover: "#4FA8FF", borderSoft: Qt_rgba(0.62, 0.82, 1.0, 0.22),
        textPrimary: "#EAF3FC", textSecondary: "#B8D0E6",
        textTertiary: "#84A2BE", textDisabled: "#5A7086", textLink: "#6FBBFF",
        accentInteractive: "#3FA0FF",
        statusGrey: "#84A2BE",
        plotGrid: "#2C4863", plotLabel: "#8FAFCB", plotText: "#E4F0FB",
        plotCellBg: Qt_rgba(0.05, 0.09, 0.14, 0.85),
        overlayBg: Qt_rgba(0.06, 0.13, 0.21, 0.72),
        overlayBgSolid: Qt_rgba(0.06, 0.13, 0.21, 0.95),
        toastBg: Qt_rgba(0.08, 0.16, 0.25, 0.94),
        menuBg: Qt_rgba(0.08, 0.16, 0.25, 0.95),
        sheetBg: Qt_rgba(0.07, 0.14, 0.22, 0.96),
        glossTop: Qt_rgba(1, 1, 1, 0.22), glossBottom: Qt_rgba(1, 1, 1, 0.03),
        glowColor: Qt_rgba(0.35, 0.65, 1.0, 0.50),
        titleBar: Qt_rgba(0.20, 0.38, 0.58, 0.55), titleBarText: "#EAF3FC",
        ambientTop: "#08131F", ambientBottom: "#12253A",
        ambientBlobA: "#1E4E80", ambientBlobB: "#2A5C92", ambientBlobC: "#1C5F72"
    }
};

// ── Metro ──────────────────────────────────────────────────────────────
// Windows 8 Modern UI: uncompromisingly flat. No radius, no bevel, no
// gradient, no shadow — colour blocks, generous whitespace, one hot accent.
var metro = {
    squareCorners: true, radiusScale: 0, bevel: false, gloss: false, elevation: 0,
    light: {
        bgBase: "#FFFFFF",
        bgPanel: "#F2F2F2", bgContainer: "#FFFFFF", bgElevated: "#E6E6E6",
        bgInput: "#FFFFFF", bgPlot: "#FFFFFF", bgHover: "#0078D7",
        bgCard: "#F2F2F2", bgCardAlt: "#E9E9E9",
        borderStrong: "#CCCCCC", borderSubtle: "#0078D7",
        borderHover: "#0078D7", borderSoft: "#E1E1E1",
        textPrimary: "#000000", textSecondary: "#4C4C4C",
        textTertiary: "#767676", textDisabled: "#B3B3B3", textLink: "#0078D7",
        accentInteractive: "#0078D7",
        statusGrey: "#767676",
        plotGrid: "#E1E1E1", plotLabel: "#767676", plotText: "#000000",
        plotCellBg: "#FFFFFF",
        overlayBg: "#F2F2F2", overlayBgSolid: "#FFFFFF",
        toastBg: "#0078D7", menuBg: "#FFFFFF", sheetBg: "#FFFFFF",
        titleBar: "#0078D7", titleBarText: "#FFFFFF",
        ambientTop: "#FFFFFF", ambientBottom: "#FFFFFF",
        ambientBlobA: "#FFFFFF", ambientBlobB: "#FFFFFF", ambientBlobC: "#FFFFFF"
    },
    dark: {
        bgBase: "#000000",
        bgPanel: "#1F1F1F", bgContainer: "#111111", bgElevated: "#2B2B2B",
        bgInput: "#1F1F1F", bgPlot: "#000000", bgHover: "#0078D7",
        bgCard: "#1F1F1F", bgCardAlt: "#171717",
        borderStrong: "#333333", borderSubtle: "#0078D7",
        borderHover: "#3AA0F0", borderSoft: "#262626",
        textPrimary: "#FFFFFF", textSecondary: "#CCCCCC",
        textTertiary: "#999999", textDisabled: "#5C5C5C", textLink: "#3AA0F0",
        accentInteractive: "#0078D7",
        statusGrey: "#999999",
        plotGrid: "#2B2B2B", plotLabel: "#999999", plotText: "#FFFFFF",
        plotCellBg: "#000000",
        overlayBg: "#1F1F1F", overlayBgSolid: "#111111",
        toastBg: "#0078D7", menuBg: "#1F1F1F", sheetBg: "#111111",
        titleBar: "#0078D7", titleBarText: "#FFFFFF",
        ambientTop: "#000000", ambientBottom: "#000000",
        ambientBlobA: "#000000", ambientBlobB: "#000000", ambientBlobC: "#000000"
    }
};

// ── Material ───────────────────────────────────────────────────────────
// Google Material Design: paper surfaces lifted by elevation shadows,
// purple/teal key colours, 4dp corner radius.
var material = {
    squareCorners: false, radiusScale: 1.0, bevel: false, gloss: false, elevation: 3,
    light: {
        bgBase: "#FAFAFA",
        bgPanel: "#FFFFFF", bgContainer: "#FFFFFF", bgElevated: "#FFFFFF",
        bgInput: "#F5F5F5", bgPlot: "#FAFAFA", bgHover: "#EDE7F6",
        bgCard: "#FFFFFF", bgCardAlt: "#F5F5F5",
        borderStrong: "#E0E0E0", borderSubtle: "#BDBDBD",
        borderHover: "#6200EE", borderSoft: "#EEEEEE",
        textPrimary: "#212121", textSecondary: "#616161",
        textTertiary: "#9E9E9E", textDisabled: "#BDBDBD", textLink: "#6200EE",
        accentInteractive: "#6200EE",
        statusGrey: "#9E9E9E",
        plotGrid: "#E0E0E0", plotLabel: "#757575", plotText: "#212121",
        plotCellBg: "#FFFFFF",
        overlayBg: Qt_rgba(1, 1, 1, 0.92), overlayBgSolid: "#FFFFFF",
        toastBg: "#323232", menuBg: "#FFFFFF", sheetBg: "#FFFFFF",
        elevationShadow: Qt_rgba(0, 0, 0, 0.24),
        titleBar: "#6200EE", titleBarText: "#FFFFFF",
        ambientTop: "#F3E5F5", ambientBottom: "#FAFAFA",
        ambientBlobA: "#D1C4E9", ambientBlobB: "#B2DFDB", ambientBlobC: "#E1BEE7"
    },
    dark: {
        bgBase: "#121212",
        bgPanel: "#1E1E1E", bgContainer: "#1E1E1E", bgElevated: "#2C2C2C",
        bgInput: "#2C2C2C", bgPlot: "#121212", bgHover: "#332940",
        bgCard: "#1E1E1E", bgCardAlt: "#242424",
        borderStrong: "#2F2F2F", borderSubtle: "#3D3D3D",
        borderHover: "#BB86FC", borderSoft: "#292929",
        textPrimary: "#FFFFFF", textSecondary: "#B0B0B0",
        textTertiary: "#8A8A8A", textDisabled: "#5C5C5C", textLink: "#BB86FC",
        accentInteractive: "#BB86FC",
        statusGrey: "#8A8A8A",
        plotGrid: "#2F2F2F", plotLabel: "#9E9E9E", plotText: "#ECECEC",
        plotCellBg: "#1A1A1A",
        overlayBg: Qt_rgba(0.13, 0.13, 0.13, 0.92), overlayBgSolid: "#2C2C2C",
        toastBg: "#2C2C2C", menuBg: "#2C2C2C", sheetBg: "#1E1E1E",
        elevationShadow: Qt_rgba(0, 0, 0, 0.60),
        titleBar: "#1E1E1E", titleBarText: "#FFFFFF",
        ambientTop: "#12121A", ambientBottom: "#121212",
        ambientBlobA: "#3A2E5A", ambientBlobB: "#1E4A46", ambientBlobC: "#43305C"
    }
};

var THEMES = {
    "win-classic": winClassic,
    "aqua": aqua,
    "aero": aero,
    "metro": metro,
    "material": material
};

/* The named themes a user can pick, in menu order. "default" and "glass" are
 * prepended by AppTheme — they live in the singleton, not here. */
function names() {
    return ["win-classic", "aqua", "aero", "metro", "material"];
}

function has(name) {
    return THEMES.hasOwnProperty(name);
}

/* Resolve one theme's colour set. Returns undefined for unknown names so
 * AppTheme falls through to its built-in palette rather than rendering a
 * screenful of undefined colours. */
function lookup(name, isDark) {
    var t = THEMES[name];
    if (!t)
        return undefined;
    return isDark ? t.dark : t.light;
}

/* Geometry/material flags for a theme; undefined for the built-ins. */
function shape(name) {
    var t = THEMES[name];
    if (!t)
        return undefined;
    return {
        squareCorners: t.squareCorners,
        radiusScale: t.radiusScale,
        bevel: t.bevel,
        gloss: t.gloss,
        elevation: t.elevation
    };
}

/* .pragma library denies us the QML `Qt` object, so rgba values are built as
 * plain CSS strings that Qt.color() parses identically. */
function Qt_rgba(r, g, b, a) {
    function h(v) {
        var n = Math.round(Math.max(0, Math.min(1, v)) * 255).toString(16);
        return n.length < 2 ? "0" + n : n;
    }
    return "#" + h(a) + h(r) + h(g) + h(b);   // Qt's #AARRGGBB form
}
