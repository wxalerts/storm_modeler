"""Left-panel bottom: volumes + detections for the selected warning.

Selecting a result in the search pane populates this with that warning's
volumes and the storm cells SCIT found in each::

    Volume [1142Z]
      └ Storm [id 7  57.5 dBZ  depth 9.2 km]   ← clickable → map recenter/highlight
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QLabel, QTreeView, QVBoxLayout, QWidget

from ..detection.detection_v2 import StormCell
from ..pipeline import VolumeResult

CELL_ROLE = Qt.UserRole + 1


def _z(dt: datetime) -> str:
    return dt.strftime("%H%M") + "Z"


class LeftVolumesPane(QWidget):
    storm_selected = Signal(object)  # StormCell

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.header = QLabel("Volumes")
        layout.addWidget(self.header)
        self.tree = QTreeView()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.model = QStandardItemModel(self)
        self.tree.setModel(self.model)
        self.tree.clicked.connect(self._on_clicked)
        layout.addWidget(self.tree, 1)

        self._warning_id: str | None = None
        self._volumes: dict[str, QStandardItem] = {}

    def set_warning(self, warning) -> None:
        self._warning_id = warning.id
        self.header.setText(
            f"Volumes — {warning.event}  {_z(warning.issued)}–{_z(warning.expires)}"
        )
        self.clear()

    def clear(self) -> None:
        self.model.clear()
        self._volumes.clear()

    def _volume_item(self, res: VolumeResult) -> QStandardItem:
        key = res.volume.valid_time.isoformat()
        if key not in self._volumes:
            it = QStandardItem(f"Volume  [{_z(res.volume.valid_time)}]")
            it.setEditable(False)
            self.model.appendRow(it)
            self._volumes[key] = it
        return self._volumes[key]

    def add_result(self, res: VolumeResult) -> None:
        """Append a processed volume and its storms (idempotent per volume)."""
        if self._warning_id is not None and res.warning.id != self._warning_id:
            return
        vol = self._volume_item(res)
        vol.removeRows(0, vol.rowCount())  # refresh on re-run
        for c in res.cells:
            leaf = QStandardItem(
                f"Storm  [id {c.cell_id}  {c.max_dbz:.1f} dBZ  depth {c.depth_km:.1f} km]"
            )
            leaf.setEditable(False)
            leaf.setData(c, CELL_ROLE)
            vol.appendRow(leaf)
        self.tree.expandAll()

    def show_results(self, results: list[VolumeResult]) -> None:
        self.clear()
        for res in results:
            self.add_result(res)

    def _on_clicked(self, index) -> None:
        cell = self.model.data(index, CELL_ROLE)
        if isinstance(cell, StormCell):
            self.storm_selected.emit(cell)
