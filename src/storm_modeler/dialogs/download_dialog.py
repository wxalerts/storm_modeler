"""Download progress dialog — blocking, with a Cancel that keeps committed work.

A thin :class:`QProgressDialog` wrapper. The label shows the current site +
``k / N`` volumes; the bar advances per committed volume. Pressing Cancel sets
the worker's cancel event — the worker stops after the in-flight volume, and
every volume already committed remains (per-volume commit; no rollback).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QProgressDialog


class DownloadDialog(QProgressDialog):
    cancel_requested = Signal()

    def __init__(self, title: str, total: int = 0, parent=None) -> None:
        super().__init__(f"{title}", "Cancel", 0, max(total, 1), parent)
        self.setWindowTitle("Downloading")
        self.setWindowModality(Qt.WindowModal)
        self.setMinimumDuration(0)
        self.setAutoClose(True)
        self.setAutoReset(False)
        self._title = title
        self.canceled.connect(self.cancel_requested.emit)

    def update_progress(self, index: int, total: int, label: str) -> None:
        if total and total != self.maximum():
            self.setMaximum(total)
        self.setValue(index)
        self.setLabelText(f"{label}   {index}/{total}")

    def finish(self) -> None:
        self.setValue(self.maximum())
        self.close()
