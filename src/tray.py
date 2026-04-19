import os
import sys
from pathlib import Path

from PyQt6.QtCore import QByteArray, QObject, Qt, pyqtSlot
from PyQt6.QtGui import QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from signals import DictationSignals


# ---------------------------------------------------------------------------
# Inline SVG mic icons — no external files
# ---------------------------------------------------------------------------

_SVG_MIC_IDLE = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
  <path fill="#888888" d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z"/>
  <path fill="#888888" d="M17 11c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z"/>
</svg>"""

_SVG_MIC_ACTIVE = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
  <path fill="#4CAF50" d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z"/>
  <path fill="#4CAF50" d="M17 11c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z"/>
</svg>"""

_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_REG_NAME = "LocalSpeechDictation"


def _svg_to_icon(svg_bytes: bytes) -> QIcon:
    renderer = QSvgRenderer(QByteArray(svg_bytes))
    pixmap = QPixmap(22, 22)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)


class DictationTrayIcon(QSystemTrayIcon):
    def __init__(
        self,
        system,
        signals: DictationSignals,
        config_path: Path,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._system = system
        self._config_path = config_path

        self._icon_idle = _svg_to_icon(_SVG_MIC_IDLE)
        self._icon_active = _svg_to_icon(_SVG_MIC_ACTIVE)

        self.setIcon(self._icon_idle)
        self.setToolTip("Dictation — Ready")

        self._toggle_action = None
        self.setContextMenu(self._build_menu())

        self.activated.connect(self._on_activated)
        signals.state_changed.connect(self._on_state_changed)

    def _build_menu(self) -> QMenu:
        from PyQt6.QtGui import QAction
        menu = QMenu()

        self._toggle_action = menu.addAction("Start Listening")
        self._toggle_action.triggered.connect(self._on_toggle)

        menu.addSeparator()

        open_settings = menu.addAction("Open Settings")
        open_settings.triggered.connect(self._on_open_settings)

        run_at_login = menu.addAction("Run at Login")
        run_at_login.setCheckable(True)
        run_at_login.setChecked(self._read_run_at_login())
        run_at_login.triggered.connect(self._on_run_at_login_toggled)

        menu.addSeparator()

        exit_action = menu.addAction("Exit")
        exit_action.triggered.connect(QApplication.instance().quit)

        return menu

    @pyqtSlot(str)
    def _on_state_changed(self, state: str):
        if state == 'listening':
            self.setIcon(self._icon_active)
            self.setToolTip("Dictation — Listening")
            if self._toggle_action:
                self._toggle_action.setText("Stop Listening")
        elif state == 'processing':
            self.setIcon(self._icon_active)
            self.setToolTip("Dictation — Processing")
            if self._toggle_action:
                self._toggle_action.setText("Start Listening")
        else:  # 'idle' or 'finishing'
            self.setIcon(self._icon_idle)
            self.setToolTip("Dictation — Ready")
            if self._toggle_action:
                self._toggle_action.setText("Start Listening")

    @pyqtSlot(QSystemTrayIcon.ActivationReason)
    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._system.toggle_listening()

    @pyqtSlot()
    def _on_toggle(self):
        self._system.toggle_listening()

    @pyqtSlot()
    def _on_open_settings(self):
        os.startfile(str(self._config_path))

    @pyqtSlot(bool)
    def _on_run_at_login_toggled(self, checked: bool):
        self._write_run_at_login(checked)

    def _read_run_at_login(self) -> bool:
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY)
            winreg.QueryValueEx(key, _REG_NAME)
            winreg.CloseKey(key)
            return True
        except (FileNotFoundError, OSError):
            return False

    def _write_run_at_login(self, enabled: bool):
        import winreg
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, _REG_KEY,
                access=winreg.KEY_SET_VALUE,
            )
            if enabled:
                # Use pythonw.exe (no console window) from the current interpreter's dir
                pythonw = str(Path(sys.executable).parent / 'pythonw.exe')
                script = str(Path(__file__).parent / 'main.py')
                winreg.SetValueEx(key, _REG_NAME, 0, winreg.REG_SZ, f'"{pythonw}" "{script}"')
            else:
                try:
                    winreg.DeleteValue(key, _REG_NAME)
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        except OSError as e:
            import logging
            logging.warning(f"Failed to update Run at Login registry entry: {e}")
