"""Environment window — HRRR 0 °C freezing level for the selected warning.

A small top-level control window (like the Lightning and Satellite windows).
It owns no map of its own: pressing **Fetch** asks the app to pull the hourly
HRRR freezing-level analyses for the current warning's data window and run
vault detection — measuring how far each radar cell's high-reflectivity tower
extends above the 0 °C level and deriving its overshooting-top flag (the GOES
ABI OT flag was removed as unreliable; this is its replacement). **Clear**
drops the grids and the per-cell vault annotations.

The window stays deliberately dumb — it emits the current :class:`Warning` and
lets the app compute the time window, bbox, and parameters from settings.
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


class HRRRWindow(QMainWindow):
    fetch_requested = Signal(object)  # Warning
    clear_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Environment (HRRR)")
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
        self.level_label = QLabel("")
        self.hint_label = QLabel(
            "Tip: download radar first — the 0°C level feeds vault detection, "
            "which flags a storm's overshooting top when its tower core "
            "punches deep above the freezing level."
        )
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet("color: #888; font-size: 11px;")
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #888;")

        buttons = QHBoxLayout()
        self.fetch_btn = QPushButton("Fetch freezing level")
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
        layout.addWidget(self.level_label)
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
        self.level_label.setText("")
        self.status_label.setText("")
        self.fetch_btn.setEnabled(True)

    def set_busy(self, busy: bool) -> None:
        self.fetch_btn.setEnabled(not busy and self._warning is not None)

    def set_status(self, msg: str) -> None:
        self.status_label.setText(msg)

    def set_count(self, n_grids: int, n_vaults: int) -> None:
        self.count_label.setText(
            f"{n_grids} analysis hour(s) · {n_vaults} cell(s) with a vault/OT"
        )

    def set_level(self, text: str) -> None:
        """Show the freezing level for the moment on the map (e.g. '4.2 km MSL')."""
        self.level_label.setText(text)

    def _on_fetch(self) -> None:
        if self._warning is not None:
            self.set_busy(True)
            self.count_label.setText("")
            self.status_label.setText("Fetching HRRR 0°C freezing level…")
            self.fetch_requested.emit(self._warning)
