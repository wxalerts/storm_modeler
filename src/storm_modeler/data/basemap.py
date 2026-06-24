"""Vector basemap: shipped vector files → VTK polyline meshes.

Reads the shipped counties / states / highways vector layers (GeoJSON under
``data/shapefiles``; ``.shp`` is also supported when GeoPandas is installed) and
turns each into a flat ``pyvista.PolyData`` line mesh at ``z=0`` for the
top-down map. Everything is rendered in planar lon/lat space so the basemap,
the self-rendered radar layer, and the cell envelopes are all geo-aligned.

No tile server is involved anywhere — these are local vector lines only.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..config import SHAPEFILE_DIR

LAYER_FILES = {
    "states": ("states.geojson", "states.shp"),
    "counties": ("counties.geojson", "counties.shp"),
    "highways": ("highways.geojson", "highways.shp"),
}

# Render colours / line widths per layer for the map pane.
LAYER_STYLE = {
    "states": {"color": (0.85, 0.85, 0.90), "line_width": 2.0},
    "counties": {"color": (0.45, 0.45, 0.50), "line_width": 1.0},
    "highways": {"color": (0.80, 0.55, 0.20), "line_width": 1.5},
}


def _coords_from_geometry(geom: dict) -> list[np.ndarray]:
    """Flatten a GeoJSON geometry into a list of (N,2) lon/lat polylines."""
    t = geom.get("type")
    c = geom.get("coordinates")
    out: list[np.ndarray] = []
    if t == "LineString":
        out.append(np.asarray(c, dtype=float))
    elif t in ("MultiLineString", "Polygon"):
        for part in c:
            out.append(np.asarray(part, dtype=float))
    elif t == "MultiPolygon":
        for poly in c:
            for ring in poly:
                out.append(np.asarray(ring, dtype=float))
    elif t == "Point":
        out.append(np.asarray([c], dtype=float))
    return [a for a in out if a.ndim == 2 and a.shape[0] >= 2]


def _read_geojson(path: Path) -> list[np.ndarray]:
    data = json.loads(path.read_text())
    feats = data.get("features", []) if data.get("type") == "FeatureCollection" else [data]
    polylines: list[np.ndarray] = []
    for f in feats:
        geom = f.get("geometry", f)
        polylines.extend(_coords_from_geometry(geom))
    return polylines


def _read_shapefile(path: Path) -> list[np.ndarray]:
    import geopandas as gpd  # type: ignore
    from shapely.geometry import mapping

    gdf = gpd.read_file(path).to_crs("EPSG:4326")
    polylines: list[np.ndarray] = []
    for geom in gdf.geometry:
        polylines.extend(_coords_from_geometry(mapping(geom)))
    return polylines


def read_layer(name: str, shapefile_dir: Path | None = None) -> list[np.ndarray]:
    """Return the polylines (lon/lat) for one basemap layer, or []."""
    d = Path(shapefile_dir or SHAPEFILE_DIR)
    gj, shp = LAYER_FILES[name]
    if (d / gj).exists():
        return _read_geojson(d / gj)
    if (d / shp).exists():
        try:
            return _read_shapefile(d / shp)
        except Exception:  # pragma: no cover - missing geopandas
            return []
    return []


def read_all_layers(shapefile_dir: Path | None = None) -> dict[str, list[np.ndarray]]:
    return {name: read_layer(name, shapefile_dir) for name in LAYER_FILES}


def polylines_to_polydata(polylines: list[np.ndarray], z: float = 0.0):
    """Build a single ``pyvista.PolyData`` of all polylines at height ``z``.

    Points are (lon, lat, z); cells are line segments. Returns ``None`` if there
    is nothing to draw.
    """
    import pyvista as pv

    if not polylines:
        return None
    points = []
    lines = []
    offset = 0
    for pl in polylines:
        n = pl.shape[0]
        zcol = np.full((n, 1), z)
        points.append(np.hstack([pl[:, :2], zcol]))
        seg = [n] + list(range(offset, offset + n))
        lines.extend(seg)
        offset += n
    pts = np.vstack(points)
    poly = pv.PolyData()
    poly.points = pts
    poly.lines = np.asarray(lines)
    return poly
