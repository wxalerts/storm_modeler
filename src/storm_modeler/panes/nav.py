"""Nav pane — the clickable hierarchy.

``State → UGC → Warning → Volume → Storm``. Storm leaves carry their
:class:`StormCell`; clicking one emits :data:`NavPane.storm_selected` so the map
pane can recenter and highlight that cell.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QTreeView, QVBoxLayout, QWidget

from ..detection.detection_v2 import StormCell
from ..pipeline import VolumeResult

CELL_ROLE = Qt.UserRole + 1
KIND_ROLE = Qt.UserRole + 2


def _z(dt: datetime) -> str:
    return dt.strftime("%H%M") + "Z"


class NavPane(QWidget):
    storm_selected = Signal(object)  # StormCell

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tree = QTreeView(self)
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.model = QStandardItemModel(self)
        self.tree.setModel(self.model)
        self.tree.clicked.connect(self._on_clicked)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tree)

        # Node registries for dedup.
        self._states: dict[str, QStandardItem] = {}
        self._ugcs: dict[tuple[str, str], QStandardItem] = {}
        self._warnings: dict[str, QStandardItem] = {}
        self._volumes: dict[tuple[str, str], QStandardItem] = {}

    def clear(self) -> None:
        self.model.clear()
        self._states.clear()
        self._ugcs.clear()
        self._warnings.clear()
        self._volumes.clear()

    # --- tree construction ------------------------------------------------

    def _node(self, text: str, kind: str, cell: StormCell | None = None) -> QStandardItem:
        it = QStandardItem(text)
        it.setEditable(False)
        it.setData(kind, KIND_ROLE)
        if cell is not None:
            it.setData(cell, CELL_ROLE)
        return it

    def _state_item(self, state: str) -> QStandardItem:
        if state not in self._states:
            it = self._node(f"State ({state})", "state")
            self.model.appendRow(it)
            self._states[state] = it
        return self._states[state]

    def _ugc_item(self, state: str, ugc: str) -> QStandardItem:
        key = (state, ugc)
        if key not in self._ugcs:
            parent = self._state_item(state)
            it = self._node(f"UGC ({ugc})", "ugc")
            parent.appendRow(it)
            self._ugcs[key] = it
        return self._ugcs[key]

    def _warning_item(self, res: VolumeResult) -> QStandardItem:
        w = res.warning
        if w.id not in self._warnings:
            state = (w.states or ["??"])[0]
            ugc = (w.ugc or ["??????"])[0]
            parent = self._ugc_item(state, ugc)
            label = (
                f"Warning  [{w.event}  {_z(w.issued)}–{_z(w.expires)}  "
                f"{res.site.icao} ETN {w.etn:04d}]"
            )
            it = self._node(label, "warning")
            parent.appendRow(it)
            self._warnings[w.id] = it
        return self._warnings[w.id]

    def _volume_item(self, res: VolumeResult) -> QStandardItem:
        key = (res.warning.id, res.volume.valid_time.isoformat())
        if key not in self._volumes:
            parent = self._warning_item(res)
            it = self._node(f"Volume  [{_z(res.volume.valid_time)}]", "volume")
            parent.appendRow(it)
            self._volumes[key] = it
        return self._volumes[key]

    def add_result(self, res: VolumeResult) -> None:
        """Add a processed volume and its storms to the tree."""
        vol_item = self._volume_item(res)
        for c in res.cells:
            label = (
                f"Storm  [id {c.cell_id}  {c.max_dbz:.1f} dBZ  "
                f"depth {c.depth_km:.1f} km]"
            )
            vol_item.appendRow(self._node(label, "storm", cell=c))
        self.tree.expandAll()

    # --- interaction ------------------------------------------------------

    def _on_clicked(self, index) -> None:
        cell = self.model.data(index, CELL_ROLE)
        if isinstance(cell, StormCell):
            self.storm_selected.emit(cell)
