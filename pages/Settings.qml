import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import "../components"

Rectangle {
    id: settings
    width: parent.width
    height: parent.height
    color: theme.bgElevated
    radius: 20
    opacity: 0.95 // Slight transparency for the content area

    AppTheme { id: theme }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 20
        spacing: 15

        // Title
        Text {
            text: "Settings"
            font.pixelSize: 20
            font.weight: Font.Bold
            color: theme.textPrimary
            horizontalAlignment: Text.AlignHCenter
            Layout.alignment: Qt.AlignHCenter
        }

        // Content Section
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            color: theme.bgContainer
            radius: 10
            border.color: theme.borderSubtle
            border.width: 2

            Text {
                text: "Settings"
                font.pixelSize: 16
                color: theme.textSecondary
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
                anchors.centerIn: parent
            }
        }

        // Buttons or Actions
        RowLayout {
            Layout.alignment: Qt.AlignHCenter
            spacing: 20

            Button {
                text: "Action 1"
                onClicked: {
                    console.log("Action 1 clicked");
                }
            }

            Button {
                text: "Action 2"
                onClicked: {
                    console.log("Action 2 clicked");
                }
            }
        }
    }
}
