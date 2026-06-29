"""Logs window — a live tail of the run's log file.

structlog already tees every line to a timestamped file (see logging_setup); the
simplest robust way to show *all* logs in the GUI is to tail that file. A timer
reads whatever has been appended and feeds it to a read-only text view, so this
window mirrors exactly what goes to stdout/the file — including worker threads
and the map subprocess.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import QMainWindow, QPlainTextEdit


class LogsWindow(QMainWindow):
    def __init__(self, log_path: Path | None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Logs")
        self.resize(900, 400)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(5000)  # bound memory on long runs
        self.view.setFont(QFont("monospace", 9))
        self.setCentralWidget(self.view)

        self._path = Path(log_path) if log_path else None
        self._pos = 0
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        self._poll()

    def _poll(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self._pos)
                chunk = fh.read()
                self._pos = fh.tell()
        except OSError:
            return
        if not chunk:
            return
        at_bottom = (
            self.view.verticalScrollBar().value()
            >= self.view.verticalScrollBar().maximum() - 4
        )
        self.view.appendPlainText(chunk.rstrip("\n"))
        if at_bottom:  # follow the tail unless the user scrolled up
            self.view.moveCursor(QTextCursor.End)
