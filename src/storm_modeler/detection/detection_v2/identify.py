"""SCIT identification — gridded volume in, storm cells out.

Phase-1 guarantee (carried from the SCIT spec): anomalous propagation never
seeds. A seed candidate is admitted only when it shows genuine vertical
structure — presence across several grid levels, a minimum echo depth, and a
minimum echo top. Ground clutter (high reflectivity confined to the lowest
level) fails every one of those gates and is dropped.
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage
from shapely.geometry import MultiPoint, Polygon

from ...config import ScitConfig
from ...models import GriddedVolume
from .types import StormCell


def _footprint_polygon(volume: GriddedVolume, ys: np.ndarray, xs: np.ndarray) -> Polygon:
    """Convex hull of the footprint cell centres, in lon/lat."""
    xm = volume.x[xs]
    ym = volume.y[ys]
    lon, lat = volume.xy_to_lonlat(xm, ym)
    pts = list(zip(np.atleast_1d(lon), np.atleast_1d(lat)))
    hull = MultiPoint(pts).convex_hull
    if isinstance(hull, Polygon):
        return hull
    # Degenerate (collinear / single point): give it a small footprint so it
    # still renders and persists as a polygon.
    return hull.buffer(0.01)


def identify(volume: GriddedVolume, config: ScitConfig | None = None) -> list[StormCell]:
    """Identify admitted storm cells in a single gridded volume.

    Deterministic: the same volume always yields the same cells in the same
    order (sorted by descending peak reflectivity, then footprint area).
    """
    config = config or ScitConfig()
    refl = volume.reflectivity  # (nz, ny, nx)
    nz, ny, nx = refl.shape
    dz_km = volume.dz_km or 0.5
    cell_area_km2 = volume.dx_km * volume.dx_km

    seed_mask = np.nan_to_num(refl, nan=-9999.0) >= config.seed_dbz
    if not seed_mask.any():
        return []

    # 6-connected 3D labelling: avoid diagonal bridging of distinct cells.
    structure = ndimage.generate_binary_structure(3, 1)
    labels, n = ndimage.label(seed_mask, structure=structure)

    # Echo-top mask spans the whole column (captures anvil above the core).
    top_mask = np.nan_to_num(refl, nan=-9999.0) >= config.echo_top_dbz

    candidates: list[StormCell] = []
    for lab in range(1, n + 1):
        region = labels == lab
        zc, yc, xc = np.nonzero(region)
        level_set = np.unique(zc)
        n_levels = int(level_set.size)

        # Footprint columns (any seed voxel in the column).
        foot = region.any(axis=0)  # (ny, nx)
        ys_foot, xs_foot = np.nonzero(foot)
        area_km2 = float(ys_foot.size) * cell_area_km2

        # Echo top / base from the broader echo-top mask over the footprint.
        col_top = top_mask[:, foot]  # (nz, n_footprint)
        z_present = np.nonzero(col_top.any(axis=1))[0]
        if z_present.size == 0:
            continue
        k_top = int(z_present.max())
        k_base = int(z_present.min())
        echo_top_km = float(volume.z[k_top]) / 1000.0
        base_km = float(volume.z[k_base]) / 1000.0
        depth_km = (float(volume.z[k_top]) - float(volume.z[k_base])) / 1000.0 + dz_km

        max_dbz = float(np.nanmax(refl[region]))

        # Reflectivity-weighted footprint centroid (composite column max).
        # Use -inf outside the region so empty columns never trip an
        # all-NaN max; footprint columns always hold >=1 finite voxel.
        comp = np.where(region, np.nan_to_num(refl, nan=-np.inf), -np.inf).max(axis=0)
        w = comp[foot]
        w = np.clip(np.where(np.isfinite(w), w, 0.0), 0.0, None)
        if w.sum() <= 0:
            w = np.ones_like(w)
        seed_x = float(np.average(volume.x[xs_foot], weights=w))
        seed_y = float(np.average(volume.y[ys_foot], weights=w))
        seed_lon, seed_lat = volume.xy_to_lonlat(np.array([seed_x]), np.array([seed_y]))
        seed_lon = float(np.atleast_1d(seed_lon)[0])
        seed_lat = float(np.atleast_1d(seed_lat)[0])

        # --- Admission gates: this is what rejects AP --------------------
        admitted = (
            n_levels >= config.min_levels
            and depth_km >= config.min_depth_km
            and echo_top_km >= config.min_echo_top_km
            and area_km2 >= config.min_area_km2
        )
        if not admitted:
            continue

        envelope = _footprint_polygon(volume, ys_foot, xs_foot)

        candidates.append(
            StormCell(
                cell_id=-1,  # assigned after sorting
                site=volume.site,
                valid_time=volume.valid_time,
                seed_lon=seed_lon,
                seed_lat=seed_lat,
                seed_x=seed_x,
                seed_y=seed_y,
                max_dbz=max_dbz,
                area_km2=area_km2,
                echo_top_km=echo_top_km,
                base_km=base_km,
                depth_km=depth_km,
                n_levels=n_levels,
                envelope=envelope,
            )
        )

    # Deterministic ordering and ids.
    candidates.sort(key=lambda c: (-c.max_dbz, -c.area_km2, c.seed_x, c.seed_y))
    for i, c in enumerate(candidates):
        c.cell_id = i + 1
    return candidates
