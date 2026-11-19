import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

// Engineering-mode unlock (#706). Not in a clinical bundle: main.qml loads
// this file through a Loader only when MotionInterface.engineeringUnlockAvailable
// (a Research build or the clinical service build), and the build leaves
// it out of a clinical bundle along with the Python unlock.
//
// It brings its own entry point: a double-click area placed over the header
// logo (logoItem, set by main.qml), so a build without this file has no
// logo double-click handler at all. MotionInterface.unlockEngineeringMode
// checks the engineering password in Python (engineering_unlock.py), turns
// engineering mode on for the session and audits the attempt.
PasswordPromptModal {
    id: root
    title: "Engineering Access"
    description: "Enter the engineering password to enable engineering mode."
    confirmLabel: "Unlock"
    submitHandler: function(pw) { return MotionInterface.unlockEngineeringMode(pw) }

    // The header logo the double-click area covers (WindowMenu.logoItem).
    property Item logoItem: null

    signal unlocked()

    onAccepted: {
        MotionInterface.notify("Engineering mode enabled.", "info", 3000, false, "engineering-mode")
        root.unlocked()
    }

    // Reparented onto the logo, so only the logo captures the double-click
    // and the rest of the header bar still drags the window.
    MouseArea {
        parent: root.logoItem
        visible: root.logoItem !== null
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton
        onDoubleClicked: root.open()
    }
}
