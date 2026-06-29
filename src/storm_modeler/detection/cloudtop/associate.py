"""Radar association — measure storm tilt from cloud-top vs radar core offset.

Each cold cloud-top cell's coldest pixel (the overshooting-top candidate, the
physical top of the updraft) is matched to the nearest radar storm cell's core
(``StormCell.seed_lon``/``seed_lat``). The horizontal displacement between the
two — ``tilt_km`` and its bearing — quantifies how far the cloud top leans off
the low-level core: a large tilt indicates a sheared/tilted storm column, often
with the anvil and overshooting top displaced downshear from the surface echo.

Pure given the two cell lists; the caller pairs a scene to the radar volume
nearest in time before calling.
"""

from __future__ import annotations

from typing import Iterable

from ..detection_v2.types import StormCell
from ...settings.resolver import CloudTopParams
from .geo import haversine_km, initial_bearing_deg
from .types import CloudTopCell


def associate_radar(
    cloudtop_cells: list[CloudTopCell],
    radar_cells: Iterable[StormCell],
    params: CloudTopParams | None = None,
) -> list[CloudTopCell]:
    """Match each cold-top cell to its nearest radar core; set tilt + CT fields.

    Greedy nearest-neighbour within ``assoc_max_km`` (cold-tops are processed
    coldest-first); each radar cell is claimed at most once. Mutates **both**
    sides in place: the cloud-top cell gets ``radar_track_id``/``tilt_km``/
    ``tilt_bearing_deg``; the matched radar :class:`StormCell` gets
    ``cloud_top_c`` (coldest-pixel temperature in deg C) and ``overshooting_top``,
    so the volume listing can show a cloud-top temperature and OT flag per storm.
    Returns the cloud-top cells. Unmatched cloud tops keep ``radar_track_id ==
    -1`` / NaN tilt; unmatched radar cells keep ``cloud_top_c is None``.
    """
    params = params or CloudTopParams()
    radar = list(radar_cells)
    claimed: set[int] = set()

    for c in cloudtop_cells:
        best_i, best_d = -1, params.assoc_max_km
        for i, r in enumerate(radar):
            if i in claimed:
                continue
            d = haversine_km(c.cold_lon, c.cold_lat, r.seed_lon, r.seed_lat)
            if d <= best_d:
                best_d, best_i = d, i
        if best_i >= 0:
            r = radar[best_i]
            c.radar_track_id = r.track_id
            c.tilt_km = best_d
            c.tilt_bearing_deg = initial_bearing_deg(
                r.seed_lon, r.seed_lat, c.cold_lon, c.cold_lat
            )
            r.cloud_top_c = c.min_bt_k - 273.15
            r.overshooting_top = c.overshooting_top
            claimed.add(best_i)
    return cloudtop_cells
