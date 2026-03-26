import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0 
import QtQuick.Dialogs as Dialogs


import "../components"
import "./scan"

Rectangle {
    id: bloodFlow
    width: parent.width
    height: parent.height
    color: "#29292B" // Background color for Page 1
    radius: 20
    opacity: 0.95 // Slight transparency for the content area

    property bool advancedSensors: (AppFlags && AppFlags.advancedSensors) ? AppFlags.advancedSensors : false
    property bool realtimePlotEnabled: (AppFlags && AppFlags.realtimePlotEnabled) ? AppFlags.realtimePlotEnabled : false
    property int defaultLeftMask:  (AppFlags && AppFlags.leftMask  !== undefined) ? AppFlags.leftMask  : 0x99
    property int defaultRightMask: (AppFlags && AppFlags.rightMask !== undefined) ? AppFlags.rightMask : 0x99
    // property to store selected directory
    property string defaultDataDir: ""

    ListModel {
        id: sensorPatterns
        ListElement { name: "None"; maskHex: "0x00" }
        ListElement { name: "Near"; maskHex: "0x5A" }
        ListElement { name: "Middle";  maskHex: "0x66" } 
        ListElement { name: "Far";  maskHex: "0x55" }  
        ListElement { name: "Outer";  maskHex: "0x99" }  
        ListElement { name: "Left";  maskHex: "0x0F" }  
        ListElement { name: "Right";  maskHex: "0xF0" }  
        ListElement { name: "Third Row";  maskHex: "0x42" }  
        ListElement { name: "All";  maskHex: "0xFF" }  
    }

    // Convert boolean array -> integer mask.  bitMap[i] is the bit position for array index i.
    function maskFromArray(arr) {
        if (!arr || arr.length !== 8) return 0;
        const bitMap = [7, 6, 5, 4, 3, 2, 1, 0];  // index i -> bit number
        var m = 0;
        for (var i = 0; i < 8; i++) {
            if (arr[i]) m |= (1 << bitMap[i]);
        }
        return m;
    }

    // Convert integer mask -> boolean array (inverse of maskFromArray).
    function arrayFromMask(mask) {
        const bitMap = [7, 6, 5, 4, 3, 2, 1, 0];
        var arr = [];
        for (var i = 0; i < 8; i++) {
            arr.push(Boolean(mask & (1 << bitMap[i])));
        }
        return arr;
    }

    // Return the sensorPatterns combo-box index whose maskHex matches mask, or 0 (None) if not found.
    function indexForMask(mask) {
        for (var i = 0; i < sensorPatterns.count; i++) {
            if (parseInt(sensorPatterns.get(i).maskHex, 16) === mask) return i;
        }
        return 0;
    }

    // Reactive masks (auto-update when sensorActive arrays change)
    property int leftMask:  maskFromArray(leftSensorView.sensorActive)
    property int rightMask: maskFromArray(rightSensorView.sensorActive)

    // LAYOUT
    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 30   // extra left space
        anchors.topMargin: 10
        anchors.rightMargin: 10
        anchors.bottomMargin: 10
        spacing: 10

        // Left Column (Input Panel)
        ColumnLayout {
            spacing: 20

            // Info container
            Rectangle {
                id: patientInfo
                width: 500
                height: 500
                color: "#1E1E20"
                radius: 10
                border.color: "#3E4E6F"
                border.width: 2

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 20
                    spacing: 20

                    // Title
                    Text {
                        text: "Patient Info"
                        font.pixelSize: 20
                        color: "#FFFFFF"
                        Layout.alignment: Qt.AlignHCenter
                    }

                    // Subject ID Field
                    RowLayout {
                        spacing: 10
                        Layout.fillWidth: true

                        Text {
                            text: "Subject ID:"
                            font.pixelSize: 16
                            color: "#BDC3C7"
                            Layout.alignment: Qt.AlignVCenter
                        }

                        TextField {
                            id: subjectIdField
                            text: MOTIONInterface.subjectId
                            font.pixelSize: 16
                            color: "white"
                            Layout.fillWidth: true
                            background: Rectangle {
                                color: "#2E2E33"
                                radius: 4
                                border.color: "#3E4E6F"
                                border.width: 1
                            }

                            // push UI -> backend when user commits
                            onEditingFinished: {
                                if (text !== MOTIONInterface.subjectId)
                                    MOTIONInterface.subjectId = text
                            }
                        }
                    }

                    // Notes Field (multi-line) - FIXED
                    ColumnLayout {
                        spacing: 6
                        Layout.fillWidth: true
                        Layout.fillHeight: true

                        Text {
                            text: "Notes:"
                            font.pixelSize: 16
                            color: "#BDC3C7"
                            horizontalAlignment: Text.AlignLeft
                            Layout.alignment: Qt.AlignLeft
                        }

                        Rectangle {
                            color: "#2E2E33"
                            radius: 6
                            border.color: "#3E4E6F"
                            border.width: 1
                            Layout.fillWidth: true
                            Layout.fillHeight: true

                            TextArea {
                                id: notesField
                                anchors.fill: parent
                                anchors.margins: 15
                                font.pixelSize: 14
                                color: "white"
                                wrapMode: Text.Wrap
                                background: null  // Use parent rectangle

                                text: MOTIONInterface.scanNotes   // Initial value from backend
                                onTextChanged: MOTIONInterface.scanNotes = text  // Push updates back
                            }
                        }
                    }
                }
            }

            // Data container
            Rectangle {
                id: dataDirInfo
                width: 500
                height: 160
                color: "#1E1E20"
                radius: 10
                border.color: "#3E4E6F"
                border.width: 2
                enabled: true

                // Default Data Directory Selection
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 10
                    spacing: 10

                    Text {
                        text: "Default Data Directory:"
                        font.pixelSize: 16
                        color: "#BDC3C7"
                    }

                    TextField {
                        id: directoryField
                        text: MOTIONInterface.directory
                        readOnly: true
                        font.pixelSize: 14
                        color: "white"
                        Layout.fillWidth: true
                        background: Rectangle {
                            color: "#2E2E33"
                            radius: 4
                            border.color: "#3E4E6F"
                            border.width: 1
                        }
                    }
                    
                    Button {
                        id: btnBrowse
                        text: "Browse"
                        Layout.preferredWidth: 120
                        Layout.preferredHeight: 40
                        Layout.alignment: Qt.AlignRight
                        hoverEnabled: enabled          

                        contentItem: Text {
                            text: parent.text
                            font.pixelSize: 14
                            color: parent.enabled ? "#BDC3C7" : "#7F8C8D"
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                        background: Rectangle {
                            color: !parent.enabled ? "#3A3F4B" : parent.hovered ? "#4A90E2" : "#3A3F4B"
                            border.color: !parent.enabled ? "#7F8C8D" : parent.hovered ? "#FFFFFF" : "#BDC3C7"
                            radius: 4
                        }
                        onClicked: {
                            folderDialog.open()
                        }
                    }

                    Dialogs.FolderDialog {
                        id: folderDialog
                        title: "Select Default Data Directory"
                        currentFolder: Qt.platform.os === "windows"
                            ? "file:///" + MOTIONInterface.directory.replace("\\", "/")
                            : MOTIONInterface.directory

                        onAccepted: {
                            MOTIONInterface.directory = folderDialog.selectedFolder.toString().replace("file:///", "")
                        }
                    }
                }
            }
        }

        // RIGHT COLUMN (Status Panel + Histogram)
        ColumnLayout {
            spacing: 20

            // Sensor Panel
            Rectangle {
                id: sensorSelection
                width: 500
                height: 360
                color: "#1E1E20"
                radius: 10
                border.color: "#3E4E6F"
                border.width: 2
                
                RowLayout {
                    anchors.fill: parent
                    anchors.margins: 20
                    spacing: 40

                    // Left Sensor Column
                    ColumnLayout {
                        spacing: 10
                        Layout.alignment: Qt.AlignHCenter

                        SensorView {
                            id: leftSensorView
                            title: "Left Sensor"
                            sensorSide: "left"
                            connector: MOTIONInterface
                        }

                        ComboBox {
                            id: leftSensorSelector
                            Layout.preferredWidth: 200
                            Layout.preferredHeight: 40
                            model: sensorPatterns
                            textRole: "name" 
                            enabled: MOTIONInterface.leftSensorConnected
                            opacity: enabled ? 1.0 : 0.4

                            onCurrentIndexChanged: {
                                switch (currentIndex) {
                                    case 0: leftSensorView.sensorActive = [false,false,false,false,false,false,false,false]; break;  // 0x00
                                    case 1: leftSensorView.sensorActive = [false,true,false,true,true,false,true,false]; break;  // 0x5A
                                    case 2: leftSensorView.sensorActive = [false,true,true,false,false,true,true,false]; break;  // 0x66
                                    case 3: leftSensorView.sensorActive = [true,false,true,false,false,true,false,true]; break;  // 0xA5
                                    case 4: leftSensorView.sensorActive = [true,false,false,true,true,false,false,true]; break;  // 0x99
                                    case 5: leftSensorView.sensorActive = [false,false,false,false,true,true,true,true]; break;  // 0x0F
                                    case 6: leftSensorView.sensorActive = [true,true,true,true,false,false,false,false]; break;  // 0xF0
                                    case 7: leftSensorView.sensorActive = [false,true,false,false,false,false,true,false]; break;  // 0x42 Third Row
                                    case 8: leftSensorView.sensorActive = [true,true,true,true,true,true,true,true]; break;  // 0xFF
                                }
                            }

                            Component.onCompleted: {
                                currentIndex = indexForMask(bloodFlow.defaultLeftMask)
                                leftSensorView.sensorActive = arrayFromMask(bloodFlow.defaultLeftMask)
                            }
                        }
                    }

                    // Right Sensor Column
                    ColumnLayout {
                        spacing: 10
                        Layout.alignment: Qt.AlignHCenter

                        SensorView {
                            id: rightSensorView
                            title: "Right Sensor"
                            sensorSide: "right"
                            connector: MOTIONInterface
                        }

                        ComboBox {
                            id: rightSensorSelector
                            Layout.preferredWidth: 200
                            Layout.preferredHeight: 40
                            model: sensorPatterns
                            textRole: "name" 
                            enabled: MOTIONInterface.rightSensorConnected
                            opacity: enabled ? 1.0 : 0.4

                            onCurrentIndexChanged: {
                                switch (currentIndex) {
                                    case 0: rightSensorView.sensorActive = [false,false,false,false,false,false,false,false]; break;  // 0x00
                                    case 1: rightSensorView.sensorActive = [false,true,false,true,true,false,true,false]; break;  // 0x5A
                                    case 2: rightSensorView.sensorActive = [false,true,true,false,false,true,true,false]; break;  // 0x66
                                    case 3: rightSensorView.sensorActive = [true,false,true,false,false,true,false,true]; break;  // 0xA5
                                    case 4: rightSensorView.sensorActive = [true,false,false,true,true,false,false,true]; break;  // 0x99
                                    case 5: rightSensorView.sensorActive = [false,false,false,false,true,true,true,true]; break;  // 0x0F
                                    case 6: rightSensorView.sensorActive = [true,true,true,true,false,false,false,false]; break;  // 0xF0
                                    case 7: rightSensorView.sensorActive = [false,true,false,false,false,false,true,false]; break;  // 0x42 Third Row
                                    case 8: rightSensorView.sensorActive = [true,true,true,true,true,true,true,true]; break;  // 0xFF
                                }                                
                            }

                            Component.onCompleted: {
                                currentIndex = indexForMask(bloodFlow.defaultRightMask)
                                rightSensorView.sensorActive = arrayFromMask(bloodFlow.defaultRightMask)
                            }
                        }
                    }
                }
            }

            // Status Panel (Connection Indicators)
            Rectangle {
                id: statusPanel
                width: 500
                height: 120
                color: "#1E1E20"
                radius: 10
                border.color: "#3E4E6F"
                border.width: 2
                

                RowLayout {
                    anchors.fill: parent
                    anchors.margins: 10
                    spacing: 20

                    // Right Column: status and indicators
                    ColumnLayout {
                        spacing: 10
                        Layout.preferredWidth: statusPanel.width/2
                        Layout.fillHeight: true
                        Layout.alignment: Qt.AlignVCenter | Qt.AlignHCenter

                        Item { Layout.fillWidth: true }

                        RowLayout {
                            spacing: 30
                            Layout.alignment: Qt.AlignHCenter

                            // Sensor Indicator
                            ColumnLayout {
                                spacing: 2
                                Layout.alignment: Qt.AlignHCenter

                                Text {
                                    text: "Sensors"
                                    font.pixelSize: 14
                                    color: "#BDC3C7"
                                    horizontalAlignment: Text.AlignHCenter
                                }

                                RowLayout {
                                    spacing: 4
                                    Layout.alignment: Qt.AlignHCenter

                                    Rectangle {
                                        width: 20; height: 20; radius: 10
                                        color: MOTIONInterface.leftSensorConnected ? "green" : "red"
                                        border.color: "black"; border.width: 1
                                    }

                                    Rectangle {
                                        width: 20; height: 20; radius: 10
                                        color: MOTIONInterface.rightSensorConnected ? "green" : "red"
                                        border.color: "black"; border.width: 1
                                    }
                                }
                            }

                            // Console Indicator
                            ColumnLayout {
                                spacing: 4
                                Layout.alignment: Qt.AlignHCenter

                                Text {
                                    text: "Console"
                                    font.pixelSize: 14
                                    color: "#BDC3C7"
                                    horizontalAlignment: Text.AlignHCenter
                                    Layout.alignment: Qt.AlignHCenter
                                }

                                Rectangle {
                                    width: 20; height: 20; radius: 10
                                    color: MOTIONInterface.consoleConnected ? "green" : "red"
                                    border.color: "black"; border.width: 1
                                    Layout.alignment: Qt.AlignHCenter
                                }
                            }

                            // Laser Indicator
                            ColumnLayout {
                                spacing: 4
                                Layout.alignment: Qt.AlignHCenter

                                Text {
                                    text: "Laser"
                                    font.pixelSize: 14
                                    color: "#BDC3C7"
                                    horizontalAlignment: Text.AlignHCenter
                                    Layout.alignment: Qt.AlignHCenter
                                }

                                Rectangle {
                                    width: 20; height: 20; radius: 10
                                    color: MOTIONInterface.triggerState === "ON" ? "green" : "red"
                                    border.color: "black"; border.width: 1
                                    Layout.alignment: Qt.AlignHCenter
                                }
                            }

                            // Failure Indicator
                            RowLayout {
                                spacing: 6
                                Layout.alignment: Qt.AlignHCenter

                                ColumnLayout {
                                    spacing: 4
                                    Layout.alignment: Qt.AlignHCenter

                                    Text {
                                        text: "Safety"
                                        font.pixelSize: 14
                                        color: "#BDC3C7"
                                        horizontalAlignment: Text.AlignHCenter
                                        Layout.alignment: Qt.AlignHCenter
                                    }

                                    Rectangle {
                                        width: 20; height: 20; radius: 10
                                        color: MOTIONInterface.safetyFailure ? "red" : "green"
                                        border.color: "black"; border.width: 1
                                        Layout.alignment: Qt.AlignHCenter
                                    }
                                }
                            }
                        }
                    }
                }
            }

            // Controls
            Rectangle {
                id: controlPanel
                width: 500
                height: 160
                color: "#1E1E20"
                radius: 10
                border.color: "#3E4E6F"
                border.width: 2

                // public duration value (seconds)
                property int durationSec: 16

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 16
                    spacing: 12

                    // === Duration row ===
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 12

                        Text {
                            text: "Duration:"
                            color: "#BDC3C7"
                            font.pixelSize: 14
                            Layout.alignment: Qt.AlignVCenter
                        }

                        Slider {
                            id: durationSlider
                            from: 16
                            to: 43200
                            stepSize: 1
                            snapMode: Slider.SnapOnRelease
                            value: controlPanel.durationSec
                            Layout.fillWidth: true
                            onValueChanged: controlPanel.durationSec = Math.round(value)
                        }

                        TextField {
                            id: durationEdit
                            text: String(controlPanel.durationSec)
                            inputMethodHints: Qt.ImhDigitsOnly
                            validator: IntValidator { bottom: 0; top: 43200  }
                            font.pixelSize: 14
                            color: "white"
                            horizontalAlignment: Text.AlignHCenter
                            Layout.preferredWidth: 80
                            background: Rectangle {
                                color: "#2E2E33"; radius: 4
                                border.color: "#3E4E6F"; border.width: 1
                            }

                            // keep in sync with slider, clamp 0..43200 
                            onEditingFinished: {
                                let v = parseInt(text);
                                if (isNaN(v)) v = controlPanel.durationSec;
                                v = advancedSensors? Math.max(0, Math.min(43200, v)) : Math.max(0, Math.min(43200 , v));
                                controlPanel.durationSec = v;
                                durationSlider.value = v;
                                text = String(v);
                            }
                        }

                        Text {
                            text: "sec"
                            color: "#BDC3C7"
                            font.pixelSize: 14
                            Layout.alignment: Qt.AlignVCenter
                        }
                    }

                    // spacer pushes buttons to bottom
                    Item { Layout.fillHeight: true }

                    // === Bottom buttons ===
                    RowLayout {
                        Layout.alignment: Qt.AlignHCenter
                        spacing: 40

                        Button {
                            id: btnStartScan
                            text: "Start Scan"
                            Layout.preferredWidth: 140
                            Layout.preferredHeight: 60
                            hoverEnabled: enabled
                            enabled: MOTIONInterface.consoleConnected && (MOTIONInterface.leftSensorConnected || MOTIONInterface.rightSensorConnected)

                            contentItem: Text {
                                text: parent.text
                                font.pixelSize: 16
                                color: parent.enabled ? "#BDC3C7" : "#7F8C8D"
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
                            background: Rectangle {
                                color: !parent.enabled ? "#3A3F4B" : parent.hovered ? "#4A90E2" : "#3A3F4B"
                                border.color: !parent.enabled ? "#7F8C8D" : parent.hovered ? "#FFFFFF" : "#BDC3C7"
                                radius: 4
                            }
                            onClicked: {
                                console.log("Start Scan", controlPanel.durationSec, "sec");
                                // start scan
                                // MOTIONInterface.startTrigger() 
                                //scanDialog.message = "Scanning"
                                //scanDialog.open()
                                
                                scanDialog.message = "Scanning…";
                                scanDialog.stageText = "Preparing…";
                                scanDialog.progress = 1;
                                scanDialog.open();
                                if (realtimePlotEnabled) {
                                    meanPlotWindow.startScan(bloodFlow.leftMask, bloodFlow.rightMask);
                                }
                                scanRunner.start();
                            }
                        }
                        
                    }
                }
            }


        }

    }
    
    // **Connections for MOTIONConnector signals**
    Connections {
        target: MOTIONInterface

        // Forward captureLog messages to QML console.log (which gets logged via qt_message_handler)
        function onCaptureLog(line) {
            console.log("Capture log: "+line)
        }

        function onSignalConnected(descriptor, port) {
            console.log(descriptor + " connected on " + port);

            if ((descriptor || "").toUpperCase() === "CONSOLE") {
                
            }
        }

        function onSignalDisconnected(descriptor, port) {
            console.log(descriptor + " disconnected from " + port);

            if ((descriptor || "").toUpperCase() === "CONSOLE") {
            }
        }

        function onSignalDataReceived(descriptor, message) {
            console.log("Data from " + descriptor + ": " + message);
        }
        
        function onConnectionStatusChanged() {
            if (!MOTIONInterface.leftSensorConnected) {
                leftSensorSelector.currentIndex = 0
                leftSensorView.resetCamerasWhenDisconnected()
            } else {
                leftSensorSelector.currentIndex = indexForMask(bloodFlow.defaultLeftMask)
                leftSensorView.sensorActive = arrayFromMask(bloodFlow.defaultLeftMask)
            }
            if (!MOTIONInterface.rightSensorConnected) {
                rightSensorSelector.currentIndex = 0
                rightSensorView.resetCamerasWhenDisconnected()
            } else {
                rightSensorSelector.currentIndex = indexForMask(bloodFlow.defaultRightMask)
                rightSensorView.sensorActive = arrayFromMask(bloodFlow.defaultRightMask)
            }
        }
        
        function onLaserStateChanged() {          
        }
        
        function onSafetyFailureStateChanged() {          
        }

    }

    Component.onDestruction: {
        console.log("Closing UI, clearing MOTIONInterface...");
    }

    ScanProgressDialog {
        id: scanDialog
        onCancelRequested: {
            if (scanDialog.done) {
                // After success or error review, Close should just dismiss
                scanDialog.close()
            } else {
                // While running, Cancel should actually cancel the scan
                scanRunner.cancel()
            }
        }
    }

    ScanRunner {
        id: scanRunner
        // connector: use the globally-exposed connector if it exists, else null
        connector: MOTIONInterface

        // inputs with safe fallbacks
        leftMask: bloodFlow.leftMask
        rightMask: bloodFlow.rightMask
        
        durationSec: controlPanel.durationSec
        subjectId: subjectIdField.text
        dataDir: directoryField.text
        disableLaser: false                        // add a checkbox later 
        laserOn: true
        laserPower: 50
        triggerConfig: (typeof appTriggerConfig !== "undefined") ? appTriggerConfig : ({
            "TriggerFrequencyHz": 40,
            "TriggerPulseWidthUsec": 500,
            "LaserPulseDelayUsec": 100,
            "LaserPulseWidthUsec": 500,
            "LaserPulseSkipInterval": 600,
            "LaserPulseSkipDelayUsec": 1800,
            "EnableSyncOut": true,
            "EnableTaTrigger": true
        })


        onStageUpdate: function(txt) {
            if (!scanDialog.visible) scanDialog.open();
            scanDialog.stageText = txt;
        }
        onProgressUpdate: function(pct) {
            if (!scanDialog.visible) scanDialog.open();
            scanDialog.progress = pct;
        }
        onMessageOut: function(line) { 
            scanDialog.appendLog(line)
            console.log("Scan message: " + line)
        }
        onScanFinished: function(ok, err, left, right) {

            if (err === "Canceled") {
                // Cancel should CLOSE the dialog
                scanDialog.close()
                if (realtimePlotEnabled) {
                    meanPlotWindow.stopScan()
                }
                return
            }

            if (!ok) {
                scanDialog.appendLog("ERROR: " + err);
                scanDialog.stageText = "Error during capture";
                // keep it open so you can read the error
                scanDialog.done = true
                if (realtimePlotEnabled) {
                    meanPlotWindow.stopScan()
                }
                return;
            }

            // Success: keep open if you want, but make the button say Close
            scanDialog.stageText = "Capture complete"
            scanDialog.progress = 100
            scanDialog.done = true
            if (realtimePlotEnabled) {
                meanPlotWindow.stopScan()
            }
            // scanDialog.close();
        }
    }

    RealtimeMeanPlotWindow {
        id: meanPlotWindow
    }
}
