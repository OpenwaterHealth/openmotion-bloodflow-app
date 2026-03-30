import QtQuick
import QtQuick.Controls
import QtCharts
import QtQuick.Layouts 6.0
import QtQuick.Window 6.0

Rectangle {
    visible: true
    color: "black"
    Layout.preferredWidth: 800
    Layout.preferredHeight: 100
    radius: 0 
    antialiasing: false

    ChartView {
        id: bfbvView
        anchors.fill: parent
        backgroundColor: "#0000FFFF"
        theme: ChartView.ChartThemeDark
        antialiasing: false
        legend.visible: false
        backgroundRoundness: 0
        margins { top: 0; bottom: 0; left: 0; right: 0 }

        ValueAxis {
            id: xAxis
            min: 0
            max: 500 //!!!
            //titleText: "Time (s)"
            gridVisible: false
            visible: false
        }

        ValueAxis {
            id: yAxisBF
            tickCount: 5
            min: -1.2
            max: 1.2
            titleBrush: Qt.rgba(1, 0, 0, 1)
            titleText: "BF"
            color: "blue"
            gridLineColor: "red"
            labelsColor: "red"
            labelFormat: "%.1f"
        }

        ValueAxis {
            id: yAxisBV
            tickCount: 7
            min: -2.0
            max: 2.0
            titleBrush: Qt.rgba(0, 0, 1, 1)
            titleText: "BV"
            color: "red"
            gridLineColor: "blue"
            labelsColor: "blue"
            labelFormat: "%.1f"
        }

        LineSeries {
            id: series1
            axisX: xAxis
            axisY: yAxisBF
            color: "blue"
            // rescale
            function updateAxes() {
                if (count > 0) {
                    var yMin = at(0).y
                    var yMax = at(0).y
                    for (var i = 1; i < count; i++) {
                        var p = at(i)
                        yMin = Math.min(yMin, p.y)
                        yMax = Math.max(yMax, p.y)
                    }
                    // Apply new ranges with a 10% margin
                    var yMargin = (yMax - yMin) * 0.1 || 1
                    axisY.min = yMin - yMargin
                    axisY.max = yMax + yMargin
                }
            }
        }

        LineSeries {
            id: series2
            axisX: xAxis
            axisYRight: yAxisBV
            color: "red"
            // rescale
            function updateAxes() {
                if (count > 0) {
                    var yMin = at(0).y
                    var yMax = at(0).y
                    for (var i = 1; i < count; i++) {
                        var p = at(i)
                        yMin = Math.min(yMin, p.y)
                        yMax = Math.max(yMax, p.y)
                    }
                    // Apply new ranges with a 10% margin
                    var yMargin = (yMax - yMin) * 0.1 || 1
                    axisYRight.min = yMin - yMargin
                    axisYRight.max = yMax + yMargin
                }
            }
        }

        MouseArea {
            anchors.fill: parent
            property int lastX: 0

            onDoubleClicked: bfbvView.zoomReset(); // Resets view to show all data

            WheelHandler {
                id: wheelHandler
                onWheel: wheelEvent => {
                    const factor = wheelEvent.angleDelta.y > 0 ? 1.2 : 0.8;
                    // The default chart.zoom(factor) zooms around the center.
                    // For zooming at the mouse cursor position, more advanced logic
                    // involving axis range adjustments is typically required in C++.

                    // Zoom left axis if mouse on left half, right if on right

                    let wxx= wheelEvent.x;
                    //console.log("X is:", wxx);

                    if (wxx < bfbvView.width / 8) {
                        bfbvView.axisY.zoom(factor);
                    } else if (wxx > bfbvView.width / 8 * 7) {
                        bfbvView.axisYRight.zoom(factor);
                    }else{
                        bfbvView.zoom(factor);
                    }
                    wheelEvent.accepted = true;
                }
            }
            onPressed: {
                lastX = mouse.x
            }
            onPositionChanged: {
                if (lastX !== mouse.x) {
                    // Scroll by the difference in pixels
                    bfbvView.scrollRight(lastX - mouse.x) 
                    lastX = mouse.x
                }
            }
        }
    }

    // Connect Python signal to update QML
    Connections {
        target: bfSystem
        function onBfUpdated(x, y1, y2, y3, y4) {
            if(x > 20){
                //c = series1.count
                series1.append(x,y1)
                //series1.remove(0)
                series1.updateAxes()
                series2.append(x,y2)
                //series2.remove(0)
                series2.updateAxes()
            }else if ( x == 20){
                for (var i = 0; i < 500; i++) {
                    //series1.append(i, y1)
                    //series2.append(i, y2)
                }
            }
            // Scroll the chart
            if (x > 500) { //!!!
                xAxis.min = x - 500
                xAxis.max = x
            }
        }
    }
}
