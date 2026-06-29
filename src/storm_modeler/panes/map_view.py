"""Map pane — 2D radar viewer on a ``QGraphicsView`` (pure-Qt raster).

A slippy-map-style top-down view that behaves like Leaflet: click-drag to pan,
scroll wheel to zoom, **no** 3D rotation. It renders with Qt's raster engine
(no OpenGL, no Chromium) — Qt WebEngine paints a black surface on this machine's
GL stack, so an embedded web map is not an option here.

Everything is placed in one lon/lat-derived scene coordinate system, so the
layers line up exactly:

* the offline vector basemap (counties / states / highways) as cosmetic-pen
  paths over a dark background;
* the composite reflectivity, warped to a lon/lat raster (NWS dBZ colours) and
  dropped in as a pixmap at its geographic bounds;
* the SCIT cell envelopes, with the selected cell bolded;
* the warning polygon for reference.

Scene coords are ``x = lon·cos(LAT_REF)``, ``y = -lat`` (north up), with a fixed
reference latitude so the aspect ratio is roughly geographic across CONUS.
"""

from __future__ import annotations

import math

import numpy as np
import structlog
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QVBoxLayout,
    QWidget,
)

from ..data import basemap as basemap_mod
from ..data import radar_render
from ..data.basemap import LAYER_STYLE
from ..detection.detection_v2 import StormCell
from ..models import GriddedVolume, Warning

log = structlog.get_logger(__name__)

#: Fixed reference latitude for the lon aspect correction (CONUS mid-latitude).
LAT_REF = 38.0
_SX = math.cos(math.radians(LAT_REF))

# Z ordering.
_Z_BASEMAP = 0
_Z_RADAR = 1
_Z_CELLS = 2
_Z_WARNING = 3
_Z_HIGHLIGHT = 4


def _qcolor(rgb01, alpha: int = 255) -> QColor:
    r, g, b = rgb01
    return QColor(int(r * 255), int(g * 255), int(b * 255), alpha)


def _sx(lon):
    return np.asarray(lon, dtype=np.float64) * _SX


class _MapView(QGraphicsView):
    """A non-rotating pan/zoom view: drag pans, wheel zooms, that's it."""

    def __init__(self, scene, parent=None) -> None:
        super().__init__(scene, parent)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setBackgroundBrush(QBrush(QColor("#0e1117")))
        # y grows downward in scene space already (we negate lat), so no flip.

    def wheelEvent(self, event) -> None:  # noqa: N802
        factor = 1.25 if event.angleDelta().y() > 0 else 1 / 1.25
        self.scale(factor, factor)


