"""Self-rendered radar layer.

Turns a gridded volume's composite (column-max) reflectivity into a geo-aligned
RGBA mesh, coloured with the standard NWS reflectivity table. This is rendered
directly from the gridded volume — there is no tile server anywhere in the
pipeline.

The mesh is a curvilinear structured grid whose nodes are the volume grid
points reprojected to lon/lat, so it overlays the vector basemap and the cell
envelopes exactly.
"""

from __future__ import annotations

import numpy as np

from ..models import GriddedVolume

# Standard NWS reflectivity colour table: (dbz_lower_bound, (r, g, b)).
NWS_DBZ_TABLE: list[tuple[float, tuple[int, int, int]]] = [
    (5, (4, 233, 231)),
    (10, (1, 159, 244)),
    (15, (3, 0, 244)),
    (20, (2, 253, 2)),
    (25, (1, 197, 1)),
    (30, (0, 142, 0)),
    (35, (253, 248, 2)),
    (40, (229, 188, 0)),
    (45, (253, 149, 0)),
    (50, (253, 0, 0)),
    (55, (212, 0, 0)),
    (60, (188, 0, 0)),
    (65, (248, 0, 253)),
    (70, (152, 84, 198)),
    (75, (255, 255, 255)),
]


def dbz_to_rgba(dbz: np.ndarray, min_dbz: float = 5.0) -> np.ndarray:
    """Map a dBZ field to an (..., 4) uint8 RGBA array (transparent < min)."""
    dbz = np.asarray(dbz, dtype=np.float32)
    out = np.zeros(dbz.shape + (4,), dtype=np.uint8)
    bounds = np.array([b for b, _ in NWS_DBZ_TABLE], dtype=np.float32)
    colors = np.array([c for _, c in NWS_DBZ_TABLE], dtype=np.uint8)

    valid = np.isfinite(dbz) & (dbz >= min_dbz)
    idx = np.clip(np.searchsorted(bounds, dbz, side="right") - 1, 0, len(bounds) - 1)
    rgb = colors[idx]
    out[..., :3] = rgb
    out[..., 3] = np.where(valid, 255, 0)
    out[~valid, :3] = 0
    return out


def composite_rgba(volume: GriddedVolume) -> np.ndarray:
    """RGBA image (ny, nx, 4) of the column-max reflectivity."""
    comp = volume.composite_reflectivity()
    return dbz_to_rgba(comp)


def radar_polydata(volume: GriddedVolume, z: float = 0.0):
    """A geo-aligned ``pyvista.StructuredGrid`` carrying per-point RGBA.

    Nodes are the volume's grid points reprojected to lon/lat at height ``z``.
    Use with ``plotter.add_mesh(mesh, scalars='rgba', rgba=True)``.
    """
    import pyvista as pv

    comp = volume.composite_reflectivity()  # (ny, nx)
    ny, nx = comp.shape
    xx, yy = np.meshgrid(volume.x, volume.y)  # metres
    lon, lat = volume.xy_to_lonlat(xx.ravel(), yy.ravel())
    lon = np.asarray(lon).reshape(ny, nx)
    lat = np.asarray(lat).reshape(ny, nx)
    zz = np.full((ny, nx), z)

    grid = pv.StructuredGrid(lon, lat, zz)
    rgba = dbz_to_rgba(comp).reshape(-1, 4)
    grid.point_data["rgba"] = rgba
    grid.point_data["dbz"] = np.nan_to_num(comp.ravel(), nan=-30.0)
    return grid


def geo_bounds(volume: GriddedVolume) -> tuple[float, float, float, float]:
    """(lon_min, lon_max, lat_min, lat_max) of the volume footprint."""
    xx, yy = np.meshgrid(
        volume.x[[0, -1]], volume.y[[0, -1]]
    )
    lon, lat = volume.xy_to_lonlat(xx.ravel(), yy.ravel())
    lon = np.asarray(lon)
    lat = np.asarray(lat)
    return float(lon.min()), float(lon.max()), float(lat.min()), float(lat.max())
