"""Quiet, native Qt chat widgets for the desktop application."""

import re
import time

from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget,
)


# Convert only explicit chat UI colors; preserve font sizes, geometry and behavior.
_DARK_COLORS = {
    "white": "#212121", "#ffffff": "#212121", "#252525": "#ececec",
    "#f8f8f8": "#171717", "#eee": "#383838", "#777": "#aaa",
    "#333": "#e5e5e5", "#eaeaea": "#303030", "#444": "#ddd",
    "#555": "#bbb", "#ddd": "#444", "#f3f3f3": "#303030",
    "#f4f4f4": "#303030", "#e8e8e8": "#3d3d3d", "#888": "#aaa",
    "#dce8df": "#496353", "#426350": "#92b99d",
    "#e8eee9": "#32443a",
}


def theme_widget(widget, dark):
    """Retain a canonical light stylesheet so repeated toggles never drift."""
    current = widget.styleSheet()
    previous = getattr(widget, "_chat_themed_style", None)
    if previous is None or current != previous:
        widget._chat_light_style = current
    source = widget._chat_light_style
    result = re.sub(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b|\bwhite\b",
                    lambda m: _DARK_COLORS.get(m[0].lower(), m[0]), source) if dark else source
    widget.setStyleSheet(result)
    widget._chat_themed_style = result


def theme_tree(widget, dark):
    theme_widget(widget, dark)
    for child in widget.findChildren(QWidget):
        theme_widget(child, dark)


class AssistantAvatar(QWidget):
    def __init__(self, size=64, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setAccessibleName("Assistant avatar")

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(self.width() / 64, self.height() / 64)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#e8eee9"))
        p.drawRoundedRect(QRectF(0, 0, 64, 64), 22, 22)
        p.setBrush(QColor("#426350"))
        p.drawRoundedRect(QRectF(15, 22, 34, 28), 12, 12)
        p.drawEllipse(QRectF(29, 10, 6, 6))
        p.setPen(QPen(QColor("#426350"), 3, Qt.PenStyle.SolidLine,
                     Qt.PenCapStyle.RoundCap))
        p.drawLine(32, 14, 32, 22)
        p.drawLine(11, 32, 11, 40)
        p.drawLine(53, 32, 53, 40)
        p.setPen(QPen(QColor("#ffffff"), 3, Qt.PenStyle.SolidLine,
                     Qt.PenCapStyle.RoundCap))
        p.drawLine(25, 32, 25, 36)
        p.drawLine(39, 32, 39, 36)
        smile = QPainterPath()
        smile.moveTo(27, 42)
        smile.quadTo(32, 46, 37, 42)
        p.drawPath(smile)


class ChatTranscript(QScrollArea):
    """Selectable conversation cards; diagnostic messages stay in the activity log."""

    _message = pyqtSignal(str)

    def __init__(self, assistant_name, activity, parent=None):
        super().__init__(parent)
        self._ai_name_lc = assistant_name.lower()
        self._activity = activity
        self._dark = False
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("QScrollArea { background: white; border: none; }"
                           "QScrollBar:vertical { width: 8px; background: white; }"
                           "QScrollBar::handle:vertical { background: #ddd; min-height: 24px; border-radius: 4px; }"
                           "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }")
        self._container = QWidget()
        self._container.setStyleSheet("background: white;")
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(28, 20, 28, 20)
        self._layout.setSpacing(24)
        self._layout.addStretch()
        self.setWidget(self._container)
        self._welcome = QWidget()
        welcome = QVBoxLayout(self._welcome)
        welcome.setSpacing(16)
        welcome.addStretch()
        welcome.addWidget(AssistantAvatar(76), alignment=Qt.AlignmentFlag.AlignHCenter)
        title = QLabel("What can I help you with?")
        title.setFont(QFont("Segoe UI", 23, QFont.Weight.DemiBold))
        title.setStyleSheet("color: #252525;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setWordWrap(True)
        welcome.addWidget(title)
        hint = QLabel("A question, a fresh idea, or a little help getting started.")
        hint.setFont(QFont("Segoe UI", 10))
        hint.setStyleSheet("color: #777;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        welcome.addWidget(hint)
        welcome.addStretch()
        self._layout.insertWidget(0, self._welcome, 1)
        self._message.connect(self._append)
        self._follow_bottom = True
        self.verticalScrollBar().rangeChanged.connect(self._scroll_if_following)

    def append_log(self, text):
        self._message.emit(text)

    def set_dark(self, dark):
        self._dark = dark
        theme_tree(self, dark)

    def _scroll_if_following(self, minimum, maximum):
        if self._follow_bottom:
            self.verticalScrollBar().setValue(maximum)

    def _append(self, text):
        prefix, sep, body = text.partition(":")
        role = prefix.strip().lower()
        if not sep or role not in ("you", "jarvis", self._ai_name_lc, "file", "err"):
            self._activity.append_log(text)
            return
        bar = self.verticalScrollBar()
        self._follow_bottom = bar.value() >= bar.maximum() - 30
        self._welcome.hide()
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        user = role == "you"
        if user:
            layout.addStretch(1)
        elif role in ("jarvis", self._ai_name_lc):
            layout.addWidget(AssistantAvatar(32), alignment=Qt.AlignmentFlag.AlignTop)
        label = QLabel(body.strip())
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setFont(QFont("Segoe UI", 11))
        label.setAccessibleName("You" if user else prefix)
        label.setStyleSheet(
            "color: #252525; padding: 12px 16px; background: #f3f3f3; border-radius: 16px;"
            if user else "color: #252525; padding: 4px 0; background: transparent;"
        )
        layout.addWidget(label, 4)
        self._layout.insertWidget(self._layout.count() - 1, row)
        theme_tree(row, self._dark)
        # Bound the number of visible cards for long voice sessions.
        if self._layout.count() > 202:
            item = self._layout.takeAt(1)
            item.widget().deleteLater()


class ChatSurface(QWidget):
    """Keeps the existing engine's visual state interface without a HUD animation."""

    file_dropped = pyqtSignal(str)

    def __init__(self, transcript, assistant_name):
        super().__init__()
        self._assistant_name = assistant_name
        self.muted = False
        self.speaking = False
        self.state = "INITIALISING"
        self._audio_sample = (0.0, 0.0)
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(transcript)

    def dragEnterEvent(self, event):
        from pathlib import Path
        if any(url.isLocalFile() and Path(url.toLocalFile()).is_file()
               for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        from pathlib import Path
        for url in event.mimeData().urls():
            if url.isLocalFile() and Path(url.toLocalFile()).is_file():
                self.file_dropped.emit(url.toLocalFile())
                event.acceptProposedAction()
                break

    def set_audio_level(self, level):
        # Called on an audio worker: publish data only, never touch Qt widgets.
        if self.state != "LISTENING" or self.muted:
            return
        try:
            self._audio_sample = (max(0.0, min(1.0, float(level))), time.monotonic())
        except (TypeError, ValueError):
            pass

    def input_level(self):
        level, captured = self._audio_sample
        if self.muted or self.state != "LISTENING" or time.monotonic() - captured > 0.35:
            return 0.0
        return level
