import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

// Small password prompt for unlocking developer mode. Opened from
// main.qml on a logo double-click. On the correct password it persists
// developerMode=true via setConfig and emits unlocked().
PasswordPromptModal {
    id: root
    title: "Developer Access"
    description: "Enter the developer password to enable developer mode."
    confirmLabel: "Unlock"

    signal unlocked()

    onAccepted: {
        MotionInterface.setConfig("developerMode", true)
        MotionInterface.notify("Developer mode enabled.", "info", 3000, false, "dev-mode")
        root.unlocked()
    }
}