class MapPane(QWidget):
    def __init__(self, parent: QWidget | None = None, off_screen: bool | None = None) -> None:
        super().__init__(parent)
        self.scene = QGraphicsScene(self)
        self.view = _MapView(self.scene, self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

        self._basemap_items: list = []
        self._radar_item: QGraphicsPixmapItem | None = None
        self._radar_img: QImage | None = None  # keep the backing buffer alive
        self._warning_item: QGraphicsPathItem | None = None
        self._cell_items: list = []
        self._highlight_item: QGraphicsPathItem | None = None
        self._extent: QRectF | None = None  # radar footprint, scene coords

    # --- geometry helpers -------------------------------------------------

    @staticmethod
    def _path_from_polylines(polylines) -> QPainterPath:
        path = QPainterPath()
        for pl in polylines:
            pl = np.asarray(pl)
            if pl.ndim != 2 or pl.shape[0] < 2:
                continue
            xs = pl[:, 0] * _SX
            ys = -pl[:, 1]
            path.moveTo(float(xs[0]), float(ys[0]))
            for i in range(1, len(xs)):
                path.lineTo(float(xs[i]), float(ys[i]))
        return path

    def _polygon_path(self, poly) -> QPainterPath:
        return self._path_from_polylines([np.asarray(poly.exterior.coords)])

    # --- layers -----------------------------------------------------------

    def set_basemap(self, shapefile_dir=None) -> None:
        for it in self._basemap_items:
            self.scene.removeItem(it)
        self._basemap_items.clear()
        layers = basemap_mod.read_all_layers(shapefile_dir)
        for name, polylines in layers.items():
            if not polylines:
                continue
            style = LAYER_STYLE.get(name, {"color": (0.6, 0.6, 0.6), "line_width": 1.0})
            item = QGraphicsPathItem(self._path_from_polylines(polylines))
            pen = QPen(_qcolor(style["color"]))
            pen.setWidthF(style["line_width"])
            pen.setCosmetic(True)  # constant pixel width regardless of zoom
            item.setPen(pen)
            item.setZValue(_Z_BASEMAP)
            self.scene.addItem(item)
            self._basemap_items.append(item)
        log.info("map.basemap_built", layers=[k for k, v in layers.items() if v])

    def show_warning(self, warning: Warning) -> None:
        if self._warning_item is not None:
            self.scene.removeItem(self._warning_item)
        item = QGraphicsPathItem(self._polygon_path(warning.polygon))
        pen = QPen(QColor("#ffffff"))
        pen.setWidthF(2.5)
        pen.setCosmetic(True)
        item.setPen(pen)
        item.setBrush(QBrush(Qt.NoBrush))
        item.setZValue(_Z_WARNING)
        self.scene.addItem(item)
        self._warning_item = item

    def set_radar(self, volume: GriddedVolume) -> None:
        if self._radar_item is not None:
            self.scene.removeItem(self._radar_item)
        rgba, (lat_min, lon_min, lat_max, lon_max) = radar_render.composite_lonlat_image(
            volume
        )
        h, w, _ = rgba.shape
        buf = np.ascontiguousarray(rgba)
        img = QImage(buf.data, w, h, 4 * w, QImage.Format_RGBA8888)
        self._radar_img = img  # keep buffer ref; QImage does not copy
        self._radar_buf = buf
        item = QGraphicsPixmapItem(QPixmap.fromImage(img))
        item.setTransformationMode(Qt.SmoothTransformation)
        item.setOpacity(0.78)
        item.setZValue(_Z_RADAR)
        # Map pixel (0,0)=NW=(lon_min, lat_max) → scene; scale to the footprint.
        from PySide6.QtGui import QTransform

        t = QTransform()
        t.translate(lon_min * _SX, -lat_max)
        t.scale((lon_max - lon_min) * _SX / w, (lat_max - lat_min) / h)
        item.setTransform(t)
        self.scene.addItem(item)
        self._radar_item = item
        self._extent = QRectF(
            QPointF(lon_min * _SX, -lat_max), QPointF(lon_max * _SX, -lat_min)
        ).normalized()

    def add_cells(self, cells: list[StormCell]) -> None:
        for it in self._cell_items:
            self.scene.removeItem(it)
        self._cell_items.clear()
        pen = QPen(QColor("#ffff00"))
        pen.setWidthF(2.0)
        pen.setCosmetic(True)
        for c in cells:
            item = QGraphicsPathItem(self._polygon_path(c.envelope))
            item.setPen(pen)
            item.setBrush(QBrush(Qt.NoBrush))
            item.setZValue(_Z_CELLS)
            self.scene.addItem(item)
            self._cell_items.append(item)

    def highlight_cell(self, cell: StormCell) -> None:
        if self._highlight_item is not None:
            self.scene.removeItem(self._highlight_item)
        item = QGraphicsPathItem(self._polygon_path(cell.envelope))
        pen = QPen(QColor("#ffffff"))
        pen.setWidthF(4.0)
        pen.setCosmetic(True)
        item.setPen(pen)
        item.setBrush(QBrush(Qt.NoBrush))
        item.setZValue(_Z_HIGHLIGHT)
        self.scene.addItem(item)
        self._highlight_item = item
        # Frame the selected cell with a little context.
        lon0, lat0, lon1, lat1 = cell.envelope.bounds
        self._fit_lonlat(lon0, lat0, lon1, lat1, pad_frac=1.5)

    # --- composite operations --------------------------------------------

    def show_result(self, warning: Warning, volume: GriddedVolume, cells) -> None:
        cells = list(cells)
        log.info("map.show_result", valid_time=volume.valid_time.isoformat(),
                 n_cells=len(cells))
        self.show_warning(warning)
        self.set_radar(volume)
        self.add_cells(cells)
        self.fit_to_warning(warning)

    def show_cell_selection(
        self, warning: Warning, volume: GriddedVolume, cells, cell: StormCell
    ) -> None:
        cells = list(cells)
        log.info("map.select_cell", valid_time=volume.valid_time.isoformat(),
                 cell_id=cell.cell_id)
        self.show_warning(warning)
        self.set_radar(volume)
        self.add_cells(cells)
        self.highlight_cell(cell)

    # --- framing ----------------------------------------------------------

    def _fit_lonlat(self, lon0, lat0, lon1, lat1, pad_frac: float = 0.25) -> None:
        x0, x1 = sorted([lon0 * _SX, lon1 * _SX])
        y0, y1 = sorted([-lat0, -lat1])
        dx = max(x1 - x0, 1e-3)
        dy = max(y1 - y0, 1e-3)
        rect = QRectF(x0 - dx * pad_frac, y0 - dy * pad_frac,
                      dx * (1 + 2 * pad_frac), dy * (1 + 2 * pad_frac))
        self.view.fitInView(rect, Qt.KeepAspectRatio)

    def fit_to_warning(self, warning: Warning) -> None:
        lon0, lat0, lon1, lat1 = warning.polygon.bounds
        self._fit_lonlat(lon0, lat0, lon1, lat1, pad_frac=0.35)

    def recenter(self, lon: float, lat: float, span_deg: float = 1.5) -> None:
        self._fit_lonlat(lon - span_deg, lat - span_deg,
                         lon + span_deg, lat + span_deg, pad_frac=0.0)

    def fit_view(self) -> None:
        if self._extent is not None:
            self.view.fitInView(self._extent, Qt.KeepAspectRatio)

    def clear(self) -> None:
        for it in (self._cell_items + [self._radar_item, self._warning_item,
                                       self._highlight_item]):
            if it is not None:
                self.scene.removeItem(it)
        self._cell_items.clear()
        self._radar_item = None
        self._warning_item = None
        self._highlight_item = None
        self._extent = None
