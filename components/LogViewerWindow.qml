import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import QtQuick.Window 6.0
import OpenMotion 1.0

/*
 * LogViewerWindow — live view of this launch's app log. Engineering-only
 * (the icon-bar Logs button that opens it is engineeringMode-gated); it
 * replaces the console=True second exe removed from the build (#57).
 *
 * A separate non-modal Window — not a ModalManager overlay — so it can
 * sit beside the main UI (or on another monitor) during a scan.
 *
 *  - Tails the per-launch log via MotionInterface.readAppLog() on a
 *    500 ms QTimer. Reads are incremental, bounded, and shared-read
 *    (see utils/log_tail.py), so the poll never blocks the GUI thread
 *    or interferes with the writer.
 *  - Search = case-insensitive substring FILTER: the list shows only
 *    lines containing the text; clearing it restores every line.
 *  - Auto-follow: sticks to the newest line while at the bottom;
 *    scrolling up pauses following, scrolling back down (or the
 *    "Resume follow" button) resumes it.
 *
 * The window retains the last `maxLines` raw lines (ring-trimmed) so a
 * long verbose session cannot grow memory unbounded; the ListView is
 * virtualized, so even a full buffer stays cheap. Content persists
 * across close/reopen — the tail just resumes where it left off.
 */
Window {
    id: logWin
    title: "Application Log" + (logPath !== "" ? " — " + logPath : "")
    width: 980
    height: 560
    minimumWidth: 560
    minimumHeight: 280
    flags: Qt.Window
    modality: Qt.NonModal
    color: AppTheme.bgBase

    property string logPath: ""
    property int maxLines: 5000

    // Retained raw lines (capped at maxLines); lineModel holds the
    // filtered view of them. _pending buffers a partial trailing line
    // between polls so lines are only ever appended whole. totalLines
    // mirrors _allLines.length as a notifying int — pushes into a
    // `var` array don't signal, so bindings can't read .length live.
    property var _allLines: []
    property int totalLines: 0
    property string _pending: ""
    property bool _follow: true
    property bool _autoScroll: false

    // The Logs button disappears when engineering mode is switched off;
    // an already-open viewer must go with it.
    readonly property bool engineeringUnlocked:
        MotionInterface.appConfig.engineeringMode === true
    onEngineeringUnlockedChanged: if (!engineeringUnlocked) logWin.close()

    function open() {
        logWin.show()
        logWin.raise()
        logWin.requestActivate()
    }

    function _matches(line) {
        var q = searchField.text
        return q === "" || line.toLowerCase().indexOf(q.toLowerCase()) !== -1
    }

    function _scrollToEnd() {
        // Guarded so the programmatic jump can't read back as a user
        // scroll and flip _follow in onContentYChanged.
        _autoScroll = true
        logList.positionViewAtEnd()
        _autoScroll = false
    }

    function _rebuildModel() {
        lineModel.clear()
        for (var i = 0; i < _allLines.length; i++)
            if (_matches(_allLines[i]))
                lineModel.append({ line: _allLines[i] })
        if (_follow) _scrollToEnd()
    }

    function _appendChunk(chunk) {
        if (chunk === "") return
        var text = _pending + chunk
        var parts = text.split("\n")
        _pending = parts.pop()  // "" when the chunk ended on a newline
        if (parts.length === 0) return
        for (var i = 0; i < parts.length; i++) {
            _allLines.push(parts[i])
            if (_matches(parts[i]))
                lineModel.append({ line: parts[i] })
        }
        // Ring-trim in batches (hysteresis avoids trimming every poll).
        // The model may still reference dropped lines, so rebuild it.
        if (_allLines.length > maxLines + 500) {
            _allLines.splice(0, _allLines.length - maxLines)
            _rebuildModel()
        } else if (_follow) {
            _scrollToEnd()
        }
        totalLines = _allLines.length
    }

    // Poll only while the window is visible. triggeredOnStart pulls the
    // initial tail the moment the window opens.
    Timer {
        interval: 500
        repeat: true
        running: logWin.visible
        triggeredOnStart: true
        onTriggered: {
            if (logWin.logPath === "")
                logWin.logPath = MotionInterface.appLogPath()
            logWin._appendChunk(MotionInterface.readAppLog())
        }
    }

    // Debounce filter rebuilds so fast typing doesn't rebuild the model
    // on every keystroke.
    Timer {
        id: filterDebounce
        interval: 200
        onTriggered: logWin._rebuildModel()
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        spacing: 8

        // Header — filter field, match count, follow state
        RowLayout {
            Layout.fillWidth: true
            spacing: 12

            TextField {
                id: searchField
                objectName: "logSearchField"
                Layout.fillWidth: true
                placeholderText: "Filter (case-insensitive substring)…"
                color: AppTheme.textPrimary
                onTextChanged: filterDebounce.restart()
            }

            Text {
                text: searchField.text === ""
                      ? lineModel.count + " lines"
                      : lineModel.count + " / " + logWin.totalLines + " lines"
                font.pixelSize: 12
                color: AppTheme.textTertiary
            }

            Button {
                text: "Resume follow"
                visible: !logWin._follow
                onClicked: {
                    logWin._follow = true
                    logWin._scrollToEnd()
                }
            }
        }

        // Log body
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            color: AppTheme.bgPlot
            radius: 6
            border.color: AppTheme.borderStrong
            border.width: 1

            ListView {
                id: logList
                anchors.fill: parent
                anchors.margins: 8
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                model: ListModel { id: lineModel; objectName: "logLineModel" }

                // User scrolls drive follow: away from the bottom pauses,
                // back at the bottom resumes. Programmatic follow jumps
                // are excluded via the _autoScroll guard.
                onContentYChanged: {
                    if (!logWin._autoScroll)
                        logWin._follow = logList.atYEnd
                }

                delegate: Text {
                    width: ListView.view ? ListView.view.width : 0
                    text: model.line
                    textFormat: Text.PlainText
                    font.family: "Consolas"
                    font.pixelSize: 12
                    color: AppTheme.textSecondary
                    wrapMode: Text.WrapAnywhere
                }

                ScrollBar.vertical: ScrollBar { }
            }

            Text {
                anchors.centerIn: parent
                visible: lineModel.count === 0
                text: logWin.logPath === ""
                      ? "No log file found."
                      : (searchField.text !== ""
                         ? "No lines match the filter."
                         : "Waiting for log output…")
                font.pixelSize: 13
                color: AppTheme.textTertiary
            }
        }
    }
}
