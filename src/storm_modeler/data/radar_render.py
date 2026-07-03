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


#: NWS default velocity color table — the AWIPS baseline 8-bit ``Vel.cmap``
#: ("8-bit Vel.cmap", Unidata awips2 @ unidata_17.1.1, colormaps/Radar/),
#: shipped as a checked-in asset and regenerated reproducibly by
#: ``python -m storm_modeler.tools.build_awips_vel_lut``. 256 RGBA rows in
#: [0, 1]:
#:
#: * index 0 — no data (fully transparent; our NaN gates),
#: * index 1 — range folded, purple #7F00CF (fed by the range_folded layer),
#: * indices 2–255 — the linear ramp magenta → purple-blue → cyan → green
#:   (inbound) → desaturated gray at center (zero) → red → orange → pale
#:   yellow → white (outbound). The extremes deliberately break out of pure
#:   green/red so a violent couplet visually saturates.
from ..config import PACKAGE_ROOT

AWIPS_VEL_LUT: np.ndarray = np.load(
    PACKAGE_ROOT / "assets" / "awips_vel_lut.npz"
)["rgba_float"]  # (256, 4) float in [0, 1]
_VEL_LUT_U8: np.ndarray = np.round(AWIPS_VEL_LUT * 255.0).astype(np.uint8)
NWS_VEL_TABLE: np.ndarray = _VEL_LUT_U8[:, :3]  # RGB view (markers, overlays)
VEL_RF_INDEX = 1  # range-folded entry


def vel_to_rgba(
    vel: np.ndarray,
    vmin: float = -32.0,
    vmax: float = 32.0,
    range_folded: np.ndarray | None = None,
) -> np.ndarray:
    """Map a radial-velocity field (m/s) to (..., 4) uint8 RGBA via ``Vel.cmap``.

    NaN gates take index 0 (transparent), ``range_folded`` gates index 1 (RF
    purple), and finite velocities map linearly onto indices 2–255 so that a
    symmetric ``[vmin, vmax]`` puts 0 m/s at the ramp's desaturated gray
    center. Out-of-range velocities clamp to the magenta/white extremes.
    """
    vel = np.asarray(vel, dtype=np.float32)
    finite = np.isfinite(vel)
    with np.errstate(invalid="ignore"):
        norm = (vel - vmin) / (vmax - vmin)
        idx = 2 + np.round(np.clip(norm, 0.0, 1.0) * 253.0)
    idx = np.where(finite, idx, 0).astype(np.uint8)
    # RF wins before the alpha pass: the LUT's alpha column (0 only at index
    # 0) then makes RF/data opaque and no-data transparent in one lookup.
    if range_folded is not None:
        idx = np.where(range_folded, np.uint8(VEL_RF_INDEX), idx)
    return _VEL_LUT_U8[idx]


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


# --- storm-relative velocity -------------------------------------------------

#: Motion (u_ms, v_ms) each volume's cached SRM layer was derived with, stored
#: as a plain attribute on the volume (not a dataclass field, so ``save_npz``
#: never persists it and the on-disk cache format is untouched).
_SRM_MOTION_ATTR = "_srm_motion_uv"
#: Motion change (m/s, either component) below which the cached layer is kept.
_SRM_MOTION_TOL = 0.1


def derive_srm(volume: GriddedVolume, u_ms: float, v_ms: float) -> np.ndarray:
    """Storm-relative radial velocity: base velocity minus the storm-motion
    projection onto each radial. NaN-preserving.

    The radar sits at the grid origin, so the radial unit vector at (x, y) is
    simply ``(x, y)/r`` — no azimuth trig. ``r`` is floored at 1 m so the
    origin cell never divides by zero. Deterministic for a given (volume,
    motion); dtype follows the base layer's promotion (the caller casts for
    display storage).
    """
    v_base = volume.product_2d("velocity")
    if v_base is None:
        raise ValueError("volume has no velocity layer to derive SRM from")
    xx, yy = np.meshgrid(volume.x, volume.y)
    r = np.maximum(np.hypot(xx, yy), 1.0)
    return v_base - (float(u_ms) * xx + float(v_ms) * yy) / r


def ensure_srm_layer(
    volume: GriddedVolume, u_ms: float, v_ms: float
) -> np.ndarray | None:
    """Compute-and-stash the SRM display layer for ``volume`` (idempotent).

    Writes ``products["storm_relative_velocity"]`` and records the motion used
    on the volume object; a repeat call recomputes only when either motion
    component moved by more than 0.1 m/s. Returns the layer, or ``None`` when
    the volume carries no base velocity (older cached volumes) — the map then
    falls back to reflectivity via its usual missing-product path.
    """
    if volume.product_2d("velocity") is None:
        return None
    prev = getattr(volume, _SRM_MOTION_ATTR, None)
    if (
        prev is not None
        and "storm_relative_velocity" in volume.products
        and abs(prev[0] - u_ms) <= _SRM_MOTION_TOL
        and abs(prev[1] - v_ms) <= _SRM_MOTION_TOL
    ):
        return volume.products["storm_relative_velocity"]
    layer = derive_srm(volume, u_ms, v_ms).astype(np.float32)
    volume.products["storm_relative_velocity"] = layer
    setattr(volume, _SRM_MOTION_ATTR, (float(u_ms), float(v_ms)))
    return layer


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
#: and velocity use the hand NWS tables instead (``dbz_to_rgba`` /
#: ``vel_to_rgba``). Order = the cycle order.
PRODUCTS: list[dict] = [
    {"key": "reflectivity", "label": "Reflectivity", "units": "dBZ"},
    {"key": "velocity", "label": "Velocity", "units": "m/s",
     "vmin": -32.0, "vmax": 32.0},
    # Derived, not gridded: the layer is injected per volume by
    # ensure_srm_layer once a storm motion is known. Shares the AWIPS LUT.
    {"key": "storm_relative_velocity", "label": "SRM", "units": "m/s",
     "vmin": -32.0, "vmax": 32.0},
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


def _colorize(
    field2d: np.ndarray, product: str, range_folded: np.ndarray | None = None
) -> np.ndarray:
    """Map a 2D field to RGBA per the product's colormap (transparent where NaN).

    ``range_folded`` (a boolean mask, velocity-family products only) marks
    pixels to draw as the AWIPS LUT's index-1 RF purple.
    """
    if product == "reflectivity":
        return dbz_to_rgba(field2d)
    if product in ("velocity", "storm_relative_velocity"):
        meta = _PRODUCT_BY_KEY[product]
        return vel_to_rgba(field2d, meta["vmin"], meta["vmax"], range_folded)
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
    pts = np.column_stack([np.asarray(y_q), np.asarray(x_q)])
    sampled = interp(pts)

    # Velocity carries a companion range-folded layer (see grid_level2): those
    # pixels render as the LUT's RF purple — RF gates beside a couplet are
    # themselves a signal that the velocities there are extreme. Volumes
    # cached before RF existed simply lack the layer.
    rf_mask = None
    if product in ("velocity", "storm_relative_velocity"):
        rf2d = volume.product_2d("range_folded")
        if rf2d is not None:
            rf2d = np.where(np.hypot(xx, yy) <= rmax, rf2d, 0.0)
            rf_interp = RegularGridInterpolator(
                (volume.y, volume.x), rf2d, bounds_error=False, fill_value=0.0
            )
            rf_mask = rf_interp(pts).reshape(height, width) >= 0.5

    rgba = _colorize(sampled.reshape(height, width), product, rf_mask)
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
