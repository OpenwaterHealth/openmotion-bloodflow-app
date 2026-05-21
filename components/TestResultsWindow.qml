import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import QtQuick.Window 6.0
import OpenMotion 1.0

Window {
    id: testWin
    title: "Test Results"
    width: 920
    height: 480
    minimumWidth: 720
    minimumHeight: 360
    flags: Qt.Window
    modality: Qt.NonModal

    AppTheme { id: theme }

    color: theme.bgBase

    readonly property var rows: MOTIONInterface.testScanRows
    readonly property string status: MOTIONInterface.testScanStatus
    readonly property string failureReason: MOTIONInterface.testScanFailureReason
    readonly property bool running: MOTIONInterface.testScanRunning

    function _fmtNum(v, decimals) {
        if (v === null || v === undefined) return ""
        if (typeof v !== "number") return String(v)
        if (isNaN(v)) return ""
        return v.toFixed(decimals)
    }

    function _copyToClipboard() {
        var lines = []
        lines.push([
            "Side", "Cam", "LightMean", "MinMean", "MeanPF",
            "DarkMean", "MaxDark", "DarkPF",
            "Contrast", "MinContrast", "ContrastPF", "Overall",
        ].join("\t"))
        for (var i = 0; i < testWin.rows.length; i++) {
            var r = testWin.rows[i]
            lines.push([
                r.side, r.cam,
                _fmtNum(r.light_mean, 2),
                _fmtNum(r.min_mean, 2),
                r.mean_pf,
                _fmtNum(r.dark_mean, 2),
                _fmtNum(r.max_dark, 2),
                r.dark_pf,
                _fmtNum(r.contrast, 5),
                _fmtNum(r.min_contrast, 4),
                r.contrast_pf,
                r.overall,
            ].join("\t"))
        }
        MOTIONInterface.copyToClipboard(lines.join("\n"))
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        spacing: 8

        // Header strip — live status + Copy + Close
        RowLayout {
            Layout.fillWidth: true
            spacing: 12

            Text {
                Layout.fillWidth: true
                text: {
                    if (testWin.running) return "Running…"
                    switch (testWin.status) {
                    case "done":    return "PASS"
                    case "failed":
                        return testWin.failureReason
                            ? "FAIL — " + testWin.failureReason
                            : "FAIL"
                    case "aborted": return "Aborted"
                    default:        return ""
                    }
                }
                color: {
                    switch (testWin.status) {
                    case "done":    return "#4CAF50"
                    case "failed":  return "#F44336"
                    case "aborted": return "#FF9800"
                    case "running": return "#2196F3"
                    default:        return theme.textPrimary
                    }
                }
                font.bold: true
                font.pixelSize: 16
                wrapMode: Text.WordWrap
            }

            Button {
                text: "Copy"
                enabled: testWin.rows && testWin.rows.length > 0
                onClicked: testWin._copyToClipboard()
            }
            Button {
                text: "Close"
                onClicked: testWin.close()
            }
        }

        // Table header
        Rectangle {
            Layout.fillWidth: true
            implicitHeight: 28
            color: theme.bgCard
            border.color: theme.borderSoft
            border.width: 1
            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 8
                anchors.rightMargin: 8
                spacing: 0
                Text { Layout.preferredWidth: 50;  text: "Side"; color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
                Text { Layout.preferredWidth: 40;  text: "Cam";  color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
                Text { Layout.preferredWidth: 90;  text: "Light Mean";    color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
                Text { Layout.preferredWidth: 70;  text: "Min Mean";      color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
                Text { Layout.preferredWidth: 60;  text: "Mean PF";       color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
                Text { Layout.preferredWidth: 90;  text: "Dark Mean";     color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
                Text { Layout.preferredWidth: 70;  text: "Max Dark";      color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
                Text { Layout.preferredWidth: 60;  text: "Dark PF";       color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
                Text { Layout.preferredWidth: 90;  text: "Contrast";      color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
                Text { Layout.preferredWidth: 90;  text: "Min Contrast";  color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
                Text { Layout.preferredWidth: 80;  text: "Contrast PF";   color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
                Text { Layout.fillWidth: true;     text: "Overall";       color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
            }
        }

        // Table body
        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true

            Column {
                width: parent.width
                spacing: 0
                Repeater {
                    model: testWin.rows
                    delegate: Rectangle {
                        width: parent.width
                        implicitHeight: 24
                        color: (index % 2 === 0) ? "transparent" : Qt.darker(theme.bgBase, 1.05)
                        border.color: theme.borderSoft
                        border.width: 0

                        property color _passColor: "#4CAF50"
                        property color _failColor: "#F44336"
                        property color _naColor:   theme.textSecondary
                        function _pfColor(s) {
                            if (s === "PASS") return _passColor
                            if (s === "FAIL") return _failColor
                            return _naColor
                        }

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 8
                            anchors.rightMargin: 8
                            spacing: 0

                            Text { Layout.preferredWidth: 50;  text: modelData.side; color: theme.textPrimary;  font.family: "Consolas"; font.pixelSize: 12 }
                            Text { Layout.preferredWidth: 40;  text: modelData.cam;  color: theme.textPrimary;  font.family: "Consolas"; font.pixelSize: 12 }
                            Text { Layout.preferredWidth: 90;  text: testWin._fmtNum(modelData.light_mean, 2); color: theme.textPrimary;  font.family: "Consolas"; font.pixelSize: 12 }
                            Text { Layout.preferredWidth: 70;  text: testWin._fmtNum(modelData.min_mean, 2);   color: theme.textSecondary; font.family: "Consolas"; font.pixelSize: 12 }
                            Text { Layout.preferredWidth: 60;  text: modelData.mean_pf;     color: parent.parent._pfColor(modelData.mean_pf); font.family: "Consolas"; font.bold: true; font.pixelSize: 12 }
                            Text { Layout.preferredWidth: 90;  text: testWin._fmtNum(modelData.dark_mean, 2); color: theme.textPrimary;  font.family: "Consolas"; font.pixelSize: 12 }
                            Text { Layout.preferredWidth: 70;  text: testWin._fmtNum(modelData.max_dark, 2);  color: theme.textSecondary; font.family: "Consolas"; font.pixelSize: 12 }
                            Text { Layout.preferredWidth: 60;  text: modelData.dark_pf;     color: parent.parent._pfColor(modelData.dark_pf); font.family: "Consolas"; font.bold: true; font.pixelSize: 12 }
                            Text { Layout.preferredWidth: 90;  text: testWin._fmtNum(modelData.contrast, 5); color: theme.textPrimary;  font.family: "Consolas"; font.pixelSize: 12 }
                            Text { Layout.preferredWidth: 90;  text: testWin._fmtNum(modelData.min_contrast, 4); color: theme.textSecondary; font.family: "Consolas"; font.pixelSize: 12 }
                            Text { Layout.preferredWidth: 80;  text: modelData.contrast_pf; color: parent.parent._pfColor(modelData.contrast_pf); font.family: "Consolas"; font.bold: true; font.pixelSize: 12 }
                            Text { Layout.fillWidth: true;     text: modelData.overall;    color: parent.parent._pfColor(modelData.overall);    font.family: "Consolas"; font.bold: true; font.pixelSize: 12 }
                        }
                    }
                }
            }
        }
    }
}
