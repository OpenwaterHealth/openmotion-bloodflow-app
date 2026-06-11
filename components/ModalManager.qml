import QtQuick 6.0

/*  ModalManager — single source of truth for which modal is on screen.
 *
 *  Wired up once in BloodFlow.qml with the list of every modal Item:
 *
 *      ModalManager {
 *          id: modalManager
 *          modals: [settingsModal, notesModal, historyModal,
 *                   scanSettingsModal, contactQualityModal]
 *      }
 *
 *  Each modal is expected to expose ``visible`` and ``function close()``,
 *  where ``close()`` is the "save + hide" operation by convention.
 *
 *  A modal may opt out of being dismissed by the manager by exposing a
 *  ``dismissable`` boolean property and setting it to ``false``. Modals
 *  that don't declare ``dismissable`` are treated as dismissable.
 *
 *  Public API:
 *      property var current
 *          The currently-visible modal Item, or null.
 *      function closeCurrent()
 *          Close (= save + hide) the current modal if it is dismissable.
 *      function toggle(target)
 *          If target is already open: close it.
 *          Else: close whatever is currently open (if dismissable), then
 *          open target. If the current modal refuses to close, the
 *          requested switch is a no-op.
 */
QtObject {
    id: mgr

    // Assigned by parent: the full set of modals this manager governs.
    property var modals: []

    // Re-evaluated whenever any modal's `visible` changes (because each
    // entry in the list is a QObject and `visible` is a Q_PROPERTY,
    // QML's binding tracker subscribes automatically).
    readonly property var current: {
        for (var i = 0; i < modals.length; i++) {
            var m = modals[i]
            if (m && m.visible) return m
        }
        return null
    }

    function _isDismissable(m) {
        // Modals that don't declare `dismissable` are dismissable.
        return m && (m.dismissable === undefined || m.dismissable)
    }

    function closeCurrent() {
        var c = current
        if (c && _isDismissable(c)) c.close()
    }

    function toggle(target) {
        var c = current
        if (c) {
            if (!_isDismissable(c)) return
            c.close()
        }
        if (c !== target && target) target.open()
    }
}
