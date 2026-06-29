"""Cloud-top identification — one ABI Band-13 scene + params in, cells out.

Pure and testable: takes an explicit :class:`CloudTopParams`, reads no globals,
and touches no pyproj (the scene is already a regular lon/lat raster, so every
location is an index lookup). The algorithm mirrors SCIT's structure
(``detection_v2.identify``): threshold → connected components → per-object
metrics → admission gate → deterministic sort.

Cold cloud tops (low brightness temperature) mark deep convection. Each cold
object yields its coldest pixel (the overshooting-top candidate), a
BT-weighted centroid, area, and two flags: ``anviled_out`` for a broad cold
canopy, and ``overshooting_top`` when the coldest pixel pokes a set depth
colder than the surrounding anvil background.
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage
from shapely.geometry import MultiPoint, Polygon

from ...models import SatelliteScene
from ...settings.resolver import CloudTopParams
from .types import CloudTopCell

#: Annulus (km) around the coldest pixel used to estimate the anvil background
#: brightness temperature for the overshooting-top depth test.
_OT_INNER_KM = 5.0
_OT_OUTER_KM = 15.0

#: Warm reference (K) for the BT-weighted centroid: colder pixels weigh more.
_WARM_REF_K = 300.0


def _envelope(lons: np.ndarray, lats: np.ndarray, ys: np.ndarray, xs: np.ndarray) -> Polygon:
    """Convex hull of an object's pixel centres, in lon/lat."""
    pts = list(zip(lons[xs], lats[ys]))
    hull = MultiPoint(pts).convex_hull
    if isinstance(hull, Polygon):
        return hull
    return hull.buffer(0.01)


def _ot_depth(
    bt: np.ndarray, yc: int, xc: int, dlat_km: float, dlon_km: float, anvil_bt_k: float
) -> float:
    """Anvil-background mean (in the annulus) minus the coldest pixel.

    The background is the mean of finite anvil pixels (``bt <= anvil_bt_k``) in a
    ring ``[_OT_INNER_KM, _OT_OUTER_KM]`` around the coldest pixel. Returns 0 when
    no anvil pixels surround the core (nothing to overshoot).
    """
    ny, nx = bt.shape
    ry = max(1, int(round(_OT_OUTER_KM / max(dlat_km, 1e-6))))
    rx = max(1, int(round(_OT_OUTER_KM / max(dlon_km, 1e-6))))
    y0, y1 = max(0, yc - ry), min(ny, yc + ry + 1)
    x0, x1 = max(0, xc - rx), min(nx, xc + rx + 1)
    win = bt[y0:y1, x0:x1]
    yy, xx = np.mgrid[y0:y1, x0:x1]
    dist = np.hypot((yy - yc) * dlat_km, (xx - xc) * dlon_km)
    ring = (dist >= _OT_INNER_KM) & (dist <= _OT_OUTER_KM) & np.isfinite(win) & (win <= anvil_bt_k)
    if not ring.any():
        return 0.0
    return float(np.mean(win[ring]) - bt[yc, xc])


def identify(
    scene: SatelliteScene, params: CloudTopParams | None = None
) -> list[CloudTopCell]:
    """Identify admitted cold cloud-top cells in a single ABI scene.

    Deterministic: the same (scene, params) always yields the same cells in the
    same order (coldest first, then largest).
    """
    params = params or CloudTopParams()
    bt = scene.bt  # (nlat, nlon) Kelvin, NaN off-disk
    lons, lats = scene.lons, scene.lats
    dlon_km, dlat_km = scene.dlon_km, scene.dlat_km
    px_area = dlon_km * dlat_km

    cold = np.isfinite(bt) & (bt <= params.cold_bt_k)
    if not cold.any():
        return []

    structure = ndimage.generate_binary_structure(2, 2)  # 8-connected
    labels, n = ndimage.label(cold, structure=structure)

    candidates: list[CloudTopCell] = []
    for lab in range(1, n + 1):
        region = labels == lab
        ys, xs = np.nonzero(region)
        area_km2 = float(ys.size) * px_area
        if area_km2 < params.min_area_km2:
            continue

        vals = bt[ys, xs]
        kmin = int(np.argmin(vals))
        yc, xc = int(ys[kmin]), int(xs[kmin])
        min_bt = float(vals[kmin])
        mean_bt = float(np.mean(vals))
        cold_lon, cold_lat = float(lons[xc]), float(lats[yc])

        # BT-weighted centroid: colder pixels weigh more.
        w = np.clip(_WARM_REF_K - vals, 0.0, None)
        if w.sum() <= 0:
            w = np.ones_like(w)
        centroid_lon = float(np.average(lons[xs], weights=w))
        centroid_lat = float(np.average(lats[ys], weights=w))

        anvil_frac = float(np.mean(vals <= params.anvil_bt_k))
        anviled_out = area_km2 >= params.anvil_area_km2 or anvil_frac >= 0.5

        ot_depth = _ot_depth(bt, yc, xc, dlat_km, dlon_km, params.anvil_bt_k)
        overshooting_top = min_bt <= params.ot_max_bt_k and ot_depth >= params.ot_delta_k

        candidates.append(
            CloudTopCell(
                cell_id=-1,
                satellite=scene.satellite,
                valid_time=scene.valid_time,
                cold_lon=cold_lon,
                cold_lat=cold_lat,
                min_bt_k=min_bt,
                centroid_lon=centroid_lon,
                centroid_lat=centroid_lat,
                mean_bt_k=mean_bt,
                area_km2=area_km2,
                anviled_out=anviled_out,
                overshooting_top=overshooting_top,
                ot_depth_k=ot_depth,
                envelope=_envelope(lons, lats, ys, xs),
            )
        )

    candidates.sort(key=lambda c: (c.min_bt_k, -c.area_km2, c.cold_lon, c.cold_lat))
    for i, c in enumerate(candidates):
        c.cell_id = i + 1
    return candidates
