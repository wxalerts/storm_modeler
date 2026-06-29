"""Storm Track window — list a warning's storm tracks and chart their history.

A top-level control window (GIMP-style, like the Lightning and Satellite
windows). The left half lists every unique storm SCIT tracked across the
selected warning's volumes (rebuilt by :func:`storm_modeler.tracks.build_tracks`
as new volumes land); clicking one charts its history on the right — reflectivity,
cloud-top temperature, overshooting-top flag, and echo-top height versus volume
time — and emits :pyattr:`track_selected` so the app draws the track on the map.

The window holds the :class:`StormTrack` objects so it can chart them, but the
app still owns building them and pushing them to the map; the window only emits
the selected track and the clear request.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QLabel,
    QListView,
    QMainWindow,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure

from ..models import Warning
from ..tracks import StormTrack

TRACK_ROLE = Qt.UserRole + 1


def _z(dt) -> str:
    return dt.strftime("%H%M") + "Z"


class _TrackCharts(FigureCanvasQTAgg):
    """Four stacked time-series panels for one storm track.

    Reflectivity, cloud-top temperature, overshooting-top flag and echo-top
    height share a common x-axis of the track's volume times. Cloud-top
    temperature is only present once a GOES ABI scene has been associated, so its
    panel leaves gaps where the storm has no satellite match.
    """

    def __init__(self) -> None:
        self._fig = Figure(figsize=(4.4, 6.0), facecolor="#101010")
        super().__init__(self._fig)
        self._axes = self._fig.subplots(4, 1, sharex=True)
        self.clear()

    def _style(self, ax, ylabel: str) -> None:
        ax.set_facecolor("#101010")
        for spine in ax.spines.values():
            spine.set_color("#666")
        ax.tick_params(colors="#ccc", labelsize=8)
        ax.set_ylabel(ylabel, color="#ccc", fontsize=8)
        ax.grid(True, color="#333", linewidth=0.5)

    def clear(self) -> None:
        labels = ["max dBZ", "cloud top (°C)", "overshoot", "echo top (km)"]
        for ax, lbl in zip(self._axes, labels):
            ax.clear()
            self._style(ax, lbl)
        self._fig.suptitle("Select a track", color="#ccc", fontsize=10)
        self._redraw()

    def set_track(self, track: StormTrack) -> None:
        samples = track.samples
        xs = list(range(len(samples)))
        dbz = [s.max_dbz for s in samples]
        tops = [s.echo_top_km for s in samples]
        ax_dbz, ax_ct, ax_ot, ax_top = self._axes
        for ax, lbl in zip(self._axes,
                           ["max dBZ", "cloud top (°C)", "overshoot",
                            "echo top (km)"]):
            ax.clear()
            self._style(ax, lbl)

        ax_dbz.plot(xs, dbz, "-o", color="#3fb6ff", markersize=4, linewidth=1.5)

        # Cloud-top temperature: only the volumes with a satellite match.
        ct_xs = [i for i, s in enumerate(samples) if s.cloud_top_c is not None]
        ct_ys = [samples[i].cloud_top_c for i in ct_xs]
        if ct_xs:
            ax_ct.plot(ct_xs, ct_ys, "-o", color="#ffd24a",
                       markersize=4, linewidth=1.5)
        else:
            ax_ct.text(0.5, 0.5, "no satellite match", color="#666",
                       fontsize=8, ha="center", va="center",
                       transform=ax_ct.transAxes)

        # Overshooting-top flag as a 0/1 step, OT volumes marked.
        ot = [1 if s.overshooting_top else 0 for s in samples]
        ax_ot.step(xs, ot, where="mid", color="#ff5050", linewidth=1.5)
        ot_xs = [i for i, v in enumerate(ot) if v]
        if ot_xs:
            ax_ot.plot(ot_xs, [1] * len(ot_xs), "o", color="#ff5050",
                       markersize=5)
        ax_ot.set_ylim(-0.2, 1.2)
        ax_ot.set_yticks([0, 1])
        ax_ot.set_yticklabels(["no", "yes"])

        ax_top.plot(xs, tops, "-o", color="#5fd35f", markersize=4, linewidth=1.5)
        ax_top.set_ylim(0, max(tops) * 1.15 + 1)

        n = len(samples)
        ax_top.set_xlim(-0.5, max(n - 1, 0) + 0.5)
        step = max(1, n // 8)
        ticks = list(range(0, n, step))
        ax_top.set_xticks(ticks)
        ax_top.set_xticklabels([_z(samples[i].valid_time) for i in ticks])

        title = (f"Track {track.track_id}" if track.track_id >= 0
                 else f"Cell @ {_z(track.start_time)}")
        self._fig.suptitle(f"{title} · {track.site}", color="#ccc", fontsize=10)
        self._redraw()

    def _redraw(self) -> None:
        try:
            self._fig.tight_layout(rect=(0, 0, 1, 0.97))
        except Exception:  # noqa: BLE001
            pass
        self.draw_idle()


class StormTrackWindow(QMainWindow):
    track_selected = Signal(object)  # StormTrack (None when deselected)
    clear_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Storm Tracks")
        self.resize(640, 620)
        self._warning: Warning | None = None
        self._tracks: list[StormTrack] = []
        self._selected_key: str | None = None

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(6)

        self.header = QLabel("No warning selected.")
        self.header.setWordWrap(True)
        outer.addWidget(self.header)

        split = QSplitter(Qt.Horizontal)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(4)
        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color: #888;")
        self.list = QListView()
        self.list.setEditTriggers(QListView.NoEditTriggers)
        self.model = QStandardItemModel(self)
        self.list.setModel(self.model)
        self.list.selectionModel().currentChanged.connect(self._on_current)
        self.clear_btn = QPushButton("Clear track")
        self.clear_btn.clicked.connect(self._on_clear)
        lv.addWidget(self.count_label)
        lv.addWidget(self.list, 1)
        lv.addWidget(self.clear_btn)

        self.charts = _TrackCharts()

        split.addWidget(left)
        split.addWidget(self.charts)
        split.setSizes([220, 420])
        outer.addWidget(split, 1)
        self.setCentralWidget(central)

    # --- public surface --------------------------------------------------

    def set_warning(self, warning: Warning) -> None:
        """Arm the window for *warning* and drop the previous track listing."""
        self._warning = warning
        self.header.setText(
            f"{warning.event}  ETN {warning.etn:04d}  ({warning.wfo})"
        )
        self.set_tracks([])

    def set_tracks(self, tracks: list[StormTrack]) -> None:
        """Replace the track listing, preserving the current selection if it
        survives the rebuild (so the charts/map keep following a live storm as
        new volumes land)."""
        self._tracks = list(tracks)
        self.model.clear()
        for t in self._tracks:
            it = QStandardItem(t.label())
            it.setEditable(False)
            it.setData(t.key, TRACK_ROLE)
            self.model.appendRow(it)
        self.count_label.setText(f"{len(self._tracks)} track(s)")
        if not self._tracks:
            self._selected_key = None
            self.charts.clear()
            return
        # Re-select the same track (by key) if it is still present.
        target = self._selected_key
        for row, t in enumerate(self._tracks):
            if t.key == target:
                self.list.setCurrentIndex(self.model.index(row, 0))
                self.charts.set_track(t)
                self.track_selected.emit(t)
                return
        # Selection gone (or none yet): leave nothing selected.
        self._selected_key = None
        self.charts.clear()

    # --- internals -------------------------------------------------------

    def _track_for_key(self, key: str | None) -> StormTrack | None:
        return next((t for t in self._tracks if t.key == key), None)

    def _on_current(self, current, _previous) -> None:
        key = current.data(TRACK_ROLE) if current.isValid() else None
        track = self._track_for_key(key)
        self._selected_key = key
        if track is None:
            self.charts.clear()
            return
        self.charts.set_track(track)
        self.track_selected.emit(track)

    def _on_clear(self) -> None:
        self._selected_key = None
        self.list.clearSelection()
        self.list.setCurrentIndex(self.model.index(-1, -1))
        self.charts.clear()
        self.clear_requested.emit()
