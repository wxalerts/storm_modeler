"""Model pane (pane 3) — Phase B.

A ``QSplitter(Qt.Vertical)``:

* **top** — a 3D perspective ``pyvistaqt.QtInteractor`` showing the selected
  cell's volume: dBZ volume render, isosurface shells, the envelope prism, and a
  labelled height marker (B1);
* **bottom** — the vertical cross-section panel (a second ``QtInteractor``) plus
  a time scrubber over the warning's volumes (B2).

Clicking a storm in the left-volumes pane drives this. Scrubbing the slider
loads each volume's grid via :class:`GridProvider` on a ``QThreadPool`` worker
(off the GUI thread) and rebuilds the 3D scene + cross-section when it arrives;
stale frames from fast dragging are dropped (coalesced) by a request id. The
camera is held fixed between scrub frames so you watch the echo top climb and
the envelope grow in place. The same offscreen-GL tolerance the map pane uses
applies: with no display the actors are still built but ``render()`` is skipped.
"""

from __future__ import annotations

import os

import structlog
from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..detection.detection_v2 import StormCell
from ..settings.resolver import ResolvedSettings, ViewerParams
from ..viz import scene_builder, xsection
from ..viz.grid_provider import GridProvider

log = structlog.get_logger(__name__)

# Toolbar toggles: (layer key, button label).
_TOGGLES = (
    ("volume", "Volume"),
    ("isosurfaces", "Isosurfaces"),
    ("envelope", "Envelope"),
    ("height", "Height"),
    ("other_cells", "Other cells"),
)


def _z(dt) -> str:
    return dt.strftime("%H%M") + "Z"


class _GridSignals(QObject):
    ready = Signal(int, object)   # req_id, GriddedVolume
    failed = Signal(int, str)     # req_id, message


class _GridLoadTask(QRunnable):
    """Fetch one volume's grid off the GUI thread (may re-grid on a cache miss)."""

    def __init__(self, provider: GridProvider, index: int, req_id: int,
                 signals: _GridSignals) -> None:
        super().__init__()
        self._provider = provider
        self._index = index
        self._req_id = req_id
        self._signals = signals

    @Slot()
    def run(self) -> None:
        try:
            vol = self._provider.get_index(self._index)
            self._signals.ready.emit(self._req_id, vol)
        except Exception as e:  # noqa: BLE001
            self._signals.failed.emit(self._req_id, str(e))


