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


def bt_to_rgba(bt_k: np.ndarray, vmin: float = 180.0, vmax: float = 300.0) -> np.ndarray:
    """Map an IR brightness-temperature field (Kelvin) to (..., 4) uint8 RGBA.

    Cold cloud tops read bright/saturated and warm scenes near-transparent, the
    usual IR enhancement: a reversed ``turbo`` ramp over ``[vmin, vmax]`` (so the
    coldest tops pop), with off-disk/NaN pixels fully transparent. Mirrors
    ``_colorize``'s matplotlib branch.
    """
    import matplotlib

    bt = np.asarray(bt_k, dtype=np.float32)
    cmap = matplotlib.colormaps["turbo_r"]
    norm = (bt - vmin) / (vmax - vmin)
    rgba = (cmap(np.clip(norm, 0.0, 1.0)) * 255).astype(np.uint8)
    finite = np.isfinite(bt)
    rgba[..., 3] = np.where(finite, 255, 0)
    rgba[~finite, :3] = 0
    return rgba


def radar_polydata(volume: GriddedVolume, z: float = 0.0):
    """A geo-aligned ``pyvista.StructuredGrid`` carrying per-point RGBA.

    Nodes are the volume's grid points reprojected to lon/lat at height ``z``.
    Use with ``plotter.add_mesh(mesh, scalars='rgba', rgba=True)``.
    """
    import pyvista as pv

    comp = volume.composite_reflectivity()  # (ny, nx)
    ny, nx = comp.shape
    xx, yy = np.meshgrid(volume.x, volume.y)  # metres east/north of the radar
    # Radar coverage is a disc centred on the site, not the square analysis grid.
    # Blank everything beyond the largest circle the grid fully contains so the
    # display reads as a NEXRAD range ring rather than a filled box.
    rmax = min(volume.x.max(), -volume.x.min(), volume.y.max(), -volume.y.min())
    comp = np.where(np.hypot(xx, yy) <= rmax, comp, np.nan)
    lon, lat = volume.xy_to_lonlat(xx.ravel(), yy.ravel())
    lon = np.asarray(lon).reshape(ny, nx)
    lat = np.asarray(lat).reshape(ny, nx)
    zz = np.full((ny, nx), z)

    grid = pv.StructuredGrid(lon, lat, zz)
    rgba = dbz_to_rgba(comp).reshape(-1, 4)
    grid.point_data["rgba"] = rgba
    grid.point_data["dbz"] = np.nan_to_num(comp.ravel(), nan=-30.0)
    return grid


# --- product registry -------------------------------------------------------

#: Per-product display metadata: matplotlib colormap + value range. Reflectivity
#: uses the hand NWS table instead (``dbz_to_rgba``). Order = the cycle order.
PRODUCTS: list[dict] = [
    {"key": "reflectivity", "label": "Reflectivity", "units": "dBZ"},
    {"key": "velocity", "label": "Velocity", "units": "m/s",
     "cmap": "RdYlGn_r", "vmin": -32.0, "vmax": 32.0},
    {"key": "spectrum_width", "label": "Spectrum Width", "units": "m/s",
     "cmap": "plasma", "vmin": 0.0, "vmax": 14.0},
    {"key": "differential_reflectivity", "label": "ZDR", "units": "dB",
     "cmap": "Spectral_r", "vmin": -2.0, "vmax": 6.0},
    {"key": "cross_correlation_ratio", "label": "Corr. Coeff (CC)", "units": "",
     "cmap": "turbo", "vmin": 0.2, "vmax": 1.02},
    {"key": "differential_phase", "label": "Diff. Phase", "units": "deg",
     "cmap": "viridis", "vmin": 0.0, "vmax": 360.0},
]
_PRODUCT_BY_KEY = {p["key"]: p for p in PRODUCTS}


def _colorize(field2d: np.ndarray, product: str) -> np.ndarray:
    """Map a 2D field to RGBA per the product's colormap (transparent where NaN)."""
    if product == "reflectivity":
        return dbz_to_rgba(field2d)
    import matplotlib

    meta = _PRODUCT_BY_KEY[product]
    cmap = matplotlib.colormaps[meta["cmap"]]
    norm = (field2d - meta["vmin"]) / (meta["vmax"] - meta["vmin"])
    rgba = (cmap(np.clip(norm, 0.0, 1.0)) * 255).astype(np.uint8)
    rgba[..., 3] = np.where(np.isfinite(field2d), 255, 0)
    rgba[~np.isfinite(field2d), :3] = 0
    return rgba


def product_lonlat_image(
    volume: GriddedVolume, product: str = "reflectivity",
    width: int = 768, height: int = 768,
) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    """Warp a product's 2D layer onto a regular lon/lat raster, coloured.

    Returns ``(rgba, (lat_min, lon_min, lat_max, lon_max))`` — an ``(h, w, 4)``
    uint8 image, row 0 = north. The layer is placed on the map by these lat/lon
    bounds and the cells/warning come from the same lon/lat, so they align. The
    aeqd grid is resampled by inverse-projecting each target lon/lat pixel back
    to local metres and bilinearly sampling there.
    """
    from scipy.interpolate import RegularGridInterpolator

    field2d = volume.product_2d(product)
    if field2d is None:
        field2d = volume.composite_reflectivity()
        product = "reflectivity"

    # Blank everything outside the radar's range disc (matches the NEXRAD look).
    xx, yy = np.meshgrid(volume.x, volume.y)
    rmax = min(volume.x.max(), -volume.x.min(), volume.y.max(), -volume.y.min())
    field2d = np.where(np.hypot(xx, yy) <= rmax, field2d, np.nan)

    lon_min, lon_max, lat_min, lat_max = geo_bounds(volume)
    lons = np.linspace(lon_min, lon_max, width)
    lats = np.linspace(lat_max, lat_min, height)  # row 0 = north
    LON, LAT = np.meshgrid(lons, lats)
    x_q, y_q = volume.lonlat_to_xy(LON.ravel(), LAT.ravel())

    interp = RegularGridInterpolator(
        (volume.y, volume.x), field2d, bounds_error=False, fill_value=np.nan
    )
    sampled = interp(np.column_stack([np.asarray(y_q), np.asarray(x_q)]))
    rgba = _colorize(sampled.reshape(height, width), product)
    return rgba, (float(lat_min), float(lon_min), float(lat_max), float(lon_max))


def composite_lonlat_image(
    volume: GriddedVolume, width: int = 768, height: int = 768
) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    """Back-compat: the reflectivity composite as a lon/lat raster."""
    return product_lonlat_image(volume, "reflectivity", width, height)


def geo_bounds(volume: GriddedVolume) -> tuple[float, float, float, float]:
    """(lon_min, lon_max, lat_min, lat_max) of the volume footprint."""
    xx, yy = np.meshgrid(
        volume.x[[0, -1]], volume.y[[0, -1]]
    )
    lon, lat = volume.xy_to_lonlat(xx.ravel(), yy.ravel())
    lon = np.asarray(lon)
    lat = np.asarray(lat)
    return float(lon.min()), float(lon.max()), float(lat.min()), float(lat.max())
