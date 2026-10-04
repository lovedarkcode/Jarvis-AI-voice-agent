"""Shared presentation for desktop controls, including legacy settings panels.

The style filter changes appearance only: callbacks, values, focus and audio
state remain owned by the existing widgets.
"""
import re
from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFrame, QLabel, QLineEdit, QPushButton,
    QScrollArea, QTextEdit, QWidget,
)


def action_icon(kind, dark=False):
    pix = QPixmap(24, 24)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor('#b6cebf' if dark else '#426350'), 1.7,
                 Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                 Qt.PenJoinStyle.RoundJoin))
    if kind == 'audio':
        p.drawArc(QRectF(4, 4, 16, 16), 0, 180 * 16)
        p.drawRoundedRect(QRectF(3, 12, 4, 8), 2, 2)
        p.drawRoundedRect(QRectF(17, 12, 4, 8), 2, 2)
    elif kind == 'theme':
        path = QPainterPath()
        path.moveTo(17, 4)
        path.cubicTo(5, -1, 0, 16, 12, 21)
        path.cubicTo(17, 23, 22, 17, 21, 13)
        path.cubicTo(14, 17, 10, 8, 17, 4)
        p.drawPath(path)
    elif kind == 'activity':
        path = QPainterPath()
        path.moveTo(2, 12)
        for x, y in ((6, 12), (9, 5), (14, 20), (17, 12), (22, 12)):
            path.lineTo(x, y)
        p.drawPath(path)
    else:
        for y, x in ((6, 9), (12, 16), (18, 7)):
            p.drawLine(3, y, x - 3, y)
            p.drawLine(x + 3, y, 21, y)
            p.drawEllipse(QRectF(x - 2.5, y - 2.5, 5, 5))
    p.end()
    return QIcon(pix)


def style_action(button, kind, dark=False):
    button.setProperty('chatIcon', kind)
    button.setIcon(action_icon(kind, dark))
    button.setIconSize(QSize(21, 21))
    button.setFixedHeight(44)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setStyleSheet(
        "QPushButton { background: #f3f3f3; color: #333; border: 1px solid #ddd;"
        " border-radius: 12px; padding: 8px 12px; text-align: left; font: 10pt 'Segoe UI'; }"
        "QPushButton:hover { background: #e8eee9; border-color: #426350; }"
        "QPushButton:checked { background: #e8eee9; border-color: #426350; }"
        "QPushButton:pressed { background: #dce8df; }"
        "QPushButton:focus { border-color: #426350; }"
    )


