"""Small great-circle helpers for cloud-top tracking and radar association.

Cloud-top cells live in lon/lat (the scene is already a lon/lat raster), so
distances and bearings are computed on the sphere rather than in projected
metres. Earth radius is the mean radius; sub-km precision is irrelevant at the
~2 km ABI pixel scale.
"""

from __future__ import annotations

import math

_EARTH_KM = 6371.0088


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in km between two (lon, lat) points (degrees)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * _EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def initial_bearing_deg(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Initial bearing in degrees (0=N, clockwise) from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lon2 - lon1)
    y = math.sin(dlmb) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlmb)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
