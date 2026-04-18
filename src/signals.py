from PyQt6.QtCore import QObject, pyqtSignal


class DictationSignals(QObject):
    """Thread-safe state bridge between DictationSystem and Qt UI components.

    Lives on the main thread. Qt automatically delivers cross-thread signal
    emissions via QueuedConnection, so DictationSystem can call .emit() from
    any thread (hotkey callback thread, OutputThread, etc.) without locks.

    State values: 'listening', 'processing', 'idle'
    """

    state_changed = pyqtSignal(str)
