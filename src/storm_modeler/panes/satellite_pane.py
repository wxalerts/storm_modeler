"""Satellite window — GOES ABI cloud-top analysis for the selected warning.

A small top-level control window (like the Lightning, 3D View, and Logs
windows). It owns no map of its own: pressing **Fetch** asks the app to pull ABI
Band-13 scenes for the current warning's data window, run cloud-top detection,
and layer the brightness-temperature image + cloud-top/overshooting-top/anvil
markers onto the existing Leaflet map. **Clear** removes the overlay.

The window stays deliberately dumb — it emits the current :class:`Warning` and
lets the app compute the time window, bbox, and satellite from settings.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..models import Warning


class SatelliteWindow(QMainWindow):
    fetch_requested = Signal(object)  # Warning
    clear_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Satellite")
        self.resize(380, 240)
        self._warning: Warning | None = None

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        self.event_label = QLabel("No warning selected.")
        self.event_label.setWordWrap(True)
        self.window_label = QLabel("")
        self.window_label.setStyleSheet("color: #888;")
        self.count_label = QLabel("")
        self.count_label.setStyleSheet("font-weight: bold;")
        self.hint_label = QLabel(
            "Tip: download radar first so cloud-top tilt can be measured "
            "against the radar core."
        )
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet("color: #888; font-size: 11px;")
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #888;")

        buttons = QHBoxLayout()
        self.fetch_btn = QPushButton("Fetch cloud tops")
        self.fetch_btn.setEnabled(False)
        self.fetch_btn.clicked.connect(self._on_fetch)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self.clear_requested.emit)
        buttons.addWidget(self.fetch_btn)
        buttons.addWidget(self.clear_btn)

        layout.addWidget(self.event_label)
        layout.addWidget(self.window_label)
        layout.addLayout(buttons)
        layout.addWidget(self.count_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.hint_label)
        layout.addStretch(1)
        self.setCentralWidget(central)

    def set_warning(self, warning: Warning) -> None:
        """Arm the window for *warning* (enables Fetch, shows its span)."""
        self._warning = warning
        self.event_label.setText(
            f"{warning.event}  ETN {warning.etn:04d}  ({warning.wfo})"
        )
        self.window_label.setText(
            f"{warning.issued:%Y-%m-%d %H:%MZ} → {warning.expires:%H:%MZ}"
        )
        self.count_label.setText("")
        self.status_label.setText("")
        self.fetch_btn.setEnabled(True)

    def set_busy(self, busy: bool) -> None:
        self.fetch_btn.setEnabled(not busy and self._warning is not None)

    def set_status(self, msg: str) -> None:
        self.status_label.setText(msg)

    def set_count(self, n_scenes: int, n_cells: int, n_ot: int) -> None:
        self.count_label.setText(
            f"{n_scenes} scene(s) · {n_cells} cloud-top cell(s) · {n_ot} OT"
        )

    def _on_fetch(self) -> None:
        if self._warning is not None:
            self.set_busy(True)
            self.count_label.setText("")
            self.status_label.setText("Fetching GOES ABI cloud tops…")
            self.fetch_requested.emit(self._warning)
