import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

// Small password prompt for unlocking engineering mode. Opened from
// main.qml on a logo double-click. On the correct password it persists
// engineeringMode=true via setConfig and emits unlocked().
PasswordPromptModal {
    id: root
    title: "Engineering Access"
    description: "Enter the engineering password to enable engineering mode."
    confirmLabel: "Unlock"

    signal unlocked()

    onAccepted: {
        MotionInterface.setConfig("engineeringMode", true)
        MotionInterface.notify("Engineering mode enabled.", "info", 3000, false, "dev-mode")
        root.unlocked()
    }
}
