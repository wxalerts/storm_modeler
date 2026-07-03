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


def _cloudtop_features(cloudtops) -> dict:
    """GeoJSON for cloud-top cells: anvil footprints + coldest-pixel markers.

    Each cell yields its envelope polygon (flagged ``anvil`` when anviled out)
    and a point at the coldest pixel, carrying BT and tilt in the properties
    for map popups.
    """
    feats = []
    for c in cloudtops:
        props = {
            "min_bt_k": round(c.min_bt_k, 1),
            "mean_bt_k": round(c.mean_bt_k, 1),
            "area_km2": round(c.area_km2, 0),
            "track_id": c.track_id,
            "anvil": bool(c.anviled_out),
        }
        import math as _math
        if not _math.isnan(c.tilt_km):
            props["tilt_km"] = round(c.tilt_km, 1)
            props["tilt_bearing_deg"] = round(c.tilt_bearing_deg, 0)
            props["radar_track_id"] = c.radar_track_id
        feats.append({"type": "Feature",
                      "properties": {**props, "kind": "anvil" if c.anviled_out else "cloud"},
                      "geometry": mapping(c.envelope)})
        feats.append({"type": "Feature",
                      "properties": {**props, "kind": "core"},
                      "geometry": {"type": "Point", "coordinates": [c.cold_lon, c.cold_lat]}})
    return {"type": "FeatureCollection", "features": feats}


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
        if self.proc is None or self.proc.poll() is not None:
            return
        # Close stdin first: the child sees EOF, persists its window geometry,
        # and quits on its own (see map_window_gtk.main). Give it a moment to do
        # so before falling back to signals — otherwise we'd kill it mid-save.
        try:
            self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=2.0)
            return
        except Exception:  # noqa: BLE001 - didn't exit in time; escalate
            pass
        # Still alive: SIGTERM (the child also saves geometry on SIGTERM), then
        # SIGKILL as a last resort so we never leave the map process orphaned.
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2.0)
        except Exception:  # noqa: BLE001
            self.proc.kill()

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

    # --- lightning --------------------------------------------------------

    def show_lightning(self, flashes, span=None) -> None:
        """Layer GLM flashes as markers, coloured oldest→newest in the window.

        Sent compactly as ``[lat, lon, t_frac]`` triples (t_frac in [0,1]) so
        thousands of points stay a small payload. ``span`` is an optional
        ``(t0, t1)`` of POSIX seconds to normalise the colour ramp against — pass
        the whole event's span so a single volume's slice keeps a consistent
        colour as the user steps through volumes (instead of each slice
        re-spanning blue→red on its own).
        """
        flashes = list(flashes)
        if not flashes:
            self._send({"cmd": "lightning", "points": []})
            return
        if span is not None:
            t0, t1 = span
        else:
            ts = [f.time.timestamp() for f in flashes]
            t0, t1 = min(ts), max(ts)
        width = (t1 - t0) or 1.0
        points = [[round(f.lat, 4), round(f.lon, 4),
                   round(min(1.0, max(0.0, (f.time.timestamp() - t0) / width)), 3)]
                  for f in flashes]
        self._send({"cmd": "lightning", "points": points})

    def clear_lightning(self) -> None:
        self._send({"cmd": "lightning_clear"})

    # --- satellite (GOES ABI cloud tops) ----------------------------------

    def show_satellite(self, scene, cloudtops) -> None:
        """Overlay a scene's brightness-temperature image + cloud-top markers.

        The scene is already a regular lon/lat raster (row 0 = north), so its BT
        colorizes straight to an image overlay; cloud-top cells become GeoJSON —
        a coldest-pixel marker per cell (styled as an overshooting top when
        flagged), the anvil footprint polygon when anviled out, and tilt details
        in the feature properties for popups.
        """
        rgba = radar_render.bt_to_rgba(scene.bt)
        lat_min, lon_min, lat_max, lon_max = scene.geo_bounds()
        self._send({"cmd": "satellite", "url": _rgba_data_url(rgba),
                    "bounds": [[lat_min, lon_min], [lat_max, lon_max]],
                    "time": scene.valid_time.strftime("%H:%MZ"),
                    "satellite": scene.satellite})
        self._send({"cmd": "cloudtops",
                    "geojson": _cloudtop_features(cloudtops)})

    def clear_satellite(self) -> None:
        self._send({"cmd": "satellite_clear"})

    def set_satellite_opacity(self, value: float) -> None:
        """Set the brightness-temperature overlay opacity (0.0–1.0)."""
        self._send({"cmd": "satellite_opacity", "value": float(value)})

    # --- storm tracks -----------------------------------------------------

    def show_storm_track(self, track) -> None:
        """Draw a storm track: a polyline through its history with a dot per
        volume (overshooting-top volumes emphasised), and centre on its head.

        ``track`` is a :class:`storm_modeler.tracks.StormTrack`. The geometry is
        sent as GeoJSON — a single ``LineString`` plus one ``Point`` per sample,
        carrying time/dBZ/OT in the properties for map popups.
        """
        samples = track.samples
        feats = []
        if len(samples) >= 2:
            feats.append({
                "type": "Feature", "properties": {"kind": "line"},
                "geometry": {"type": "LineString",
                             "coordinates": [[s.seed_lon, s.seed_lat]
                                             for s in samples]},
            })
        for s in samples:
            feats.append({
                "type": "Feature",
                "properties": {
                    "kind": "ot" if s.overshooting_top else "pt",
                    "time": s.valid_time.strftime("%H:%MZ"),
                    "dbz": round(s.max_dbz, 1),
                    "top_km": round(s.echo_top_km, 1),
                },
                "geometry": {"type": "Point",
                             "coordinates": [s.seed_lon, s.seed_lat]},
            })
        self._send({"cmd": "stormtrack",
                    "geojson": {"type": "FeatureCollection", "features": feats}})
        last = samples[-1]
        self._send({"cmd": "pan", "lat": last.seed_lat, "lon": last.seed_lon})

    def clear_stormtrack(self) -> None:
        self._send({"cmd": "stormtrack_clear"})

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
