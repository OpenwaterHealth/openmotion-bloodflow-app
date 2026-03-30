import QtQuick
import QtQuick.Controls
import QtCharts
import QtQuick.Layouts 6.0
import QtQuick.Window 6.0

ApplicationWindow {
    visible: true
    width: 800
    height: 400
    title: "BF_BV_plot QML"

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 0
        spacing: 0
        Rectangle {
            color: "white"
            Layout.fillWidth: true
            Layout.preferredHeight: 1
        }

        Loader {
            id: topLeftLoader
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.margins: 0
            source: "bfplot_qml.qml"
            // Ensures the Loader takes up only the space of the loaded item
            //Layout.preferredWidth: item ? item.implicitWidth : 0
            //Layout.preferredHeight: item ? item.implicitHeight : 0
            // Completely removes the item from the layout's calculation if not visible
            visible: status == Loader.Ready
        }
        Loader {
            id: bottomRightLoader
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.margins: 0
            source: "bfplot_qml.qml"
            // Ensures the Loader takes up only the space of the loaded item
            //Layout.preferredWidth: item ? item.implicitWidth : 0
            //Layout.preferredHeight: item ? item.implicitHeight : 0
            // Completely removes the item from the layout's calculation if not visible
            visible: status == Loader.Ready
        }
        //Item { Layout.fillHeight: true }
    }


}
