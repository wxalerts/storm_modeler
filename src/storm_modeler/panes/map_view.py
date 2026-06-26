"""Map pane — VTK orthographic top-down view.

A ``pyvistaqt.QtInteractor`` rendering, all in planar lon/lat space at ``z=0``:

* the vector basemap (counties + states + highways) as polylines;
* the self-rendered composite reflectivity from the gridded volume (NWS dBZ
  colours, geo-aligned RGBA) — never a tile server;
* cell envelopes as polygons, with the selected cell highlighted;
* the warning polygon for reference.

Clicking a storm in the nav pane recenters and highlights it here.
"""

from __future__ import annotations

import os

import numpy as np
import structlog
from PySide6.QtWidgets import QVBoxLayout, QWidget
from shapely.geometry import Polygon

from ..data import basemap as basemap_mod
from ..data import radar_render
from ..data.basemap import LAYER_STYLE
from ..detection.detection_v2 import StormCell
from ..models import GriddedVolume, Warning

log = structlog.get_logger(__name__)


def _polygon_to_lines(poly: Polygon, z: float = 0.01):
    """Closed polyline mesh of a polygon exterior at height ``z``."""
    import pyvista as pv

    coords = np.asarray(poly.exterior.coords)
    n = coords.shape[0]
    pts = np.hstack([coords[:, :2], np.full((n, 1), z)])
    mesh = pv.PolyData()
    mesh.points = pts
    mesh.lines = np.asarray([n] + list(range(n)))
    return mesh


