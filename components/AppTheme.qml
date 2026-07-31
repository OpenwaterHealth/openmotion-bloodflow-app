pragma Singleton
import QtQuick 6.0
import OpenMotion 1.0
import "themePalettes.js" as Palettes

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
 *
 *  ── Liquid Glass (MotionInterface.appConfig.liquidGlass) ───────────
 *  A third, orthogonal axis on top of dark/light. When `glass` is on,
 *  the *surface* tokens (panels, cards, containers, elevated, hover,
 *  borders, overlays) become translucent frost so the AmbientBackground
 *  shows through and every panel in the app reads as glass with no
 *  per-component edits. Data-critical surfaces (plot cells, plot bg,
 *  input fields) and text/semantic-accent tokens stay opaque so traces
 *  and numbers never lose contrast. Each glassy token therefore carries
 *  a 4-way value: glass×{dark,light} on top of the original
 *  opaque×{dark,light}. Keep the opaque branch byte-identical to the
 *  pre-glass values so turning the toggle off is a perfect revert.
 */
QtObject {
    // ── mode axes ──────────────────────────────────────────────────
    // Three independent axes now: which theme, light-vs-dark, and the
    // legacy glass flag. `dark` drives every theme — a user picking Metro
    // still gets to choose light or dark — so each named palette in
    // themePalettes.js supplies both variants.
    readonly property bool dark:  MotionInterface.appConfig.darkMode !== false
    readonly property string themeName: MotionInterface.appConfig.themeName || "default"

    // Glass stays a *theme* now ("glass"), but the old boolean is still
    // honoured so a config written by an older build keeps its appearance
    // without a migration step.
    readonly property bool glass: themeName === "glass"
                                  || (themeName === "default"
                                      && MotionInterface.appConfig.liquidGlass === true)

    // Resolved palette for the named themes; undefined for default/glass,
    // which keep their original inline expressions untouched below.
    readonly property var _p: Palettes.lookup(themeName, dark)
    readonly property var _s: Palettes.shape(themeName)

    // ── geometry / material ───────────────────────────────────────
    // Corner radius is a theme decision, not a per-component constant:
    // Windows Classic and Metro are *defined* by square corners, Aqua by
    // lozenges. Call sites pass their design-intent radius through r()
    // and the active theme scales it. Identity for default/glass.
    readonly property bool squareCorners: _s ? _s.squareCorners === true : false
    readonly property real radiusScale:   _s ? _s.radiusScale : 1.0
    readonly property bool bevel:         _s ? _s.bevel === true : false
    readonly property bool gloss:         _s ? _s.gloss === true : false
    readonly property real elevation:     _s ? _s.elevation : 0

    function r(n) {
        if (squareCorners) return 0
        return n * radiusScale
    }

    // Windows Classic chisel. Unused (and harmless) when bevel is false.
    readonly property color bevelLight: (_p && _p.bevelLight) ? _p.bevelLight
                                                              : Qt.rgba(1,1,1,0.6)
    readonly property color bevelDark:  (_p && _p.bevelDark)  ? _p.bevelDark
                                                              : Qt.rgba(0,0,0,0.5)
    // Aqua/Aero specular sheen laid over the top half of a control.
    readonly property color glossTop:    (_p && _p.glossTop)    ? _p.glossTop
                                                                : Qt.rgba(1,1,1,0.5)
    readonly property color glossBottom: (_p && _p.glossBottom) ? _p.glossBottom
                                                                : Qt.rgba(1,1,1,0.05)
    // Aero outer glow / Material drop shadow.
    readonly property color glowColor:       (_p && _p.glowColor) ? _p.glowColor
                                                                  : Qt.rgba(0.3,0.6,1,0.5)
    readonly property color elevationShadow: (_p && _p.elevationShadow) ? _p.elevationShadow
                                                                        : Qt.rgba(0,0,0,0.3)
    // Window chrome. Windows Classic and Metro colour their title bar;
    // the others fall back to the panel colour so nothing changes.
    readonly property color titleBar:     (_p && _p.titleBar)     ? _p.titleBar     : bgPanel
    readonly property color titleBarText: (_p && _p.titleBarText) ? _p.titleBarText : textPrimary

    // ── backgrounds (lightest → darkest in dark mode) ─────────────
    // bgBase is the window's root fill. In glass mode it goes fully
    // transparent so the AmbientBackground (drawn behind it in main.qml)
    // is what you see through every translucent panel.
    readonly property color bgBase:       (_p && _p.bgBase !== undefined) ? _p.bgBase : (glass ? "transparent" : (dark ? "#1C1C1E" : "#F0EEE6"))
    readonly property color bgPanel:      (_p && _p.bgPanel !== undefined) ? _p.bgPanel : (glass ? (dark ? Qt.rgba(1,1,1,0.05) : Qt.rgba(1,1,1,0.44)) : (dark ? "#1A1A1C" : "#E8E4D8"))
    readonly property color bgContainer:  (_p && _p.bgContainer !== undefined) ? _p.bgContainer : (glass ? (dark ? Qt.rgba(1,1,1,0.07) : Qt.rgba(1,1,1,0.55)) : (dark ? "#1E1E20" : "#F7F5EE"))
    readonly property color bgElevated:   (_p && _p.bgElevated !== undefined) ? _p.bgElevated : (glass ? (dark ? Qt.rgba(1,1,1,0.12) : Qt.rgba(1,1,1,0.64)) : (dark ? "#252528" : "#EBE7DB"))
    // Input fields stay near-opaque even in glass mode — editable text
    // must stay legible over the busy ambient.
    readonly property color bgInput:      (_p && _p.bgInput !== undefined) ? _p.bgInput : (glass ? (dark ? Qt.rgba(0.16,0.16,0.19,0.86) : Qt.rgba(1,1,1,0.90)) : (dark ? "#2E2E33" : "#FDFCF8"))
    // Plot region background (the gutters between cells). Opaque in the
    // solid theme; transparent in glass so the ambient shows through the
    // gaps and the grid reads as floating glass tiles.
    readonly property color bgPlot:       (_p && _p.bgPlot !== undefined) ? _p.bgPlot : (glass ? "transparent" : (dark ? "#141417" : "#F5F3EB"))
    readonly property color bgHover:      (_p && _p.bgHover !== undefined) ? _p.bgHover : (glass ? (dark ? Qt.rgba(1,1,1,0.16) : Qt.rgba(1,1,1,0.78)) : (dark ? "#2E2E33" : "#E3DFD1"))
    readonly property color bgCard:       (_p && _p.bgCard !== undefined) ? _p.bgCard : (glass ? (dark ? Qt.rgba(1,1,1,0.08) : Qt.rgba(1,1,1,0.58)) : (dark ? "#262630" : "#FAF9F4"))
    readonly property color bgCardAlt:    (_p && _p.bgCardAlt !== undefined) ? _p.bgCardAlt : (glass ? (dark ? Qt.rgba(1,1,1,0.045): Qt.rgba(1,1,1,0.40)) : (dark ? "#232329" : "#F1EEE5"))

    // ── borders ───────────────────────────────────────────────────
    // In glass mode borders become bright hairlines — the light-catching
    // edge that sells the material. borderHover is the specular edge.
    readonly property color borderStrong: (_p && _p.borderStrong !== undefined) ? _p.borderStrong : (glass ? (dark ? Qt.rgba(1,1,1,0.14) : Qt.rgba(1,1,1,0.55)) : (dark ? "#2A2A2E" : "#CFC9BB"))
    readonly property color borderSubtle: (_p && _p.borderSubtle !== undefined) ? _p.borderSubtle : (glass ? (dark ? Qt.rgba(1,1,1,0.22) : Qt.rgba(1,1,1,0.65)) : (dark ? "#3E4E6F" : "#C6C0B0"))
    readonly property color borderHover:  (_p && _p.borderHover !== undefined) ? _p.borderHover : (glass ? (dark ? Qt.rgba(1,1,1,0.45) : Qt.rgba(1,1,1,0.85)) : (dark ? "#5A6B8C" : "#8F8877"))
    readonly property color borderSoft:   (_p && _p.borderSoft !== undefined) ? _p.borderSoft : (glass ? (dark ? Qt.rgba(1,1,1,0.10) : Qt.rgba(1,1,1,0.45)) : (dark ? "#333340" : "#D8D3C5"))

    // ── text ──────────────────────────────────────────────────────
    // Opaque in every mode. In glass+light the ambient is bright, so
    // keep the darkest ink; glass+dark keeps pure white.
    readonly property color textPrimary:   (_p && _p.textPrimary !== undefined) ? _p.textPrimary : (dark ? "#FFFFFF" : "#1F1E1B")
    readonly property color textSecondary: (_p && _p.textSecondary !== undefined) ? _p.textSecondary : (dark ? "#BDC3C7" : "#57544A")
    readonly property color textTertiary:  (_p && _p.textTertiary !== undefined) ? _p.textTertiary : (dark ? "#7F8C8D" : "#8A867A")
    readonly property color textDisabled:  (_p && _p.textDisabled !== undefined) ? _p.textDisabled : (dark ? "#555555" : "#B6B1A4")
    readonly property color textLink:      (_p && _p.textLink !== undefined) ? _p.textLink : (dark ? "#4A90E2" : "#2060C0")

    // ── interactive accent ────────────────────────────────────────
    // Hover fills, focus rings, toggle-on, selected rows, CTA chips.
    // Terracotta on the light paper palette; unchanged blue in dark
    // mode. Semantic blues (info toasts, active-camera dots, status
    // chips) stay on accentBlue in both modes — do not swap those.
    readonly property color accentInteractive: (_p && _p.accentInteractive !== undefined) ? _p.accentInteractive : (dark ? "#4A90E2" : "#D97757")

    // accentInteractive is a *fill* colour. Some chrome (the update banner's
    // white pill) paints accent-coloured text on a near-white fill instead,
    // and a light accent — Material dark's #BB86FC, say — drops under readable
    // contrast there. accentOnLight is the same hue, darkened only as far as
    // legibility on a light fill requires, and left untouched when the accent
    // is already dark enough.
    // Target luminance for accent ink sitting on a near-white fill. 0.40 keeps
    // the hue clearly recognisable while staying comfortably readable; going
    // darker starts turning every theme's accent into anonymous near-black.
    readonly property real _inkTargetLum: 0.40
    readonly property color accentOnLight: {
        var c = Qt.color(accentInteractive)
        var lum = 0.299 * c.r + 0.587 * c.g + 0.114 * c.b
        return lum > _inkTargetLum ? Qt.darker(c, lum / _inkTargetLum) : c
    }

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
    readonly property color statusGrey:   (_p && _p.statusGrey !== undefined) ? _p.statusGrey : (dark ? "#7F8C8D" : "#A8A399")

    // ── chart / plot specific ─────────────────────────────────────
    readonly property color plotGrid:     (_p && _p.plotGrid !== undefined) ? _p.plotGrid : (dark ? "#333333" : "#CFC9BA")
    readonly property color plotLabel:    (_p && _p.plotLabel !== undefined) ? _p.plotLabel : (dark ? "#999999" : "#6B675C")
    readonly property color plotText:     (_p && _p.plotText !== undefined) ? _p.plotText : (dark ? "#C9D1D9" : "#33312B")
    // Plot cell / scrubber track surface. In glass mode the cells become
    // lightly translucent frosted tiles (kept dark/light-dominant so the
    // white/ink traces and BFI/BVI numbers stay high-contrast — the soft
    // ambient only whispers through behind the data). Solid theme opaque.
    readonly property color plotCellBg:   (_p && _p.plotCellBg !== undefined) ? _p.plotCellBg : (glass ? (dark ? Qt.rgba(0.10,0.10,0.13,0.80) : Qt.rgba(1,1,1,0.82)) : (dark ? "#1A1A1C" : "#FFFFFF"))

    // ── floating overlays (pills, popups, tooltips over the plot) ──
    // Translucent in both modes; dark values match the previous
    // hard-coded Qt.rgba constants so dark mode is unchanged. Light
    // values are warm-tinted whites so overlays sit on the paper
    // palette without going clinical-white. In glass mode they lean
    // further into frost (lower alpha, cooler tint).
    readonly property color overlayBg:      (_p && _p.overlayBg !== undefined) ? _p.overlayBg : (glass ? (dark ? Qt.rgba(0.10,0.10,0.14,0.60) : Qt.rgba(1,1,1,0.62)) : (dark ? Qt.rgba(0.10, 0.10, 0.12, 0.82) : Qt.rgba(0.99, 0.98, 0.95, 0.90)))
    // overlayBgSolid is the "you are reading something off this" tier —
    // the hover tooltip, the window-length menu, the ⋯ settings popup. In
    // glass it therefore carries the same near-opaque weight as menuBg /
    // toastBg: these float over live traces, and a readout you can see the
    // plot through is a readout you have to squint at. (overlayBg above
    // stays airy — it backs pills and chips, which are chrome, not text
    // you stop to read.)
    readonly property color overlayBgSolid: (_p && _p.overlayBgSolid !== undefined) ? _p.overlayBgSolid : (glass ? (dark ? Qt.rgba(0.12,0.12,0.16,0.94) : Qt.rgba(1,1,1,0.94)) : (dark ? Qt.rgba(0.12, 0.12, 0.14, 0.96) : Qt.rgba(0.99, 0.985, 0.96, 0.97)))

    // ── toast surface ─────────────────────────────────────────────
    // Transient chrome that must stay readable over whatever it lands on,
    // including the busy ambient. In glass mode it can't inherit
    // bgElevated: that token is a 12% *white* wash, which would leave the
    // white body text sitting on bright frost. So toasts go dark-dominant
    // and near-opaque here (same trick as sheetBg / plotCellBg), a touch
    // lighter than a modal sheet so they still read as floating chrome
    // rather than a focused task surface. Solid-theme values are
    // byte-identical to bgElevated so turning glass off is a perfect
    // revert.
    readonly property color toastBg: (_p && _p.toastBg !== undefined) ? _p.toastBg : (glass ? (dark ? Qt.rgba(0.12,0.12,0.16,0.94) : Qt.rgba(1,1,1,0.94)) : (dark ? "#252528" : "#EBE7DB"))

    // ── menu / dropdown surface ───────────────────────────────────
    // The list panel a ComboBox drops open. Same reasoning as toastBg,
    // and more urgent: these popups previously took bgCard, which in
    // glass+dark is only 8% white — an open dropdown all but vanished
    // into the ambient, and its options are a list you have to read to
    // click. Solid-theme values are byte-identical to bgCard so turning
    // glass off is a perfect revert.
    readonly property color menuBg: (_p && _p.menuBg !== undefined) ? _p.menuBg : (glass ? (dark ? Qt.rgba(0.12,0.12,0.16,0.94) : Qt.rgba(1,1,1,0.94)) : (dark ? "#262630" : "#FAF9F4"))

    // ── modal sheet surface ───────────────────────────────────────
    // Modal panels (Settings, History, Contact Quality, …) need a much
    // heavier frost than chrome like the header: a focused task sheet
    // must abstract the busy plot grid behind it, not reveal it sharply.
    // Nearly opaque in glass (the material reads from its bright glass
    // border + the ambient around its edges, not from see-through).
    // Matches bgContainer exactly in the solid theme so nothing changes
    // when glass is off.
    readonly property color sheetBg: (_p && _p.sheetBg !== undefined) ? _p.sheetBg : (glass ? (dark ? Qt.rgba(0.12,0.12,0.16,0.97) : Qt.rgba(1,1,1,0.96)) : (dark ? "#1E1E20" : "#F7F5EE"))

    // ── liquid-glass helper tokens ────────────────────────────────
    // Used by GlassSurface / AmbientBackground and any panel that wants
    // to lean into the material explicitly. Harmless when glass is off
    // (nothing references them then).
    //
    // glassTint     — the frost fill laid over the blurred backdrop.
    // glassHighlight— bright specular line for the top edge.
    // glassEdge     — the thin outer stroke that defines the pane.
    // glassShadow   — soft drop shadow colour under a floating pane.
    readonly property color glassTint:      dark ? Qt.rgba(1,1,1,0.06) : Qt.rgba(1,1,1,0.30)
    readonly property color glassHighlight: dark ? Qt.rgba(1,1,1,0.55) : Qt.rgba(1,1,1,0.90)
    readonly property color glassEdge:      dark ? Qt.rgba(1,1,1,0.18) : Qt.rgba(1,1,1,0.70)
    readonly property color glassShadow:    dark ? Qt.rgba(0,0,0,0.55) : Qt.rgba(0.25,0.22,0.18,0.28)

    // AmbientBackground gradient + blob palette. Deep blue-violet dusk
    // in dark; pearlescent warm dawn in light.
    readonly property color ambientTop:     (_p && _p.ambientTop !== undefined) ? _p.ambientTop : (dark ? "#0B0D18" : "#EAF0F7")
    readonly property color ambientBottom:  (_p && _p.ambientBottom !== undefined) ? _p.ambientBottom : (dark ? "#141020" : "#F3ECF2")
    // blobA cool · blobB violet · blobC teal/warm
    readonly property color ambientBlobA:   (_p && _p.ambientBlobA !== undefined) ? _p.ambientBlobA : (dark ? "#2B4C8C" : "#BFD4F2")
    readonly property color ambientBlobB:   (_p && _p.ambientBlobB !== undefined) ? _p.ambientBlobB : (dark ? "#6E3B8C" : "#E7C9DA")
    readonly property color ambientBlobC:   (_p && _p.ambientBlobC !== undefined) ? _p.ambientBlobC : (dark ? "#1F6E7A" : "#CDE7E2")

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
