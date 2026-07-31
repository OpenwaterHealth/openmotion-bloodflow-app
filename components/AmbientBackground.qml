import QtQuick 6.0
import QtQuick.Effects
import OpenMotion 1.0

/*  AmbientBackground — the luminous backdrop that Liquid Glass panels
 *  reveal through their translucent fills. Rendered behind all content
 *  in main.qml, and only while AppTheme.glass is on (the caller gates
 *  visibility). Off the glass path this file is never instantiated.
 *
 *  Construction: an opaque vertical gradient base, plus a layer of a few
 *  large accent-coloured circles melted together by a heavy MultiEffect
 *  blur into soft glows. The circles drift on long, offset loops so the
 *  light feels alive without pulling focus in a clinical UI. Motion can
 *  be frozen with `animate: false`.
 */
Item {
    id: root
    property bool animate: true

    // Opaque base wash — nothing behind the window shows through, so the
    // glass look is consistent regardless of what's on the desktop.
    Rectangle {
        anchors.fill: parent
        gradient: Gradient {
            GradientStop { position: 0.0; color: AppTheme.ambientTop }
            GradientStop { position: 1.0; color: AppTheme.ambientBottom }
        }
    }

    // ── Vista aurora ───────────────────────────────────────────────────
    // Aero's desktop wasn't a diffuse wash, it was a light ribbon: a bright
    // filament sweeping the field with a broad glow beneath it. Built from
    // rotated, heavily-rounded bars so the curve reads without a shader —
    // the blur below melts the straight edges into a arc.
    Item {
        id: aurora
        anchors.fill: parent
        // Hidden and consumed as the blur source, same as the blob layer —
        // leaving it visible would draw the hard-edged bars on top of their
        // own blurred copy.
        visible: false
        layer.enabled: AppTheme.aeroGlass

        // Broad underglow that lifts the lower half off the deep blue.
        Rectangle {
            width: root.width * 1.6
            height: root.height * 0.75
            radius: height / 2
            x: -root.width * 0.30
            y: root.height * 0.42
            rotation: -9
            color: AppTheme.auroraGlow
            opacity: 0.55
        }
        // The bright filament itself, thin and higher up.
        Rectangle {
            width: root.width * 1.5
            height: root.height * 0.16
            radius: height / 2
            x: -root.width * 0.22
            y: root.height * 0.34
            rotation: -13
            color: AppTheme.auroraCore
            opacity: 0.75
            SequentialAnimation on y {
                running: root.animate; loops: Animation.Infinite
                NumberAnimation { to: root.height * 0.40; duration: 17000; easing.type: Easing.InOutSine }
                NumberAnimation { to: root.height * 0.34; duration: 17000; easing.type: Easing.InOutSine }
            }
        }
        // A second, fainter crossing ribbon — the depth cue that stops the
        // first one looking like a single painted stripe.
        Rectangle {
            width: root.width * 1.4
            height: root.height * 0.10
            radius: height / 2
            x: -root.width * 0.18
            y: root.height * 0.60
            rotation: 7
            color: AppTheme.auroraCore
            opacity: 0.35
        }
    }
    MultiEffect {
        anchors.fill: parent
        source: aurora
        visible: AppTheme.aeroGlass
        blurEnabled: true
        blur: 1.0
        blurMax: 64
        autoPaddingEnabled: false
    }

    // Raw blob layer — hidden; consumed as the blur source below.
    Item {
        id: blobs
        anchors.fill: parent
        visible: false
        layer.enabled: true

        // Each blob drifts on its own long, offset loop.
        Rectangle {
            id: blobA
            width: root.width * 0.75; height: width; radius: width / 2
            color: AppTheme.ambientBlobA
            opacity: AppTheme.dark ? 0.50 : 0.65
            x: -root.width * 0.15; y: -root.height * 0.20
            SequentialAnimation on x {
                running: root.animate; loops: Animation.Infinite
                NumberAnimation { to: root.width * 0.10;  duration: 19000; easing.type: Easing.InOutSine }
                NumberAnimation { to: -root.width * 0.15; duration: 19000; easing.type: Easing.InOutSine }
            }
            SequentialAnimation on y {
                running: root.animate; loops: Animation.Infinite
                NumberAnimation { to: root.height * 0.05;  duration: 23000; easing.type: Easing.InOutSine }
                NumberAnimation { to: -root.height * 0.20; duration: 23000; easing.type: Easing.InOutSine }
            }
        }
        Rectangle {
            id: blobB
            width: root.width * 0.70; height: width; radius: width / 2
            color: AppTheme.ambientBlobB
            opacity: AppTheme.dark ? 0.45 : 0.60
            x: root.width * 0.55; y: root.height * 0.35
            SequentialAnimation on x {
                running: root.animate; loops: Animation.Infinite
                NumberAnimation { to: root.width * 0.40; duration: 27000; easing.type: Easing.InOutSine }
                NumberAnimation { to: root.width * 0.60; duration: 27000; easing.type: Easing.InOutSine }
            }
            SequentialAnimation on y {
                running: root.animate; loops: Animation.Infinite
                NumberAnimation { to: root.height * 0.50; duration: 21000; easing.type: Easing.InOutSine }
                NumberAnimation { to: root.height * 0.25; duration: 21000; easing.type: Easing.InOutSine }
            }
        }
        Rectangle {
            id: blobC
            width: root.width * 0.60; height: width; radius: width / 2
            color: AppTheme.ambientBlobC
            opacity: AppTheme.dark ? 0.40 : 0.55
            x: root.width * 0.20; y: root.height * 0.55
            SequentialAnimation on x {
                running: root.animate; loops: Animation.Infinite
                NumberAnimation { to: root.width * 0.35; duration: 25000; easing.type: Easing.InOutSine }
                NumberAnimation { to: root.width * 0.10; duration: 25000; easing.type: Easing.InOutSine }
            }
            SequentialAnimation on y {
                running: root.animate; loops: Animation.Infinite
                NumberAnimation { to: root.height * 0.35; duration: 29000; easing.type: Easing.InOutSine }
                NumberAnimation { to: root.height * 0.60; duration: 29000; easing.type: Easing.InOutSine }
            }
        }
    }

    // Melt the blobs into broad, soft light.
    MultiEffect {
        anchors.fill: parent
        source: blobs
        blurEnabled: true
        blur: 1.0
        blurMax: 64
        blurMultiplier: 1.0
        autoPaddingEnabled: false
        // This layer paints after the aurora, so at full strength it would
        // smother the ribbon it is supposed to sit behind. Under Aero the
        // blobs are atmosphere, not the subject.
        opacity: AppTheme.aeroGlass ? 0.35 : 1.0
    }

    // Faint top sheen so the whole surface has a subtle vertical fall-off
    // of light, the way a real glass stack catches the sky.
    Rectangle {
        anchors.fill: parent
        gradient: Gradient {
            GradientStop { position: 0.0; color: AppTheme.dark ? Qt.rgba(1,1,1,0.05) : Qt.rgba(1,1,1,0.18) }
            GradientStop { position: 0.4; color: "transparent" }
            GradientStop { position: 1.0; color: AppTheme.dark ? Qt.rgba(0,0,0,0.15) : Qt.rgba(0,0,0,0.03) }
        }
    }
}