class MapPane(QWidget):
    def __init__(self, parent: QWidget | None = None, off_screen: bool | None = None) -> None:
        super().__init__(parent)
        import pyvista as pv
        from pyvistaqt import QtInteractor

        # A real display (an X server, incl. Xvfb) means VTK has a working GL
        # context and we render live — this holds even under the Qt "offscreen"
        # platform, where the QWidget is windowless but VTK drives its own GL
        # window on DISPLAY. With no display at all we fall back to an offscreen
        # GL buffer and skip live renders (see _allow_render).
        if off_screen is None:
            off_screen = not os.environ.get("DISPLAY")
        self.off_screen = off_screen
        # Live GL renders are only performed when there is a real display.
        # Offscreen software GL on some headless stacks intermittently aborts
        # during shader compilation; in that mode we still build every actor
        # (so the panes are fully instantiated) but skip the render() calls.
        self._allow_render = not off_screen
        self.plotter = QtInteractor(self, off_screen=off_screen)
        if off_screen:
            # The interactor widget is never shown offscreen, so its viewport
            # would be 0x0 — force a real render-window size so VTK allocates a
            # valid framebuffer instead of aborting on a zero-length array.
            try:
                self.plotter.ren_win.SetSize(1024, 768)
            except Exception:  # noqa: BLE001
                pass
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.plotter.interactor)

        self._radar_actor = None
        self._warning_actor = None
        self._cell_actors: list = []
        self._highlight_actor = None
        self._extent: tuple[float, float, float, float] | None = None

        try:
            self.plotter.set_background("black")
            self.plotter.enable_parallel_projection()
            self.plotter.view_xy()
        except Exception as e:  # noqa: BLE001 - headless GL may be limited
            log.info("map.init_render_skipped", reason=str(e).splitlines()[0])

    # --- layers -----------------------------------------------------------

    def set_basemap(self, shapefile_dir=None) -> None:
        layers = basemap_mod.read_all_layers(shapefile_dir)
        for name, polylines in layers.items():
            poly = basemap_mod.polylines_to_polydata(polylines, z=0.0)
            if poly is None:
                continue
            style = LAYER_STYLE.get(name, {"color": (0.6, 0.6, 0.6), "line_width": 1.0})
            self._safe_add(poly, color=style["color"], line_width=style["line_width"])

    def show_warning(self, warning: Warning) -> None:
        if self._warning_actor is not None:
            self._safe_remove(self._warning_actor)
        mesh = _polygon_to_lines(warning.polygon, z=0.02)
        self._warning_actor = self._safe_add(
            mesh, color=(1.0, 1.0, 1.0), line_width=2.5
        )

    def set_radar(self, volume: GriddedVolume) -> None:
        if self._radar_actor is not None:
            self._safe_remove(self._radar_actor)
        grid = radar_render.radar_polydata(volume, z=0.0)
        self._radar_actor = self._safe_add(
            grid, scalars="rgba", rgba=True, show_scalar_bar=False
        )
        self._extent = radar_render.geo_bounds(volume)

    def add_cells(self, cells: list[StormCell]) -> None:
        for c in cells:
            mesh = _polygon_to_lines(c.envelope, z=0.03)
            actor = self._safe_add(mesh, color=(1.0, 1.0, 0.0), line_width=2.0)
            if actor is not None:
                self._cell_actors.append(actor)

    def _clear_cells(self) -> None:
        for actor in self._cell_actors:
            self._safe_remove(actor)
        self._cell_actors.clear()

    def show_result(self, warning: Warning, volume: GriddedVolume, cells) -> None:
        log.info("map.show_result.begin", valid_time=volume.valid_time.isoformat(),
                 n_cells=len(list(cells)), allow_render=self._allow_render)
        self.show_warning(warning)
        self.set_radar(volume)
        self._clear_cells()
        self.add_cells(list(cells))
        self.fit_view()
        log.info("map.show_result.done", valid_time=volume.valid_time.isoformat())

    # --- interaction ------------------------------------------------------

    def show_cell_selection(
        self, warning: Warning, volume: GriddedVolume, cells, cell: StormCell
    ) -> None:
        """Switch the map to a selected cell's volume.

        Swaps the radar layer to that volume's reflectivity, redraws that
        volume's cell envelopes, then highlights and recenters on the chosen
        cell. This is what makes the map track the volume you click in the
        navigator (the radar previously stayed on the last downloaded volume).
        """
        log.info("map.select_cell.begin", valid_time=volume.valid_time.isoformat(),
                 cell_id=cell.cell_id, allow_render=self._allow_render)
        self.show_warning(warning)
        self.set_radar(volume)
        self._clear_cells()
        self.add_cells(list(cells))
        self.highlight_cell(cell)
        log.info("map.select_cell.done", valid_time=volume.valid_time.isoformat())

    def highlight_cell(self, cell: StormCell) -> None:
        """Recenter on a cell and draw a bold highlight on its envelope."""
        if self._highlight_actor is not None:
            self._safe_remove(self._highlight_actor)
        mesh = _polygon_to_lines(cell.envelope, z=0.05)
        self._highlight_actor = self._safe_add(
            mesh, color=(1.0, 1.0, 1.0), line_width=5.0
        )
        self.recenter(cell.seed_lon, cell.seed_lat, span_deg=0.8)

    def recenter(self, lon: float, lat: float, span_deg: float = 1.5) -> None:
        try:
            cam = self.plotter.camera
            cam.focal_point = (lon, lat, 0.0)
            cam.position = (lon, lat, 100.0)
            cam.up = (0.0, 1.0, 0.0)
            cam.parallel_scale = span_deg
            if self._allow_render:
                self.plotter.render()
        except Exception as e:  # noqa: BLE001
            log.info("map.recenter_skipped", reason=str(e).splitlines()[0])

    def fit_view(self) -> None:
        try:
            if self._extent is not None:
                lon0, lon1, lat0, lat1 = self._extent
                self.recenter(
                    (lon0 + lon1) / 2,
                    (lat0 + lat1) / 2,
                    span_deg=max(lon1 - lon0, lat1 - lat0) / 2 * 1.1,
                )
            elif self._allow_render:
                self.plotter.reset_camera()
                self.plotter.render()
        except Exception as e:  # noqa: BLE001
            log.info("map.fit_skipped", reason=str(e).splitlines()[0])

    def clear(self) -> None:
        try:
            self.plotter.clear()
        except Exception:  # noqa: BLE001
            pass
        self._radar_actor = None
        self._warning_actor = None
        self._cell_actors.clear()
        self._highlight_actor = None
        self._extent = None

    # --- guarded plotter ops (headless GL tolerance) ----------------------

    def _safe_add(self, mesh, **kw):
        try:
            return self.plotter.add_mesh(mesh, **kw)
        except Exception as e:  # noqa: BLE001
            log.info("map.add_mesh_skipped", reason=str(e).splitlines()[0])
            return None

    def _safe_remove(self, actor) -> None:
        try:
            self.plotter.remove_actor(actor)
        except Exception:  # noqa: BLE001
            pass
