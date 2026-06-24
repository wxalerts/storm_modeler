"""Left-panel top: IEM search form + results.

Search fields are pinned at the top (date start/end, states/WFO, event-type
toggles, optional pre/post-window override) and pre-populate from settings
defaults. Below scrolls a results list — one row per matching warning with a
per-row ``[Download]`` button that fetches + processes that warning's NEXRAD
window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..models import Warning
from ..settings.resolver import ResolvedSettings


@dataclass
class SearchParams:
    start: datetime
    end: datetime
    states: list[str] = field(default_factory=list)
    tornado: bool = True
    severe: bool = True
    pre_minutes: int = 60
    post_minutes: int = 30


def _z(dt: datetime) -> str:
    return dt.strftime("%H%M") + "Z"


class _ResultRow(QWidget):
    def __init__(self, warning: Warning, on_download) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        states = ",".join(warning.states)
        text = (
            f"{warning.event} · {warning.wfo} · ETN {warning.etn:04d} · "
            f"{_z(warning.issued)}–{_z(warning.expires)} · {states}"
        )
        label = QLabel(text)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(label, 1)
        btn = QPushButton("Download")
        btn.setFixedWidth(90)
        btn.clicked.connect(lambda: on_download(warning))
        lay.addWidget(btn)


class LeftSearchPane(QWidget):
    search_requested = Signal(object)      # SearchParams
    download_requested = Signal(object)    # Warning
    warning_selected = Signal(object)      # Warning

    def __init__(self, settings: ResolvedSettings | None = None, parent=None) -> None:
        super().__init__(parent)
        self._warnings: list[Warning] = []

        root = QVBoxLayout(self)

        form = QFormLayout()
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        lookback = settings.iem_default_lookback_hours if settings else 12
        self.start_edit = QLineEdit((now - timedelta(hours=lookback)).strftime("%Y-%m-%dT%H:%MZ"))
        self.end_edit = QLineEdit(now.strftime("%Y-%m-%dT%H:%MZ"))
        self.states_edit = QLineEdit(",".join(settings.iem_default_states) if settings else "")
        self.states_edit.setPlaceholderText("states/WFO, e.g. TX,OK")
        self.tornado_chk = QCheckBox("Tornado")
        self.tornado_chk.setChecked(True)
        self.severe_chk = QCheckBox("Severe Tstorm")
        self.severe_chk.setChecked(True)
        ev = QHBoxLayout()
        ev.addWidget(self.tornado_chk)
        ev.addWidget(self.severe_chk)
        ev.addStretch(1)
        ev_w = QWidget()
        ev_w.setLayout(ev)
        self.pre_spin = QSpinBox()
        self.pre_spin.setRange(0, 240)
        self.pre_spin.setValue(settings.pre_minutes if settings else 60)
        self.post_spin = QSpinBox()
        self.post_spin.setRange(0, 240)
        self.post_spin.setValue(settings.post_minutes if settings else 30)
        win = QHBoxLayout()
        win.addWidget(QLabel("pre"))
        win.addWidget(self.pre_spin)
        win.addWidget(QLabel("post"))
        win.addWidget(self.post_spin)
        win.addStretch(1)
        win_w = QWidget()
        win_w.setLayout(win)

        form.addRow("Start (UTC)", self.start_edit)
        form.addRow("End (UTC)", self.end_edit)
        form.addRow("States/WFO", self.states_edit)
        form.addRow("Events", ev_w)
        form.addRow("Window (min)", win_w)
        root.addLayout(form)

        self.search_btn = QPushButton("Search")
        self.search_btn.clicked.connect(self._emit_search)
        root.addWidget(self.search_btn)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        root.addWidget(QLabel("Results"))
        self.results = QListWidget()
        self.results.currentRowChanged.connect(self._on_row)
        root.addWidget(self.results, 1)

    # --- search -----------------------------------------------------------

    def _parse(self, s: str) -> datetime:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00")).astimezone(
            timezone.utc
        )

    def _emit_search(self) -> None:
        try:
            params = SearchParams(
                start=self._parse(self.start_edit.text()),
                end=self._parse(self.end_edit.text()),
                states=[s.strip().upper() for s in self.states_edit.text().split(",") if s.strip()],
                tornado=self.tornado_chk.isChecked(),
                severe=self.severe_chk.isChecked(),
                pre_minutes=self.pre_spin.value(),
                post_minutes=self.post_spin.value(),
            )
        except ValueError:
            return
        self.search_requested.emit(params)

    def set_searching(self, on: bool) -> None:
        self.progress.setVisible(on)
        self.progress.setRange(0, 0 if on else 1)
        self.search_btn.setEnabled(not on)

    # --- results ----------------------------------------------------------

    def clear_results(self) -> None:
        self.results.clear()
        self._warnings.clear()

    def add_result(self, warning: Warning) -> None:
        self._warnings.append(warning)
        item = QListWidgetItem(self.results)
        row = _ResultRow(warning, self.download_requested.emit)
        item.setSizeHint(row.sizeHint())
        self.results.addItem(item)
        self.results.setItemWidget(item, row)

    def _on_row(self, row: int) -> None:
        if 0 <= row < len(self._warnings):
            self.warning_selected.emit(self._warnings[row])
