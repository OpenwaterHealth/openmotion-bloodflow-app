import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0

Item {
    id: histogramView
    width: 1024
    height: 300

    AppTheme { id: theme }

    property var histogramData: []
    property int maxValue: 1
    property bool showAxes: true

    signal saveRequested()
    signal exportCSVRequested()

    function forceRepaint() {
        histogramCanvas.requestPaint()
    }

    Rectangle {
        anchors.fill: parent
        color: theme.bgContainer
        border.color: theme.borderSubtle
        radius: 6

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 8
            spacing: 4

            RowLayout {
                Layout.fillWidth: true
                spacing: 10

                IconSmallButton {
                    iconGlyph: "\uea1d"  
                    buttonText: "Export CSV"
                    onClicked: {
                        if (histogramWidget.histogramData.length > 0) {
                            MOTIONConnector.saveHistogramToCSV(histogramWidget.histogramData)
                        }
                    }
                }

                IconSmallButton {
                    iconGlyph: "\uea4a"  
                    buttonText: "Save PNG"
                    onClicked: {
                        const timestamp = Qt.formatDateTime(new Date(), "yyyyMMdd_HHmmss");
                        const fullPath = "histogram_" + timestamp + ".png";
                        histogramWidget.grabToImage(function(result) {
                            result.saveToFile(fullPath);
                            console.log("Saved to", fullPath);
                        })
                    }
                }
            }

            Item {
                Layout.fillWidth: true
                Layout.fillHeight: true

                Canvas {
                    id: histogramCanvas
                    anchors.fill: parent
                    onPaint: {
                        let ctx = getContext("2d")
                        ctx.clearRect(0, 0, width, height)

                        const totalBins = histogramData.length
                        const padding = 40
                        const drawHeight = height - 2 * padding
                        const drawWidth = width - 2 * padding

                        // Axes
                        ctx.strokeStyle = theme.textSecondary
                        ctx.lineWidth = 1
                        ctx.beginPath()
                        ctx.moveTo(padding, padding)
                        ctx.lineTo(padding, height - padding)
                        ctx.lineTo(width - padding, height - padding)
                        ctx.stroke()

                        // Histogram
                        if (histogramData.length > 0) {
                            maxValue = Math.max(...histogramData)

                            const barWidth = drawWidth / totalBins

                            ctx.fillStyle = theme.accentBlue
                            for (let i = 0; i < totalBins; i++) {
                                let value = histogramData[i]
                                let barHeight = (value / maxValue) * drawHeight
                                let x = padding + i * barWidth
                                let y = height - padding - barHeight
                                ctx.fillRect(x, y, barWidth, barHeight)
                            }
                        }

                        // Y-axis label
                        ctx.save()
                        ctx.translate(10, height / 2)
                        ctx.rotate(-Math.PI / 2)
                        ctx.textAlign = "center"    
                        ctx.font = "12px sans-serif"                    
                        ctx.fillStyle = theme.textSecondary
                        ctx.fillText("Pixel Count", 0, 0)
                        ctx.restore()

                        // X-axis title
                        ctx.save()
                        ctx.textAlign = "center"
                        ctx.textBaseline = "bottom"
                        ctx.font = "12px sans-serif"      
                        ctx.fillStyle = theme.textSecondary
                        ctx.fillText("Intensity (0–1023)", width / 2, height - 2)
                        ctx.restore()
                    }
                }
            }
        }
    }

    Component.onCompleted: {
        if (histogramData.length === 0) {
            let fake = []
            for (let i = 0; i < 1024; i++) {
                let v = 1000 + Math.round(Math.sin(i * 0.05) * 700 + Math.random() * 200)
                fake.push(Math.max(0, v))
            }
            histogramData = fake
        }
    }
}