class ModelPane(QWidget):
    # Emitted for each scrub/playback frame so the map can follow the 3D view in
    # lock-step: (volume, emphasis cell, all cells in that volume).
    frame_ready = Signal(object, object, object)

    def __init__(
        self,
        settings: ResolvedSettings | None = None,
        parent: QWidget | None = None,
        off_screen: bool | None = None,
    ) -> None:
        super().__init__(parent)
        import pyvista as pv  # noqa: F401 - ensure VTK/pyvista import early
        from pyvistaqt import QtInteractor

        self._viewer: ViewerParams = settings.viewer if settings else ViewerParams()
        if off_screen is None:
            # Mirror the map pane: a live X server (incl. the private Xvfb the
            # smoke stands up) means VTK has a real GL context and we render
            # live; with no display we fall back to an offscreen GL buffer and
            # skip the render() calls. ``_ensure_display`` guarantees Qt's
            # platform and DISPLAY agree before we get here.
            off_screen = not os.environ.get("DISPLAY")
        self.off_screen = off_screen
        self._allow_render = not off_screen

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Toolbar (3D layer toggles).
        bar = QWidget()
        bar_l = QHBoxLayout(bar)
        bar_l.setContentsMargins(4, 2, 4, 2)
        bar_l.setSpacing(4)
        self._toggle_btns: dict[str, QToolButton] = {}
        for key, label in _TOGGLES:
            btn = QToolButton()
            btn.setText(label)
            btn.setCheckable(True)
            btn.setChecked(True)
            btn.toggled.connect(lambda on, k=key: self._on_toggle(k, on))
            bar_l.addWidget(btn)
            self._toggle_btns[key] = btn
        bar_l.addStretch(1)
        layout.addWidget(bar)

        # Vertical split: 3D on top, cross-section + scrubber below.
        split = QSplitter(Qt.Vertical)
        self.plotter = QtInteractor(self, off_screen=off_screen)
        self.xplotter = QtInteractor(self, off_screen=off_screen)
        for p in (self.plotter, self.xplotter):
            if off_screen:
                try:
                    p.ren_win.SetSize(1024, 768)
                except Exception:  # noqa: BLE001
                    pass
            try:
                p.set_background("black")
            except Exception as e:  # noqa: BLE001
                log.info("model.init_render_skipped", reason=str(e).splitlines()[0])
        split.addWidget(self.plotter.interactor)
        split.addWidget(self._build_xsection_panel())
        split.setSizes([560, 340])
        self.split = split
        layout.addWidget(split, 1)

        # Threading / scrub state.
        self._pool = QThreadPool.globalInstance()
        self._grid_signals = _GridSignals()
        self._grid_signals.ready.connect(self._on_grid_ready)
        self._grid_signals.failed.connect(self._on_grid_failed)
        self._req_id = 0
        self._pending_reset_cam = True
        self._pending_focus: StormCell | None = None
        self._cam_initialised = False

        self._provider: GridProvider | None = None
        self._results: list = []
        self._cells_by_time: dict[str, list[StormCell]] = {}
        self._selected_cell: StormCell | None = None
        self._scene: scene_builder.SceneActors | None = None

        # Play timer for the scrubber.
        self._play_timer = QTimer(self)
        self._play_timer.setInterval(700)
        self._play_timer.timeout.connect(self._advance)

    # --- bottom panel (cross-section + scrubber) -------------------------

    def _build_xsection_panel(self) -> QWidget:
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        v.addWidget(self.xplotter.interactor, 1)

        bar = QWidget()
        h = QHBoxLayout(bar)
        h.setContentsMargins(4, 2, 4, 4)
        h.setSpacing(6)
        self._play_btn = QPushButton("▶")
        self._play_btn.setFixedWidth(32)
        self._play_btn.clicked.connect(self._toggle_play)
        self._prev_btn = QPushButton("⏮")
        self._prev_btn.setFixedWidth(32)
        self._prev_btn.clicked.connect(lambda: self._step(-1))
        self._next_btn = QPushButton("⏭")
        self._next_btn.setFixedWidth(32)
        self._next_btn.clicked.connect(lambda: self._step(1))
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setMinimum(0)
        self._slider.setMaximum(0)
        self._slider.valueChanged.connect(self._on_slider)
        self._time_label = QLabel("—")
        self._time_label.setMinimumWidth(60)
        self._time_label.setStyleSheet("color: #ccc;")
        for w in (self._prev_btn, self._play_btn, self._next_btn):
            h.addWidget(w)
        h.addWidget(self._slider, 1)
        h.addWidget(self._time_label)
        v.addWidget(bar)
        return panel

    # --- settings ---------------------------------------------------------

    def set_settings(self, settings: ResolvedSettings) -> None:
        """Adopt new viewer settings; applied on the next scene build."""
        self._viewer = settings.viewer
        if self._provider is not None:
            self._provider.cache_size = max(
                self._viewer.grid_cache_size, len(self._results)
            )
        # Rebuild the current frame so changes (iso levels, vert exag…) show.
        if self._provider is not None and len(self._provider):
            self._request_index(self._slider.value(), reset_cam=False)

    # --- selection wiring -------------------------------------------------

    def show_cell(self, cell: StormCell, results: list, source_factory=None) -> None:
        """Build the 3D scene + cross-section for ``cell`` and arm the scrubber.

        ``results`` is the ordered list of ``VolumeResult`` for the warning;
        ``source_factory`` (optional) re-grids any volume evicted from the LRU
        cache during scrubbing.
        """
        self._play_timer.stop()
        self._set_play_icon(False)
        self._results = list(results)
        self._cells_by_time = {
            res.volume.valid_time.isoformat(): list(res.cells) for res in self._results
        }
        self._selected_cell = cell
        self._cam_initialised = False

        # Retain every already-gridded frame of this warning (they are already
        # in RAM via ``results``), so scrubbing within the event never re-grids;
        # ``grid_cache_size`` still bounds the on-demand re-grid path.
        cache_size = max(self._viewer.grid_cache_size, len(self._results))
        provider = GridProvider(source_factory, cache_size=cache_size)
        for res in self._results:
            provider.register(res.volume)
        provider.set_times(res.volume.valid_time for res in self._results)
        self._provider = provider

        idx = provider.index_of(cell.valid_time)
        self._slider.blockSignals(True)
        self._slider.setMaximum(max(0, len(provider) - 1))
        self._slider.setValue(idx)
        self._slider.blockSignals(False)
        self._request_index(idx, reset_cam=True, focus_cell=cell)

    # --- scrubber controls ------------------------------------------------

    def _on_slider(self, value: int) -> None:
        self._request_index(value, reset_cam=False)

    def _step(self, delta: int) -> None:
        self._play_timer.stop()
        self._set_play_icon(False)
        self._slider.setValue(
            max(0, min(self._slider.value() + delta, self._slider.maximum()))
        )

    def _advance(self) -> None:
        # Loop: wrap back to the first volume at the end (continuous radar loop).
        if self._slider.value() >= self._slider.maximum():
            self._slider.setValue(0)
        else:
            self._slider.setValue(self._slider.value() + 1)

    def _toggle_play(self) -> None:
        if self._play_timer.isActive():
            self._play_timer.stop()
            self._set_play_icon(False)
        elif self._provider is not None and len(self._provider) > 1:
            if self._slider.value() >= self._slider.maximum():
                self._slider.setValue(0)
            self._play_timer.start()
            self._set_play_icon(True)

    def _set_play_icon(self, playing: bool) -> None:
        self._play_btn.setText("⏸" if playing else "▶")

    # --- threaded grid load + build --------------------------------------

    def _request_index(
        self, index: int, reset_cam: bool, focus_cell: StormCell | None = None
    ) -> None:
        if self._provider is None or len(self._provider) == 0:
            return
        index = max(0, min(index, len(self._provider) - 1))
        self._req_id += 1
        self._pending_reset_cam = reset_cam
        self._pending_focus = focus_cell
        task = _GridLoadTask(self._provider, index, self._req_id, self._grid_signals)
        self._pool.start(task)

    @Slot(int, object)
    def _on_grid_ready(self, req_id: int, volume) -> None:
        if req_id != self._req_id:
            return  # stale frame from a faster scrub — drop it
        cells = self._cells_by_time.get(volume.valid_time.isoformat(), [])
        emphasis = self._emphasis_cell(self._pending_focus, cells)
        if emphasis is None:
            return
        self._selected_cell = emphasis
        self._build_3d(volume, emphasis, cells, reset_cam=self._pending_reset_cam)
        self._build_xsection(volume, emphasis)
        self._time_label.setText(_z(volume.valid_time))
        # Drive the map in lock-step with this 3D frame (radar + cells follow).
        self.frame_ready.emit(volume, emphasis, cells)

    @Slot(int, str)
    def _on_grid_failed(self, req_id: int, message: str) -> None:
        if req_id == self._req_id:
            log.info("model.grid_miss", reason=message)

    def _emphasis_cell(
        self, focus: StormCell | None, cells: list[StormCell]
    ) -> StormCell | None:
        """Pick the cell to highlight in a volume.

        Prefer the same track as the originally-clicked cell; else the clicked
        cell itself (if it lives in this volume); else the strongest cell.
        """
        if self._selected_cell is not None:
            tid = self._selected_cell.track_id
            same = [c for c in cells if tid >= 0 and c.track_id == tid]
            if same:
                return same[0]
        if focus is not None and any(c.cell_id == focus.cell_id for c in cells):
            return next(c for c in cells if c.cell_id == focus.cell_id)
        if cells:
            return max(cells, key=lambda c: c.max_dbz)
        return focus

    def _build_3d(self, volume, cell: StormCell, cells: list[StormCell],
                  reset_cam: bool) -> None:
        log.info("model.build_3d.begin", valid_time=volume.valid_time.isoformat(),
                 cell_id=cell.cell_id, n_cells=len(cells), reset_cam=reset_cam)
        toggles = {k: b.isChecked() for k, b in self._toggle_btns.items()}
        try:
            self.plotter.clear()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._scene = scene_builder.build_scene(
                self.plotter, volume, cell, self._viewer,
                other_cells=cells, toggles=toggles,
            )
        except Exception as e:  # noqa: BLE001
            log.info("model.build_skipped", reason=str(e).splitlines()[0])
            return
        self._frame_axes(volume)
        try:
            # Keep the camera fixed between scrub frames so the storm evolves in
            # place; only frame it on a fresh selection.
            if reset_cam or not self._cam_initialised:
                self.plotter.view_isometric()
                self._frame_camera_on_cell(volume, cell)
                self._cam_initialised = True
            if self._allow_render:
                self.plotter.render()
        except Exception as e:  # noqa: BLE001
            log.info("model.render_skipped", reason=str(e).splitlines()[0])
        log.info("model.build_3d.done", cell_id=cell.cell_id)

    def _frame_camera_on_cell(self, volume, cell: StormCell) -> None:
        """Frame the isometric camera on the selected cell, not the whole domain.

        ``view_isometric`` fits *every* actor — including the faint other-cell
        prisms scattered across the 300 km grid — which shrinks the storm of
        interest into a corner. Reset to a bounding box around just this cell's
        ROI (its footprint + padding, ground to echo top) so it sits centred.
        """
        from ..viz.scene_builder import roi_window_km

        ve = self._viewer.vert_exag
        w = roi_window_km(volume, cell)
        cx, cy = cell.seed_x / 1000.0, cell.seed_y / 1000.0
        ztop = max(cell.echo_top_km, 0.1) * ve
        bounds = (cx - w, cx + w, cy - w, cy + w, 0.0, ztop)
        try:
            self.plotter.reset_camera(bounds=bounds)
            self.plotter.camera.zoom(1.1)
        except Exception as e:  # noqa: BLE001
            log.info("model.frame_skipped", reason=str(e).splitlines()[0])
            self.plotter.camera.zoom(1.4)

    def _build_xsection(self, volume, cell: StormCell) -> None:
        log.info("model.build_xsection.begin", cell_id=cell.cell_id)
        try:
            self.xplotter.clear()
        except Exception:  # noqa: BLE001
            pass
        try:
            section = xsection.make_section(volume, cell, self._results, self._viewer)
            xsection.build_panel(self.xplotter, volume, cell, section, self._viewer)
            if self._allow_render:
                self.xplotter.render()
        except Exception as e:  # noqa: BLE001
            log.info("model.xsection_skipped", reason=str(e).splitlines()[0])
        log.info("model.build_xsection.done", cell_id=cell.cell_id)

    def _frame_axes(self, volume) -> None:
        ve = self._viewer.vert_exag
        try:
            self.plotter.show_bounds(
                xtitle="x (km E)", ytitle="y (km N)",
                ztitle=f"z (km AGL x{ve:g})", color="gray",
            )
        except Exception as e:  # noqa: BLE001
            log.info("model.axes_skipped", reason=str(e).splitlines()[0])

    # --- toolbar ----------------------------------------------------------

    def _on_toggle(self, layer: str, on: bool) -> None:
        if self._scene is not None:
            self._scene.set_visible(layer, on)
            try:
                if self._allow_render:
                    self.plotter.render()
            except Exception:  # noqa: BLE001
                pass
