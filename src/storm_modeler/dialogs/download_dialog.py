"""Download progress dialog — blocking, with a Cancel that keeps committed work.

A thin :class:`QProgressDialog` wrapper. The first label line shows the current
site + ``k / N`` volumes (the bar advances per committed volume); the second
line shows the live sub-step the worker is on — listing the catalog, the bytes
of the in-flight download, or the gridding phase — so the dialog visibly moves
while a single volume is being fetched. Pressing Cancel sets the worker's
cancel event — the worker stops after the in-flight volume, and every volume
already committed remains (per-volume commit; no rollback).
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
        self._progress_text = title
        self._status_text = "Preparing…"
        self._render()
        self.canceled.connect(self.cancel_requested.emit)

    def _render(self) -> None:
        if self._status_text:
            self.setLabelText(f"{self._progress_text}\n{self._status_text}")
        else:
            self.setLabelText(self._progress_text)

    def update_progress(self, index: int, total: int, label: str) -> None:
        if total and total != self.maximum():
            self.setMaximum(total)
        self.setValue(index)
        self._progress_text = f"{label}   {index}/{total}"
        self._render()

    def set_status(self, message: str) -> None:
        """Update the live sub-step line (download bytes, gridding phase, …)."""
        self._status_text = message
        self._render()

    def finish(self) -> None:
        self.setValue(self.maximum())
        self.close()
