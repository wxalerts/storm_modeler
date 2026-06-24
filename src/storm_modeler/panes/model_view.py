"""Model pane (pane 3) — Phase B.

A ``QSplitter(Qt.Vertical)``:

* **top** — a 3D perspective ``pyvistaqt.QtInteractor`` showing the selected
  cell's volume: dBZ volume render, isosurface shells, the envelope prism, and a
  labelled height marker (B1);
* **bottom** — the vertical cross-section + time scrubber (B2).

Clicking a storm in the left-volumes pane drives this: the cell's grid is built
into the scene. The same offscreen-GL tolerance the map pane uses applies here —
on a display-less stack the actors are still built (panes fully instantiate) but
the live ``render()`` calls are skipped.
"""

from __future__ import annotations

import os

import structlog
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..detection.detection_v2 import StormCell
from ..settings.resolver import ResolvedSettings, ViewerParams
from ..viz import scene_builder
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


class ModelPane(QWidget):
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
            # skip the render() calls (``_allow_render``). ``_ensure_display``
            # guarantees Qt's platform and DISPLAY agree before we get here.
            off_screen = not os.environ.get("DISPLAY")
        self.off_screen = off_screen
        self._allow_render = not off_screen

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Toolbar (layer toggles).
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

        # Vertical split: 3D on top, cross-section (B2) below.
        split = QSplitter(Qt.Vertical)
        self.plotter = QtInteractor(self, off_screen=off_screen)
        if off_screen:
            try:
                self.plotter.ren_win.SetSize(1024, 768)
            except Exception:  # noqa: BLE001
                pass
        split.addWidget(self.plotter.interactor)

        self._xsection_placeholder = QLabel("Cross-section\n(Phase B2)")
        self._xsection_placeholder.setAlignment(Qt.AlignCenter)
        self._xsection_placeholder.setStyleSheet("color: #888; font-size: 13px;")
        split.addWidget(self._xsection_placeholder)
        split.setSizes([600, 300])
        self.split = split
        layout.addWidget(split, 1)

        try:
            self.plotter.set_background("black")
        except Exception as e:  # noqa: BLE001
            log.info("model.init_render_skipped", reason=str(e).splitlines()[0])

        # State.
        self._provider: GridProvider | None = None
        self._results: list = []
        self._cells_by_time: dict[str, list[StormCell]] = {}
        self._selected_cell: StormCell | None = None
        self._scene: scene_builder.SceneActors | None = None

    # --- settings ---------------------------------------------------------

    def set_settings(self, settings: ResolvedSettings) -> None:
        """Adopt new viewer settings; applied on the next scene build."""
        self._viewer = settings.viewer

    # --- selection wiring -------------------------------------------------

    def show_cell(self, cell: StormCell, results: list, source_factory=None) -> None:
        """Build the 3D scene for ``cell`` from its warning's volumes.

        ``results`` is the ordered list of ``VolumeResult`` for the warning (so
        the volume the cell belongs to — and, for B2, the rest of the event — is
        available). ``source_factory`` (optional) re-grids any volume evicted
        from the LRU cache during scrubbing.
        """
        self._results = list(results)
        self._cells_by_time = {
            res.volume.valid_time.isoformat(): list(res.cells) for res in self._results
        }
        self._selected_cell = cell

        provider = GridProvider(source_factory, cache_size=self._viewer.grid_cache_size)
        for res in self._results:
            provider.register(res.volume)
        provider.set_times(res.volume.valid_time for res in self._results)
        self._provider = provider

        idx = provider.index_of(cell.valid_time)
        self._render_index(idx, focus_cell=cell)

    def _render_index(self, index: int, focus_cell: StormCell | None = None) -> None:
        if self._provider is None or len(self._provider) == 0:
            return
        index = max(0, min(index, len(self._provider) - 1))
        try:
            volume = self._provider.get_index(index)
        except KeyError as e:
            log.info("model.grid_miss", reason=str(e))
            return
        cells = self._cells_by_time.get(volume.valid_time.isoformat(), [])
        emphasis = self._emphasis_cell(focus_cell, cells)
        if emphasis is None:
            return
        self._selected_cell = emphasis
        self._build(volume, emphasis, cells)

    def _emphasis_cell(
        self, focus: StormCell | None, cells: list[StormCell]
    ) -> StormCell | None:
        """Pick the cell to highlight in a volume.

        Prefer the same track as the originally-clicked cell; else the clicked
        cell itself (if it lives in this volume); else the strongest cell.
        """
        if focus is not None and self._selected_cell is not None:
            tid = self._selected_cell.track_id
            same = [c for c in cells if tid >= 0 and c.track_id == tid]
            if same:
                return same[0]
        if focus is not None and any(c.cell_id == focus.cell_id for c in cells):
            return next(c for c in cells if c.cell_id == focus.cell_id)
        if cells:
            return max(cells, key=lambda c: c.max_dbz)
        return focus

    def _build(self, volume, cell: StormCell, cells: list[StormCell]) -> None:
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
            self.plotter.view_isometric()
            self.plotter.camera.zoom(1.4)
            if self._allow_render:
                self.plotter.render()
        except Exception as e:  # noqa: BLE001
            log.info("model.render_skipped", reason=str(e).splitlines()[0])

    def _frame_axes(self, volume) -> None:
        ve = self._viewer.vert_exag
        try:
            self.plotter.show_bounds(
                xtitle="x (km E)", ytitle="y (km N)",
                ztitle=f"z (km AGL ×{ve:g})", color="gray",
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
