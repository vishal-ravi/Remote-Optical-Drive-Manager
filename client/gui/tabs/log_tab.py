"""Live operation log pane."""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class QtLogHandler(logging.Handler, QObject):
    """Routes stdlib log records into a Qt signal (safe from any thread)."""

    recorded = Signal(str, str)  # levelname, formatted message

    def __init__(self, level: int = logging.INFO) -> None:
        logging.Handler.__init__(self, level=level)
        QObject.__init__(self)
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            message = self.format(record)
        except Exception:  # pragma: no cover
            return
        self.recorded.emit(record.levelname, message)


LEVEL_COLORS = {
    "DEBUG": "#8a8a8a",
    "INFO": "#d8d8d8",
    "WARNING": "#e0b040",
    "ERROR": "#e06c5a",
    "CRITICAL": "#ff4d4d",
}


class LogTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._autoscroll = True
        self._limit = 5000

        self.view = QTextEdit(self)
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QTextEdit.NoWrap)
        font = QFont("Monospace")
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(9)
        self.view.setFont(font)

        self.autoscroll_box = QCheckBox("Follow output", self)
        self.autoscroll_box.setChecked(True)
        self.autoscroll_box.toggled.connect(self._toggle_autoscroll)

        self.clear_button = QPushButton("Clear", self)
        self.clear_button.clicked.connect(self.view.clear)

        top = QHBoxLayout()
        top.addWidget(self.autoscroll_box)
        top.addStretch(1)
        top.addWidget(self.clear_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addLayout(top)
        layout.addWidget(self.view, 1)

    # ------------------------------------------------------------------ #

    def _toggle_autoscroll(self, checked: bool) -> None:
        self._autoscroll = checked

    def append(self, level: str, message: str) -> None:
        color = LEVEL_COLORS.get(level.upper(), "#d8d8d8")
        self.view.append(f'<span style="color:{color}">{_escape(message)}</span>')
        # trim to keep memory bounded
        block_count = self.view.document().blockCount()
        if block_count > self._limit:
            cursor = self.view.textCursor()
            cursor.movePosition(QTextCursor.Start)
            cursor.movePosition(
                QTextCursor.Down, QTextCursor.KeepAnchor, block_count - self._limit
            )
            cursor.removeSelectedText()
        if self._autoscroll:
            self.view.verticalScrollBar().setValue(
                self.view.verticalScrollBar().maximum()
            )

    def clear(self) -> None:
        self.view.clear()


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
