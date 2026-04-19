from PyQt6.QtCore import (
    QByteArray, QEasingCurve, QObject, QPropertyAnimation, QRectF, Qt,
    pyqtSlot,
)
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QApplication, QWidget

from signals import DictationSignals


# ---------------------------------------------------------------------------
# Inline SVG mic icons (single-line path data to avoid parser edge cases)
# ---------------------------------------------------------------------------

_SVG_MIC_LISTENING = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
  <path fill="#4CAF50" d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z"/>
  <path fill="#4CAF50" d="M17 11c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z"/>
</svg>"""

_SVG_MIC_PROCESSING = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
  <path fill="#FFC107" d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z"/>
  <path fill="#FFC107" d="M17 11c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z"/>
</svg>"""

_OVERLAY_W = 320
_OVERLAY_H = 288
_ICON_SIZE  = 160   # mic icon, fills top two-thirds
_TEXT_H     = 96    # bottom third reserved for label


class DictationOverlay(QWidget):
    def __init__(
        self,
        signals: DictationSignals,
        overlay_config: dict,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._config = overlay_config
        self._state = 'idle'
        self._label = ''
        self._target_opacity = float(overlay_config.get('opacity', 0.70))
        self._fade_duration = int(overlay_config.get('fade_duration_ms', 1500))
        self._position = overlay_config.get('position', 'top-third')
        self._offset = overlay_config.get('offset_px', [0, 0])

        self._renderer = QSvgRenderer(QByteArray(_SVG_MIC_LISTENING))

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(_OVERLAY_W, _OVERLAY_H)

        self._anim = QPropertyAnimation(self, b"windowOpacity")
        self._anim.setDuration(self._fade_duration)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._on_fade_finished)

        signals.state_changed.connect(self.on_state_changed)

    def _reposition(self):
        screen = QApplication.primaryScreen().availableGeometry()
        ox, oy = self._offset[0], self._offset[1]
        w, h = _OVERLAY_W, _OVERLAY_H
        positions = {
            'top-center':   ((screen.width() - w) // 2 + ox, oy),
            'top-third':    ((screen.width() - w) // 2 + ox, screen.height() // 3 - h // 2 + oy),
            'top-left':     (ox, oy),
            'top-right':    (screen.width() - w - ox, oy),
            'bottom-left':  (ox, screen.height() - h - oy),
            'bottom-right': (screen.width() - w - ox, screen.height() - h - oy),
        }
        x, y = positions.get(self._position, positions['top-center'])
        self.move(x, y)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Rounded-rect background
        painter.setBrush(QColor(20, 20, 20, 210))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(self.rect(), 24, 24)

        # Mic icon — centered horizontally, filling the top two-thirds
        icon_top = (_OVERLAY_H - _TEXT_H - _ICON_SIZE) // 2  # vertically center within top zone
        icon_x = (_OVERLAY_W - _ICON_SIZE) / 2
        icon_rect = QRectF(icon_x, icon_top, _ICON_SIZE, _ICON_SIZE)
        self._renderer.render(painter, icon_rect)

        # Label — large, centered in the bottom third
        painter.setPen(QColor(255, 255, 255, 240))
        font = QFont("Segoe UI", 22, QFont.Weight.DemiBold)
        painter.setFont(font)
        text_rect = self.rect().adjusted(8, _OVERLAY_H - _TEXT_H, -8, -8)
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            self._label,
        )

    @pyqtSlot(str)
    def on_state_changed(self, state: str):
        self._state = state
        if state == 'listening':
            self._anim.stop()
            self._label = 'Listening'
            self._renderer = QSvgRenderer(QByteArray(_SVG_MIC_LISTENING))
            self.setWindowOpacity(self._target_opacity)
            self._reposition()
            self.show()
            self.update()
        elif state == 'processing':
            self._label = 'Processing'
            self._renderer = QSvgRenderer(QByteArray(_SVG_MIC_PROCESSING))
            if not self.isVisible():
                self.setWindowOpacity(self._target_opacity)
                self._reposition()
                self.show()
            else:
                self._anim.stop()
                self.setWindowOpacity(self._target_opacity)
            self.update()
        elif state == 'finishing':
            self._label = 'Finishing'
            self._renderer = QSvgRenderer(QByteArray(_SVG_MIC_PROCESSING))
            if not self.isVisible():
                self.setWindowOpacity(self._target_opacity)
                self._reposition()
                self.show()
            else:
                self._anim.stop()
                self.setWindowOpacity(self._target_opacity)
            self.update()
        else:  # 'idle'
            self._start_fade()

    def _start_fade(self):
        self._anim.stop()
        if not self.isVisible():
            return
        self._anim.setStartValue(self.windowOpacity())
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _on_fade_finished(self):
        if self._state == 'idle':
            self.hide()
            self.setWindowOpacity(self._target_opacity)
