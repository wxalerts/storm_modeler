"""Velocity-couplet detection — grid-based LLSD-style azimuthal shear.

Runs on the **base** velocity layer (``products["velocity"]``), not SRM:
azimuthal shear is (to leading order) invariant under uniform-flow
subtraction, so detection needs no frame — only the Vrot *measurement* is
frame-corrected, via :func:`storm_modeler.detection.srm.correct_vrot`.

The grid is radar-centred (radar at the origin, ``x`` east / ``y`` north in
metres), so at grid point (x, y) the radial unit vector is ``r̂ = (x, y)/r``
and the tangential (azimuthal, **counterclockwise**) unit vector is
``t̂ = (-y, x)/r``. Azimuthal shear is the directional derivative of radial
velocity along t̂::

    shear = ∂v/∂s along t̂ = (∂v/∂x)·t̂x + (∂v/∂y)·t̂y      [s⁻¹]

**Sign convention (derived, locked by test):** consider a Northern-Hemisphere
cyclonic — counterclockwise — vortex due north of the radar. Looking down the
beam, its outbound half is east of the vortex centre and the inbound half
west, so ``∂v/∂x > 0`` across the couplet; at that location
``t̂ = (-1, 0)`` points **west**, hence ``t̂·∇v = -∂v/∂x < 0``. With the
counterclockwise t̂ defined above, cyclonic rotation therefore yields
**negative** azimuthal shear (and anticyclonic positive); the classifier is
``cyclonic = mean(shear over the component) < 0``, and the synthetic
counterclockwise-Rankine test asserts it.

Gradients are computed with explicit central differences over array slices —
NaN propagates through any stencil that touches a NaN gate, which is exactly
the masking the method calls for — and the shear field is additionally masked
where velocity itself is NaN or the range is outside [5 km,
``max_range_km``] (uniform flow's own radial projection has azimuthal shear
``|U|·sin(Δθ)/r``, so very short ranges are structurally noisy).

Pure given (volume, cells, params, results); no Qt, no globals. Deterministic:
identical inputs yield identical couplets in the same order (strongest
``vr_sr_ms`` first, then largest area).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import structlog
from scipy import ndimage

from ..models import GriddedVolume
from ..settings.resolver import CoupletParams
from ..viz.motion import mean_motion_uv, track_motion_uv
from .srm import correct_vrot

log = structlog.get_logger(__name__)

#: Ranges closer than this (km) are masked — the beam geometry degenerates at
#: the radar and uniform flow's own azimuthal shear (∝ 1/r) blows up there.
_MIN_RANGE_KM = 5.0

#: Grid cells the component is dilated by before sampling the velocity
#: extrema — the couplet's inbound/outbound peaks straddle the shear maximum.
_EXTREMA_DILATE_CELLS = 2


@dataclass
class Couplet:
    """One velocity couplet (mesocyclone-scale rotation) within a volume."""

    couplet_id: int          # sequential within volume
    valid_time: datetime
    centroid_lon: float
    centroid_lat: float
    range_km: float
    center_az_deg: float
    max_shear_s1: float      # peak |azimuthal shear| in the component
    cyclonic: bool
    v_max_ms: float
    v_min_ms: float
    vr_ms: float             # ground-relative two-signed / one-sided rule
    vr_sr_ms: float          # storm-relative via correct_vrot
    motion_source: str       # "track:<id>" | "volume_mean" | "none"
    area_km2: float


def azimuthal_shear(
    volume: GriddedVolume,
    max_range_km: float,
    min_range_km: float = _MIN_RANGE_KM,
) -> np.ndarray | None:
    """(ny, nx) azimuthal shear (s⁻¹) of the base velocity layer, NaN-masked.

    NaN wherever velocity is NaN, any central-difference neighbour is NaN
    (propagation through the stencil), the grid edge truncates the stencil,
    or the range is outside [min_range_km, max_range_km]. ``None`` when the
    volume has no velocity layer.
    """
    v = volume.product_2d("velocity")
    if v is None:
        return None
    v = np.asarray(v, dtype=np.float64)
    xx, yy = np.meshgrid(volume.x, volume.y)
    r = np.hypot(xx, yy)

    dvdx = np.full_like(v, np.nan)
    dvdy = np.full_like(v, np.nan)
    dvdx[:, 1:-1] = (v[:, 2:] - v[:, :-2]) / (xx[:, 2:] - xx[:, :-2])
    dvdy[1:-1, :] = (v[2:, :] - v[:-2, :]) / (yy[2:, :] - yy[:-2, :])

    rr = np.maximum(r, 1.0)
    shear = dvdx * (-yy / rr) + dvdy * (xx / rr)
    shear[~np.isfinite(v)] = np.nan
    shear[(r < min_range_km * 1000.0) | (r > max_range_km * 1000.0)] = np.nan
    return shear


def _motion_for(
    x_c: float, y_c: float, cells, results, assoc_max_km: float
) -> tuple[tuple[float, float], str]:
    """Storm motion for a couplet at grid metres (x_c, y_c).

    Nearest tracked SCIT cell within ``assoc_max_km`` (centroid/seed distance
    — TODO: envelope distance is a later upgrade) whose track yields a motion
    vector; else the mean motion of the volume's tracks; else (0, 0).
    """
    best: tuple[tuple[float, float], str] | None = None
    best_d = assoc_max_km * 1000.0
    for c in cells or []:
        if c.track_id < 0:
            continue
        d = math.hypot(c.seed_x - x_c, c.seed_y - y_c)
        if d <= best_d:
            uv = track_motion_uv(c, results or [])
            if uv is not None:
                best_d, best = d, (uv, f"track:{c.track_id}")
    if best is not None:
        return best
    uv = mean_motion_uv(cells or [], results or [])
    if uv is not None:
        return uv, "volume_mean"
    return (0.0, 0.0), "none"


def detect_couplets(
    volume: GriddedVolume,
    cells,
    params: CoupletParams | None = None,
    results=None,
) -> list[Couplet]:
    """Detect velocity couplets in one volume's base velocity layer.

    ``cells`` are the volume's SCIT detections (motion association);
    ``results`` the warning's :class:`VolumeResult` list so far (track
    history — without it the motion chain degrades to ``"none"``).
    Deterministic: strongest ``vr_sr_ms`` first, then largest area.
    """
    params = params or CoupletParams()
    shear = azimuthal_shear(volume, params.max_range_km)
    if shear is None:
        return []
    hot = np.isfinite(shear) & (np.abs(shear) >= params.min_shear_s1)
    if not hot.any():
        return []

    structure = ndimage.generate_binary_structure(2, 2)  # 8-connected
    labels, n = ndimage.label(hot, structure=structure)
    v = np.asarray(volume.product_2d("velocity"), dtype=np.float64)
    xx, yy = np.meshgrid(volume.x, volume.y)
    px_area = volume.dx_km * volume.dx_km

    out: list[Couplet] = []
    for lab in range(1, n + 1):
        region = labels == lab
        area_km2 = float(region.sum()) * px_area
        if area_km2 < params.min_area_km2:
            continue

        w = np.abs(shear[region])
        x_c = float(np.average(xx[region], weights=w))
        y_c = float(np.average(yy[region], weights=w))
        max_shear = float(w.max())
        # CCW t̂ makes cyclonic rotation negative shear (see module docstring).
        cyclonic = float(np.mean(shear[region])) < 0.0

        # Velocity extrema straddle the shear maximum: sample the component
        # dilated by a couple of grid cells.
        dil = ndimage.binary_dilation(region, structure=structure,
                                      iterations=_EXTREMA_DILATE_CELLS)
        vals = v[dil]
        finite = vals[np.isfinite(vals)]
        v_max = float(finite.max())
        v_min = float(finite.min())

        range_km = math.hypot(x_c, y_c) / 1000.0
        center_az_deg = math.degrees(math.atan2(x_c, y_c)) % 360.0
        (u_ms, v_ms), motion_source = _motion_for(
            x_c, y_c, cells, results, params.assoc_max_km
        )
        vr_ms, _, _ = correct_vrot(v_max, v_min, center_az_deg, 0.0, 0.0)
        vr_sr_ms, _, _ = correct_vrot(v_max, v_min, center_az_deg, u_ms, v_ms)

        lon, lat = volume.xy_to_lonlat(np.array([x_c]), np.array([y_c]))
        out.append(Couplet(
            couplet_id=-1,
            valid_time=volume.valid_time,
            centroid_lon=float(np.atleast_1d(lon)[0]),
            centroid_lat=float(np.atleast_1d(lat)[0]),
            range_km=range_km,
            center_az_deg=center_az_deg,
            max_shear_s1=max_shear,
            cyclonic=cyclonic,
            v_max_ms=v_max,
            v_min_ms=v_min,
            vr_ms=vr_ms,
            vr_sr_ms=vr_sr_ms,
            motion_source=motion_source,
            area_km2=area_km2,
        ))

    out.sort(key=lambda c: (-c.vr_sr_ms, -c.area_km2,
                            c.centroid_lon, c.centroid_lat))
    for i, c in enumerate(out):
        c.couplet_id = i + 1
    if out:
        log.info("couplets.detected", valid_time=volume.valid_time.isoformat(),
                 n=len(out), strongest_vr_sr=round(out[0].vr_sr_ms, 1))
    return out
