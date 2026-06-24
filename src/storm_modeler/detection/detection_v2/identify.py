"""SCIT identification — gridded volume + params in, storm cells out.

Pure and testable: takes an explicit :class:`DetectionParams`, reads no globals.

Phase-1 guarantee: anomalous propagation never seeds. A seed candidate is
admitted only when it shows genuine vertical structure — presence across
``continuity_levels`` grid levels and an echo top above ``echo_top_min_km``,
measured against the ``continuity_dbz`` contour. Ground clutter (high
reflectivity confined to the lowest level) fails both gates.
"""

from __future__ import annotations

import math

import numpy as np
from scipy import ndimage
from shapely.geometry import MultiPoint, Polygon

from ...models import GriddedVolume
from ...settings.resolver import DetectionParams
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
    return hull.buffer(0.01)


def _suppress_close_seeds(cells: list[StormCell], min_sep_km: float) -> list[StormCell]:
    """Drop weaker cells whose centroid is within ``min_sep_km`` of a stronger one."""
    if min_sep_km <= 0:
        return cells
    kept: list[StormCell] = []
    # cells arrive strongest-first; greedily keep, suppressing close weaker ones.
    for c in cells:
        too_close = False
        for k in kept:
            d = math.hypot(c.seed_x - k.seed_x, c.seed_y - k.seed_y) / 1000.0
            if d < min_sep_km:
                too_close = True
                break
        if not too_close:
            kept.append(c)
    return kept


def identify(volume: GriddedVolume, params: DetectionParams | None = None) -> list[StormCell]:
    """Identify admitted storm cells in a single gridded volume.

    Deterministic: the same (volume, params) always yields the same cells in
    the same order (sorted by descending peak reflectivity, then footprint).
    """
    params = params or DetectionParams()
    refl = volume.reflectivity  # (nz, ny, nx)
    nz, ny, nx = refl.shape
    dz_km = volume.dz_km or params.grid_v_km
    cell_area_km2 = volume.dx_km * volume.dx_km

    filled = np.nan_to_num(refl, nan=-9999.0)
    seed_mask = filled >= params.seed_dbz
    if not seed_mask.any():
        return []

    base_mask2d = (filled >= params.base_dbz).any(axis=0)  # (ny, nx) footprint extent
    top_mask = filled >= params.continuity_dbz  # echo-top / continuity contour

    structure = ndimage.generate_binary_structure(3, 1)  # 6-connected
    labels, n = ndimage.label(seed_mask, structure=structure)

    # Optional watershed split of merged seed blobs by per-column peak markers.
    if params.watershed_split and n >= 1:
        labels, n = _watershed_split(filled, seed_mask, labels)

    # Connected-component labels of the base footprint, to grow each seed to its
    # base extent.
    base_labels, _ = ndimage.label(base_mask2d)

    candidates: list[StormCell] = []
    for lab in range(1, n + 1):
        region = labels == lab
        zc, _, _ = np.nonzero(region)
        n_levels = int(np.unique(zc).size)

        seed_foot = region.any(axis=0)  # (ny, nx)
        # Grow to the base-reflectivity footprint that contains this seed.
        overlap = base_labels[seed_foot]
        overlap = overlap[overlap > 0]
        if overlap.size:
            base_id = np.bincount(overlap).argmax()
            foot = base_labels == base_id
        else:
            foot = seed_foot

        ys_foot, xs_foot = np.nonzero(foot)
        area_km2 = float(ys_foot.size) * cell_area_km2

        col_top = top_mask[:, foot]
        z_present = np.nonzero(col_top.any(axis=1))[0]
        if z_present.size == 0:
            continue
        k_top, k_base = int(z_present.max()), int(z_present.min())
        echo_top_km = float(volume.z[k_top]) / 1000.0
        base_km = float(volume.z[k_base]) / 1000.0
        depth_km = (float(volume.z[k_top]) - float(volume.z[k_base])) / 1000.0 + dz_km

        max_dbz = float(np.nanmax(refl[region]))

        comp = np.where(region, np.nan_to_num(refl, nan=-np.inf), -np.inf).max(axis=0)
        w = comp[seed_foot]
        w = np.clip(np.where(np.isfinite(w), w, 0.0), 0.0, None)
        ys_seed, xs_seed = np.nonzero(seed_foot)
        if w.sum() <= 0:
            w = np.ones_like(w)
        seed_x = float(np.average(volume.x[xs_seed], weights=w))
        seed_y = float(np.average(volume.y[ys_seed], weights=w))
        seed_lon, seed_lat = volume.xy_to_lonlat(np.array([seed_x]), np.array([seed_y]))

        # --- Admission gates: what rejects AP -----------------------------
        admitted = (
            n_levels >= params.continuity_levels
            and echo_top_km >= params.echo_top_min_km
            and area_km2 >= params.min_area_km2
        )
        if not admitted:
            continue

        candidates.append(
            StormCell(
                cell_id=-1,
                site=volume.site,
                valid_time=volume.valid_time,
                seed_lon=float(np.atleast_1d(seed_lon)[0]),
                seed_lat=float(np.atleast_1d(seed_lat)[0]),
                seed_x=seed_x,
                seed_y=seed_y,
                max_dbz=max_dbz,
                area_km2=area_km2,
                echo_top_km=echo_top_km,
                base_km=base_km,
                depth_km=depth_km,
                n_levels=n_levels,
                envelope=_footprint_polygon(volume, ys_foot, xs_foot),
            )
        )

    candidates.sort(key=lambda c: (-c.max_dbz, -c.area_km2, c.seed_x, c.seed_y))
    candidates = _suppress_close_seeds(candidates, params.seed_min_separation_km)
    for i, c in enumerate(candidates):
        c.cell_id = i + 1
    return candidates


def _watershed_split(filled: np.ndarray, seed_mask: np.ndarray, labels: np.ndarray):
    """Split merged seed components at reflectivity saddles (skimage watershed)."""
    from scipy import ndimage as ndi
    from skimage.feature import peak_local_max
    from skimage.segmentation import watershed

    # Markers: local reflectivity maxima within the seed mask.
    coords = peak_local_max(
        np.where(seed_mask, filled, 0.0), min_distance=3, labels=labels
    )
    if coords.shape[0] == 0:
        return labels, int(labels.max())
    markers = np.zeros_like(labels)
    for i, (z, y, x) in enumerate(coords, 1):
        markers[z, y, x] = i
    split = watershed(-filled, markers=markers, mask=seed_mask)
    return split, int(split.max())
