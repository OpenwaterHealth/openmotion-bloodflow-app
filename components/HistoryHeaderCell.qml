import QtQuick 6.0
import QtQuick.Layouts 6.0

// One clickable, sort-aware column header in the History table.
Item {
    id: cell
    property string text: ""
    property string sortName: ""
    property string activeSort: ""   // the table's current sort key
    property bool ascending: false
    signal sortRequested(string name)

    Layout.preferredHeight: 30

    AppTheme { id: theme }

    Row {
        anchors.verticalCenter: parent.verticalCenter
        spacing: 4
        Text {
            text: cell.text
            color: theme.textSecondary
            font.pixelSize: 12; font.weight: Font.DemiBold
        }
        Text {
            visible: cell.activeSort === cell.sortName
            text: cell.ascending ? "▲" : "▼"
            color: theme.textSecondary; font.pixelSize: 9
            anchors.verticalCenter: parent.verticalCenter
        }
    }
    MouseArea {
        anchors.fill: parent
        cursorShape: Qt.PointingHandCursor
        onClicked: cell.sortRequested(cell.sortName)
    }
}