class DesktopStyle(QObject):
    ROOTS = {
        'AudioDeviceOverlay', 'SetupOverlay', 'CustomizeOverlay',
        'PluginManagerOverlay', 'ConfirmBanner', 'MemoryOverlay',
        'PluginSettingsOverlay', 'RemoteKeyOverlay', 'ClipboardPanel', '_CameraPreview',
    }
    TITLES = {
        'QuickDrawer': 'Settings', 'AudioDeviceOverlay': 'Audio settings',
        'CustomizeOverlay': 'Customize assistant', 'SetupOverlay': 'Connect your assistant',
        'PluginManagerOverlay': 'Plugins', 'PluginSettingsOverlay': 'Plugin settings',
        'MemoryOverlay': 'Memory', 'RemoteKeyOverlay': 'Connect another device',
        'ConfirmBanner': 'Confirm action', 'ClipboardPanel': 'Clipboard',
    }
    MODALS = ROOTS - {'ClipboardPanel', '_CameraPreview'}

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.busy = False
        self.shade = QWidget(window.centralWidget())
        self.shade.setObjectName('DialogBackdrop')
        self.shade.setStyleSheet('QWidget#DialogBackdrop { background: rgba(0, 0, 0, 80); border: none; }')
        self.shade.hide()
        QApplication.instance().installEventFilter(self)

    def root_for(self, widget):
        node = widget
        while isinstance(node, QWidget) and node is not self.window:
            if type(node).__name__ in self.ROOTS or node.objectName() in ('QuickDrawer', 'ContentPanel'):
                return node if self.window.isAncestorOf(node) else None
            node = node.parentWidget()
        return None

    def eventFilter(self, obj, event):
        if obj is self.window.centralWidget() and event.type() == QEvent.Type.Resize:
            self.shade.setGeometry(obj.rect())
        if isinstance(obj, QWidget) and type(obj).__name__ in self.MODALS and self.window.isAncestorOf(obj):
            if event.type() == QEvent.Type.Show:
                self.shade.setGeometry(self.window.centralWidget().rect())
                self.shade.show()
                self.shade.raise_()
                obj.raise_()
            elif event.type() == QEvent.Type.Hide:
                visible = [w for w in self.window.findChildren(QWidget)
                           if w is not obj and type(w).__name__ in self.MODALS and w.isVisible()]
                if visible:
                    visible[-1].raise_()
                else:
                    self.shade.hide()
        if not self.busy and isinstance(obj, QWidget) and event.type() in (QEvent.Type.Show, QEvent.Type.StyleChange):
            root = self.root_for(obj)
            if root is not None and (event.type() == QEvent.Type.Show or root.isVisible()):
                self.polish(root)
                if obj is root and event.type() == QEvent.Type.Show:
                    if root.objectName() not in ('QuickDrawer', 'ContentPanel'):
                        drawer = getattr(self.window, '_quick_drawer', None)
                        if drawer:
                            drawer.hide()
                            self.window._drawer_btn.setChecked(False)
                    if type(root).__name__ in ('AudioDeviceOverlay', 'ConfirmBanner', 'PluginManagerOverlay'):
                        root.adjustSize()
                        area = self.window.centralWidget()
                        root.move(max(8, (area.width() - root.width()) // 2),
                                  max(8, (area.height() - root.height()) // 2))
        return False

    def refresh(self):
        for child in self.window.findChildren(QWidget):
            if type(child).__name__ in self.ROOTS or child.objectName() in ('QuickDrawer', 'ContentPanel'):
                self.polish(child)

    def polish(self, root):
        if self.busy:
            return
        self.busy = True
        try:
            dark = self.window._dark_mode
            bg, field, text, muted, border, hover = (
                ('#262626', '#303030', '#ededed', '#b5b5b5', '#454545', '#3b3b3b') if dark else
                ('#ffffff', '#f6f7f6', '#252525', '#666666', '#dedede', '#edf2ee')
            )
            accent = '#a9c9b4' if dark else '#426350'
            arrow = (Path(__file__).resolve().parent / 'assets' / 'chevron-down.svg').as_posix()
            selector = type(root).__name__
            if root.objectName():
                selector = 'QWidget#' + root.objectName()
            sheet = f'{selector} {{ background: {bg}; color: {text}; border: 1px solid {border}; border-radius: 16px; }}'
            self._set_sheet(root, sheet)
            root.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            labels = root.findChildren(QLabel)
            heading = self.TITLES.get(root.objectName() or type(root).__name__)
            if heading and labels:
                labels[0].setText(heading)
                labels[0].setFont(QFont('Segoe UI', 14, QFont.Weight.DemiBold))
            for widget in root.findChildren(QWidget):
                if isinstance(widget, QPushButton):
                    self._button(widget, root, text, field, border, hover, accent)
                elif isinstance(widget, QLabel):
                    font = widget.font()
                    size = 14 if font.pointSize() >= 11 and font.bold() else 9
                    new_font = QFont('Segoe UI', size, QFont.Weight.DemiBold if font.bold() else QFont.Weight.Normal)
                    if font != new_font:
                        widget.setFont(new_font)
                    self._set_sheet(widget, f'color: {text if font.bold() else muted}; background: transparent; border: none;')
                elif isinstance(widget, (QLineEdit, QTextEdit, QComboBox)):
                    self._set_sheet(widget, f'''
                        {type(widget).__name__} {{ background: {field}; color: {text}; border: 1px solid {border};
                            border-radius: 8px; padding: 4px 8px; font: 10pt 'Segoe UI'; selection-background-color: {accent}; }}
                        {type(widget).__name__}:focus {{ border-color: {accent}; }}
                        QComboBox::drop-down {{ border: none; width: 24px; }}
                        QComboBox::down-arrow {{ image: url("{arrow}"); width: 16px; height: 16px; }}
                        QComboBox QAbstractItemView {{ background: {field}; color: {text}; selection-background-color: {hover}; border: 1px solid {border}; }}
                    ''')
                elif isinstance(widget, QFrame) and widget.frameShape() == QFrame.Shape.HLine:
                    self._set_sheet(widget, f'color: {border}; background: {border}; border: none; max-height: 1px;')
                elif type(widget) in (QWidget, QFrame, QScrollArea):
                    self._set_sheet(widget, f'background: transparent; color: {text}; border: none;')
        finally:
            self.busy = False

    @staticmethod
    def _set_sheet(widget, sheet):
        if widget.styleSheet() != sheet:
            widget.setStyleSheet(sheet)

    def _button(self, button, root, text, field, border, hover, accent):
        raw = button.text()
        # Only presentation labels are changed; button signals and stored data are untouched.
        clean = re.sub(r'^[^\w]+', '', raw).strip()
        if clean and clean.isupper():
            clean = clean.capitalize()
        if clean and clean != raw:
            button.setText(clean)
        primary = clean.lower() in ('apply', 'apply changes', 'save', 'save all', 'confirm', 'recheck')
        destructive = clean.lower() in ('confirm', 'forget', 'delete', 'clear all')
        fill = ('#8c4141' if destructive else '#426350') if primary else field
        foreground = '#ffffff' if primary else text
        align = 'left' if root.objectName() == 'QuickDrawer' else 'center'
        self._set_sheet(button, f'''
            QPushButton {{ color: {foreground}; background: {fill}; border: 1px solid {border};
                border-radius: 8px; padding: 2px 8px; text-align: {align}; font: 9pt 'Segoe UI'; }}
            QPushButton:hover {{ border-color: {accent}; background: {'#53755f' if primary else hover}; }}
            QPushButton:checked {{ border-color: {accent}; background: {hover}; color: {text}; }}
            QPushButton:focus {{ border: 2px solid {accent}; }}
            QPushButton:disabled {{ color: #888888; background: {field}; }}
        ''')
        if root.objectName() == 'QuickDrawer':
            button.setFixedHeight(34)
            button.setIcon(action_icon('audio' if 'audio' in clean.lower() else 'settings', self.window._dark_mode))
            button.setIconSize(QSize(17, 17))
