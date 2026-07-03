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
COUPLET_ROLE = Qt.UserRole + 2


def _z(dt: datetime) -> str:
    return dt.strftime("%H%M") + "Z"


def _cell_label(c: StormCell) -> str:
    """One-line storm summary, with cloud-top temp and vault/OT when available.

    e.g. ``id 1  63.0 dBZ  depth 16.5 km  -20°C CT  vault +5.2 km  OT`` — the
    CT segment appears once a GOES ABI scene has been associated to the cell;
    the vault/OT segments once an HRRR freezing-level grid has (vault depth is
    how far the tower core extends above the 0°C level).
    """
    parts = [f"id {c.cell_id}", f"{c.max_dbz:.1f} dBZ", f"depth {c.depth_km:.1f} km"]
    if c.cloud_top_c is not None:
        parts.append(f"{c.cloud_top_c:.0f}°C CT")
    if c.vault_depth_km is not None:
        parts.append(f"vault {c.vault_depth_km:+.1f} km")
        if c.overshooting_top:
            parts.append("OT")
    return "  ".join(parts)


def _couplet_label(cp) -> str:
    """One-line rotation summary, e.g. ``Vrot 32 kt SR · cyclonic · r 58 km``."""
    return (
        f"Vrot {cp.vr_sr_ms * 1.94384:.0f} kt SR"
        f" · {'cyclonic' if cp.cyclonic else 'anticyc'}"
        f" · r {cp.range_km:.0f} km"
    )


class LeftVolumesPane(QWidget):
    storm_selected = Signal(object)  # StormCell
    couplet_selected = Signal(object)  # detection.couplets.Couplet

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
            leaf = QStandardItem(f"Storm  [{_cell_label(c)}]")
            leaf.setEditable(False)
            leaf.setData(c, CELL_ROLE)
            vol.appendRow(leaf)
        for cp in getattr(res, "couplets", []) or []:
            leaf = QStandardItem(f"Couplet  [{_couplet_label(cp)}]")
            leaf.setEditable(False)
            leaf.setData(cp, COUPLET_ROLE)
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
            return
        couplet = self.model.data(index, COUPLET_ROLE)
        if couplet is not None:
            self.couplet_selected.emit(couplet)
