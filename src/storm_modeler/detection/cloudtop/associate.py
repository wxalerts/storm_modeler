"""Radar association — measure storm tilt from cloud-top vs radar core offset.

Each cloud-top cell is first **parallax-corrected** to its true ground position
(see :mod:`.parallax`) so a tall top registers over the storm that made it
rather than off toward the satellite limb. Then each radar storm cell is matched
to the cloud top that sits **over** it — the cloud top whose (parallax-corrected)
anvil footprint contains the radar core, coldest top winning when several
overlap, with a nearest-within-``assoc_max_km`` fallback. The matched radar
:class:`StormCell` is annotated with ``cloud_top_c`` (coldest-pixel temperature,
deg C) and ``overshooting_top`` for the volume listing; the cloud top records the
residual core-to-top offset as storm tilt (``tilt_km`` / ``tilt_bearing_deg``).

A single anvil may overlie several cells, so cloud tops are **not** claimed
exclusively. Pure given the two cell lists; the caller pairs a scene to the radar
volume nearest in time before calling.
"""

from __future__ import annotations

from typing import Iterable

import numpy as np
from shapely.geometry import Point, Polygon

from ..detection_v2.types import StormCell
from ...settings.resolver import CloudTopParams
from .geo import haversine_km, initial_bearing_deg
from .parallax import bt_to_height_m, correct, subsat_lon
from .types import CloudTopCell

#: Leniency (deg, ~5 km) when testing whether an anvil footprint covers a core.
_ENVELOPE_BUFFER_DEG = 0.05


def _parallax_correct_cell(c: CloudTopCell) -> None:
    """Shift a cloud top's geometry to true ground (in place).

    The coldest pixel uses the deepest (highest) top; the anvil centroid and
    footprint use the cell-mean height — warmer anvil edges sit lower and shift
    less, but one height per cell is plenty for the association test.
    """
    sat_lon = subsat_lon(c.satellite)
    h_cold = bt_to_height_m(c.min_bt_k)
    h_anvil = bt_to_height_m(c.mean_bt_k)
    c.cold_lon, c.cold_lat = correct(c.cold_lon, c.cold_lat, h_cold, sat_lon)
    c.centroid_lon, c.centroid_lat = correct(
        c.centroid_lon, c.centroid_lat, h_anvil, sat_lon
    )
    try:
        xs, ys = c.envelope.exterior.xy
        lon_t, lat_t = correct(np.asarray(xs), np.asarray(ys), h_anvil, sat_lon)
        poly = Polygon(zip(np.atleast_1d(lon_t), np.atleast_1d(lat_t)))
        c.envelope = poly if poly.is_valid else poly.buffer(0)
    except Exception:  # noqa: BLE001 - keep the apparent envelope if reshape fails
        pass


def associate_radar(
    cloudtop_cells: list[CloudTopCell],
    radar_cells: Iterable[StormCell],
    params: CloudTopParams | None = None,
) -> list[CloudTopCell]:
    """Parallax-correct cloud tops, then annotate radar cells with the top over them.

    Mutates both sides in place and returns the cloud-top cells. Radar cells with
    no cloud top over them (or within ``assoc_max_km``) keep ``cloud_top_c is
    None``; unmatched cloud tops keep ``radar_track_id == -1`` / NaN tilt.
    """
    params = params or CloudTopParams()

    # 1. Parallax-correct every cloud top (always — markers/persistence too).
    for c in cloudtop_cells:
        _parallax_correct_cell(c)

    radar = list(radar_cells)
    if not radar:
        return cloudtop_cells

    # 2. Match each radar cell to the cloud top that sits over it.
    for r in radar:
        core = Point(r.seed_lon, r.seed_lat)
        covering = [
            c for c in cloudtop_cells
            if c.envelope.buffer(_ENVELOPE_BUFFER_DEG).contains(core)
        ]
        if covering:
            best = min(covering, key=lambda c: c.min_bt_k)  # deepest/coldest top
        else:
            best, best_d = None, params.assoc_max_km
            for c in cloudtop_cells:
                d = haversine_km(r.seed_lon, r.seed_lat, c.cold_lon, c.cold_lat)
                if d <= best_d:
                    best_d, best = d, c
            if best is None:
                continue

        r.cloud_top_c = best.min_bt_k - 273.15
        r.overshooting_top = best.overshooting_top

        # Record storm tilt on the cloud top (closest core wins when shared).
        d = haversine_km(r.seed_lon, r.seed_lat, best.cold_lon, best.cold_lat)
        if best.radar_track_id == -1 or d < best.tilt_km:
            best.radar_track_id = r.track_id
            best.tilt_km = d
            best.tilt_bearing_deg = initial_bearing_deg(
                r.seed_lon, r.seed_lat, best.cold_lon, best.cold_lat
            )
    return cloudtop_cells
