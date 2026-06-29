"""Lightning window — fetch GOES GLM flashes for the selected warning.

A small top-level control window (GIMP-style, like the 3D View and Logs
windows). It owns no map of its own: pressing **Fetch** asks the app to pull GLM
``LCFA`` flashes for the current warning's data window, and those flashes are
layered as markers onto the existing Leaflet map. **Clear** removes the markers.

The window stays deliberately dumb — it emits the current :class:`Warning` and
lets the app compute the time window, bbox, and satellite from settings.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..models import Warning


class _GradientBar(QWidget):
    """Horizontal legend strip matching the map's oldest→newest strike ramp.

    The map colours each strike ``hsl(240*(1-t), 100%, 55%)`` over its time
    fraction ``t`` (blue oldest → red newest); this paints the same ramp so the
    bar is a faithful key for the markers.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(14)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(0, 0, -1, -1)
        grad = QLinearGradient(rect.left(), 0, rect.right(), 0)
        steps = 12
        for i in range(steps + 1):
            t = i / steps
            hue = int(round(240 * (1 - t)))
            grad.setColorAt(t, QColor.fromHsl(hue, 255, int(0.55 * 255)))
        p.setPen(QColor("#555"))
        p.setBrush(grad)
        p.drawRoundedRect(rect, 3, 3)


class LightningWindow(QMainWindow):
    fetch_requested = Signal(object)  # Warning
    clear_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Lightning")
        self.resize(360, 200)
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
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #888;")

        buttons = QHBoxLayout()
        self.fetch_btn = QPushButton("Fetch lightning")
        self.fetch_btn.setEnabled(False)
        self.fetch_btn.clicked.connect(self._on_fetch)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self.clear_requested.emit)
        buttons.addWidget(self.fetch_btn)
        buttons.addWidget(self.clear_btn)

        # Colour key for the strike markers (oldest → newest).
        self.legend_title = QLabel("Strike time")
        self.legend_title.setStyleSheet("color: #888;")
        self.legend_bar = _GradientBar()
        ends = QHBoxLayout()
        self.legend_start = QLabel("older")
        self.legend_end = QLabel("newer")
        for lbl, align in ((self.legend_start, Qt.AlignLeft),
                           (self.legend_end, Qt.AlignRight)):
            lbl.setStyleSheet("color: #888; font-size: 11px;")
            lbl.setAlignment(align)
        ends.addWidget(self.legend_start)
        ends.addStretch(1)
        ends.addWidget(self.legend_end)

        layout.addWidget(self.event_label)
        layout.addWidget(self.window_label)
        layout.addLayout(buttons)
        layout.addWidget(self.count_label)
        layout.addWidget(self.legend_title)
        layout.addWidget(self.legend_bar)
        layout.addLayout(ends)
        layout.addWidget(self.status_label)
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
        self.set_time_range(None, None)
        self.fetch_btn.setEnabled(True)

    def set_time_range(self, t0: datetime | None, t1: datetime | None) -> None:
        """Label the gradient ends with the event's strike time span."""
        if t0 is None or t1 is None:
            self.legend_start.setText("older")
            self.legend_end.setText("newer")
        else:
            self.legend_start.setText(f"{t0:%H:%MZ}")
            self.legend_end.setText(f"{t1:%H:%MZ}")

    def set_busy(self, busy: bool) -> None:
        self.fetch_btn.setEnabled(not busy and self._warning is not None)

    def set_status(self, msg: str) -> None:
        self.status_label.setText(msg)

    def set_count(self, shown: int, total: int | None = None) -> None:
        if total is None or total == shown:
            self.count_label.setText(f"{shown} strike(s) on the map.")
        else:
            self.count_label.setText(
                f"{shown} strike(s) on this volume — {total} in the event."
            )

    def _on_fetch(self) -> None:
        if self._warning is not None:
            self.set_busy(True)
            self.count_label.setText("")
            self.status_label.setText("Fetching GLM lightning…")
            self.fetch_requested.emit(self._warning)
