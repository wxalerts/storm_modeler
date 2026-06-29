"""Qt-side controller for the out-of-process GTK/WebKit Leaflet map.

The map can't be a Qt child widget (it's GTK), so it runs as a separate process
(:mod:`storm_modeler.map_window_gtk`) under the system ``python3`` that carries
the gi/WebKit2 bindings. This class launches it and drives it with newline-JSON
commands over stdin, exposing the same surface the panes call (``show_result``,
``show_cell_selection`` …) so the rest of the app is unchanged.

Radar is encoded to a PNG data URL with Qt (no Pillow); the warning and cells go
as GeoJSON — everything is placed by lon/lat in Leaflet, so it lines up.

If the subprocess can't start (no WebKitGTK), a small Qt window shows the error
(no native fallback, by design).
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import structlog
from PySide6.QtCore import QBuffer, QByteArray, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QLabel, QMainWindow
from shapely.geometry import mapping

from ..data import radar_render
from ..detection.detection_v2 import StormCell
from ..models import GriddedVolume, Warning

log = structlog.get_logger(__name__)

_GTK_SCRIPT = Path(__file__).resolve().parent.parent / "map_window_gtk.py"


def _product_label(key: str) -> str:
    for p in radar_render.PRODUCTS:
        if p["key"] == key:
            return f"{p['label']} ({p['units']})" if p["units"] else p["label"]
    return key


def _rgba_data_url(rgba: np.ndarray) -> str:
    h, w, _ = rgba.shape
    buf = np.ascontiguousarray(rgba)
    img = QImage(buf.data, w, h, 4 * w, QImage.Format_RGBA8888)
    ba = QByteArray()
    qb = QBuffer(ba)
    qb.open(QBuffer.WriteOnly)
    img.save(qb, "PNG")
    qb.close()
    return "data:image/png;base64," + base64.b64encode(bytes(ba)).decode("ascii")


def _features(geoms) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {}, "geometry": mapping(g)}
                     for g in geoms],
    }


class MapClient:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self._error_win: QMainWindow | None = None
        # Warning id we've already framed; once framed, stepping through that
        # warning's volumes keeps the user's zoom and only recenters.
        self._framed_id: str | None = None
        self._product = "reflectivity"
        self._last_volume: GriddedVolume | None = None
        self._launch()

    # --- product selection ------------------------------------------------

    def set_product(self, product: str) -> None:
        """Switch the radar layer to another moment and re-render the current
        volume (reflectivity, velocity, spectrum_width, …)."""
        self._product = product
        if self._last_volume is not None:
            self.set_radar(self._last_volume)

    # --- process management ----------------------------------------------

    def _launch(self) -> None:
        gtk_python = os.environ.get("STORM_MODELER_GTK_PYTHON", "/usr/bin/python3")
        env = dict(os.environ)
        try:  # let the map process persist its own window geometry
            from ..config import CACHE_DIR
            env["STORM_MODELER_MAP_GEOM"] = str(CACHE_DIR / "map_geometry.json")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc = subprocess.Popen(
                [gtk_python, str(_GTK_SCRIPT)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1, env=env,
            )
        except Exception as e:  # noqa: BLE001
            log.error("map.launch_failed", error=str(e))
            self._show_error(f"Could not launch the map process:\n{e}")
            return
        # Drain child output to the log (and keep the pipe from filling).
        threading.Thread(target=self._reader, daemon=True).start()
        # If WebKitGTK is missing, the child dies almost immediately — surface it.
        QTimer.singleShot(2000, self._check_alive)
        log.info("map.launched", python=gtk_python, script=str(_GTK_SCRIPT))

    def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.rstrip()
            if line:
                log.info("map.child", line=line[:300])

    def _check_alive(self) -> None:
        if self.proc is not None and self.proc.poll() is not None:
            self._show_error(
                "The map window (WebKitGTK) exited on startup.\n"
                "Install it with:\n"
                "  sudo apt install gir1.2-webkit2-4.1 libwebkit2gtk-4.1-0"
            )

    def _show_error(self, message: str) -> None:
        win = QMainWindow()
        win.setWindowTitle("Map — unavailable")
        win.resize(560, 160)
        label = QLabel(message)
        label.setStyleSheet("padding: 16px; color: #ddd;")
        label.setWordWrap(True)
        win.setCentralWidget(label)
        win.show()
        self._error_win = win

    def shutdown(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            self.proc.terminate()

    # --- command channel --------------------------------------------------

    def _send(self, obj: dict) -> None:
        if self.proc is None or self.proc.poll() is not None or not self.proc.stdin:
            return
        try:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
        except Exception as e:  # noqa: BLE001
            log.info("map.send_failed", reason=str(e).splitlines()[0])

    # --- MapPane-compatible surface --------------------------------------

    def set_basemap(self, shapefile_dir=None) -> None:
        return  # the ESRI tile layer is the basemap

    def show_warning(self, warning: Warning) -> None:
        self._send({"cmd": "warning", "geojson": mapping(warning.polygon)})

    def set_radar(self, volume: GriddedVolume) -> None:
        self._last_volume = volume
        # If the selected product wasn't gridded into this (older) volume, say so
        # rather than silently showing reflectivity.
        if self._product != "reflectivity" and volume.product_2d(self._product) is None:
            label = (f"{_product_label('reflectivity')}   "
                     f"({self._product} not in this volume — re-download)")
        else:
            label = _product_label(self._product)
        rgba, (lat_min, lon_min, lat_max, lon_max) = radar_render.product_lonlat_image(
            volume, self._product
        )
        self._send({"cmd": "radar", "url": _rgba_data_url(rgba),
                    "bounds": [[lat_min, lon_min], [lat_max, lon_max]],
                    "product": label})

    def add_cells(self, cells) -> None:
        self._send({"cmd": "cells", "geojson": _features([c.envelope for c in cells])})

    def highlight_cell(self, cell: StormCell) -> None:
        # Recenter only — keep the user's current zoom while stepping volumes.
        self._send({"cmd": "highlight", "geojson": mapping(cell.envelope)})
        self._send({"cmd": "pan", "lat": cell.seed_lat, "lon": cell.seed_lon})

    def show_result(self, warning: Warning, volume: GriddedVolume, cells) -> None:
        cells = list(cells)
        log.info("map.show_result", valid_time=volume.valid_time.isoformat(),
                 n_cells=len(cells))
        self.show_warning(warning)
        self.set_radar(volume)
        self.add_cells(cells)
        # Frame the warning once; later volumes of the same warning keep the
        # user's zoom (the warning polygon is static, so no recenter needed).
        if warning.id != self._framed_id:
            self.fit_to_warning(warning)
            self._framed_id = warning.id

    def show_cell_selection(self, warning: Warning, volume: GriddedVolume, cells,
                            cell: StormCell) -> None:
        cells = list(cells)
        self.show_warning(warning)
        self.set_radar(volume)
        self.add_cells(cells)
        self._send({"cmd": "highlight", "geojson": mapping(cell.envelope)})
        if warning.id != self._framed_id:
            # First landing on this warning: frame the cell to set the zoom.
            lon0, lat0, lon1, lat1 = cell.envelope.bounds
            self._send({"cmd": "fit", "bounds": [[lat0, lon0], [lat1, lon1]]})
            self._framed_id = warning.id
        else:
            # Stepping through volumes: keep zoom, just recenter on the cell.
            self._send({"cmd": "pan", "lat": cell.seed_lat, "lon": cell.seed_lon})

    def fit_to_warning(self, warning: Warning) -> None:
        lon0, lat0, lon1, lat1 = warning.polygon.bounds
        mlon = max((lon1 - lon0) * 0.35, 0.1)
        mlat = max((lat1 - lat0) * 0.35, 0.1)
        self._send({"cmd": "fit",
                    "bounds": [[lat0 - mlat, lon0 - mlon], [lat1 + mlat, lon1 + mlon]]})

    def recenter(self, lon: float, lat: float, span_deg: float = 1.5) -> None:
        self._send({"cmd": "fit", "bounds": [[lat - span_deg, lon - span_deg],
                                             [lat + span_deg, lon + span_deg]]})

    def fit_view(self) -> None:
        return

    def clear(self) -> None:
        self._send({"cmd": "clear"})
