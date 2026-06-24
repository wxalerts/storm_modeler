"""Model pane — Phase B placeholder.

Intentionally empty in Phase A. The 3D model view, cross-sections, and batch
eval are out of scope until human review of Phase A passes.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class ModelPane(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        label = QLabel("3D Model View\n(Phase B)")
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("color: #888; font-size: 14px;")
        layout.addWidget(label)
