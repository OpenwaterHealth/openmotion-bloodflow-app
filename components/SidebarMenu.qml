import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0

Rectangle {
    id: sidebarMenu
    width: 60
    height: parent.height
    radius: 0
    color: "#2C3E50" // Dark sidebar background

    // Current active button index
    property int activeButtonIndex: 0

    // Whether the Check button is enabled (parent page disables during scans)
    property bool checkEnabled: true

    // Signal to handle button clicks
    signal buttonClicked(int index)

    // Reusable function for button handling
    function handleButtonClick(index) {
        activeButtonIndex = index;
        buttonClicked(index);
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 20
        Layout.alignment: Qt.AlignVCenter

        // Demo Button
        IconButton {
            buttonIcon: "\ueb34"
            buttonText: "Demo"
            Layout.alignment: Qt.AlignHCenter
            backgroundColor: sidebarMenu.activeButtonIndex === 0 ? "white" : "transparent"
            iconColor: sidebarMenu.activeButtonIndex === 0 ? "#2C3E50" : "#BDC3C7"
            onClicked: {
                sidebarMenu.handleButtonClick(0); // Call the global function
            }
        }

        // Data Analysis Button
        IconButton {
            buttonIcon: "\uf080"
            buttonText: "Analyze"
            Layout.alignment: Qt.AlignHCenter
            backgroundColor: sidebarMenu.activeButtonIndex === 1 ? "white" : "transparent"
            iconColor: sidebarMenu.activeButtonIndex === 1 ? "#2C3E50" : "#BDC3C7"
            onClicked: {
                sidebarMenu.handleButtonClick(1); // Call the global function
            }
        }

        // Check Button (contact quality check)
        IconButton {
            buttonIcon: "\ueaa3"
            buttonText: "Check"
            Layout.alignment: Qt.AlignHCenter
            enabled: sidebarMenu.checkEnabled
            opacity: enabled ? 1.0 : 0.4
            backgroundColor: sidebarMenu.activeButtonIndex === 2 ? "white" : "transparent"
            iconColor: sidebarMenu.activeButtonIndex === 2 ? "#2C3E50" : "#BDC3C7"
            onClicked: {
                sidebarMenu.handleButtonClick(2); // Call the global function
            }
        }

        // Settings Button
        //IconButton {
        //    buttonIcon: "\ueabf"
        //    buttonText: "Settings"
        //    Layout.alignment: Qt.AlignHCenter
        //    enabled: false
        //    backgroundColor: sidebarMenu.activeButtonIndex === 2 ? "white" : "transparent"
        //    iconColor: sidebarMenu.activeButtonIndex === 2 ? "#2C3E50" : "#BDC3C7"
        //    onClicked: {
        //        sidebarMenu.handleButtonClick(2); // Call the global function
        //    }
        //}
    }
}
