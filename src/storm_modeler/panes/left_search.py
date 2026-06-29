"""Left-panel top: the search launcher + the persistent "Downloaded" list.

The IEM search form and its results no longer live here — they moved to a
floating :class:`~storm_modeler.dialogs.search_dialog.SearchDialog` opened by
the ``Search alerts…`` button. What remains in the sidebar is the list of
warnings already downloaded: selecting one re-shows its volumes (replayed from
the on-disk download cache, so the list survives app restarts).

This widget is a thin facade over the dialog: it re-emits the dialog's
``search_requested`` / ``download_requested`` / ``warning_selected`` signals and
forwards the few form/result methods the app drives directly, so the rest of the
app is unchanged by the move.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..dialogs.search_dialog import SearchDialog, SearchParams  # noqa: F401 (re-export)
from ..models import Warning
from ..settings.resolver import ResolvedSettings


def _z(dt: datetime) -> str:
    return dt.strftime("%H%M") + "Z"


class LeftSearchPane(QWidget):
    # Re-emitted from the floating search dialog so app wiring is unchanged.
    search_requested = Signal(object)      # SearchParams
    download_requested = Signal(object)    # Warning
    warning_selected = Signal(object)      # Warning
    downloaded_selected = Signal(object)   # Warning (a previously-downloaded one)

    def __init__(self, settings: ResolvedSettings | None = None, parent=None) -> None:
        super().__init__(parent)

        # The search form + results live in their own floating window.
        self.dialog = SearchDialog(settings, self)
        self.dialog.search_requested.connect(self.search_requested)
        self.dialog.download_requested.connect(self.download_requested)
        self.dialog.warning_selected.connect(self.warning_selected)

        root = QVBoxLayout(self)
        self.search_btn = QPushButton("Search alerts…")
        self.search_btn.clicked.connect(self.open_search)
        root.addWidget(self.search_btn)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        root.addWidget(line)

        # Warnings already downloaded — persisted to disk across launches.
        # Selecting one replays its volumes from the download cache.
        self.downloaded_label = QLabel("Downloaded")
        root.addWidget(self.downloaded_label)
        self.downloaded = QListWidget()
        self.downloaded.currentRowChanged.connect(self._on_downloaded_row)
        root.addWidget(self.downloaded, 1)
        self._downloaded_warnings: list[Warning] = []

    # --- search window ----------------------------------------------------

    def open_search(self) -> None:
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    # Forwards so the app can drive the search form/results unchanged.
    @property
    def results(self) -> QListWidget:
        return self.dialog.results

    @property
    def start_edit(self):
        return self.dialog.start_edit

    @property
    def end_edit(self):
        return self.dialog.end_edit

    def _emit_search(self) -> None:
        self.dialog._emit_search()

    def clear_results(self) -> None:
        self.dialog.clear_results()

    def set_searching(self, on: bool) -> None:
        self.dialog.set_searching(on)

    def add_result(self, warning: Warning) -> None:
        self.dialog.add_result(warning)

    # --- downloaded (persistent) list ------------------------------------

    def add_downloaded(self, warning: Warning) -> None:
        """Record a downloaded warning in the list (idempotent by id)."""
        if any(w.id == warning.id for w in self._downloaded_warnings):
            return
        self._downloaded_warnings.append(warning)
        states = ",".join(warning.states)
        text = (
            f"{warning.event} · {warning.wfo} · ETN {warning.etn:04d} · "
            f"{_z(warning.issued)}–{_z(warning.expires)}"
            + (f" · {states}" if states else "")
        )
        self.downloaded.addItem(QListWidgetItem(text))

    def _on_downloaded_row(self, row: int) -> None:
        if 0 <= row < len(self._downloaded_warnings):
            self.downloaded_selected.emit(self._downloaded_warnings[row])
