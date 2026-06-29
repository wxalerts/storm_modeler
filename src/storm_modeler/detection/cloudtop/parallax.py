"""GOES parallax correction for cloud-top positions.

A tall cloud top imaged from geostationary orbit is displaced in the image away
from the sub-satellite point — it is *seen* past where it actually sits on the
ground, by roughly ``height * tan(satellite-zenith-angle)`` (tens of km for a
deep storm top at CONUS mid-latitudes). Left uncorrected, even an untilted
overshooting top lands well off its radar core, so it never associates with the
warned cell. This module shifts an *apparent* (lon, lat) back to the cloud's
*true ground* (lon, lat) given the cloud-top height, so cloud tops register over
the storms that produced them — leaving the residual offset to the radar core as
genuine storm tilt.

Pure NumPy (no pyproj); a spherical-Earth line-of-sight intersection, which is
accurate to well under a km at these scales.
"""

from __future__ import annotations

import numpy as np

#: Earth mean radius (m) and geostationary orbit radius from Earth centre (m).
_RE = 6371008.8
_RS = 42164000.0

#: Sub-satellite longitude (deg) by GOES short name. East = 16/19 at −75.2°,
#: West = 17/18 at −137°.
_SUBSAT_LON = {
    "GOES-16": -75.2, "GOES-19": -75.2,
    "GOES-17": -137.2, "GOES-18": -137.0,
}


def subsat_lon(satellite: str, default: float = -75.2) -> float:
    """Sub-satellite longitude (deg) for a GOES short name (e.g. ``GOES-19``)."""
    return _SUBSAT_LON.get(satellite, default)


def bt_to_height_m(bt_k: float) -> float:
    """Rough cloud-top height (m) from Band-13 brightness temperature.

    A standard-atmosphere estimate: 6.5 K/km from a 288 K surface up to the
    ~217 K / ~10.9 km tropopause, then a slow rise above it (overshooting tops),
    clamped to 0–16 km. Good enough to drive the parallax shift, whose
    sensitivity to height is gentle.
    """
    if bt_k >= 217.0:
        h_km = (288.0 - bt_k) / 6.5
    else:
        h_km = 10.9 + (217.0 - bt_k) * 0.05
    return max(0.0, min(16.0, h_km)) * 1000.0


def correct(lon, lat, height_m: float, sat_lon: float):
    """Apparent (lon, lat) → true-ground (lon, lat) for a cloud at ``height_m``.

    Scalars or NumPy arrays. The apparent surface point, the satellite, and the
    cloud are colinear; we intersect that line of sight with the sphere of radius
    ``Re + height`` (the near side, facing the satellite) and read off its
    ground projection.
    """
    lon = np.asarray(lon, dtype=np.float64)
    lat = np.asarray(lat, dtype=np.float64)
    lam, phi = np.radians(lon), np.radians(lat)
    lam_s = np.radians(sat_lon)

    # Satellite position (sub-lat 0) and apparent surface point, ECEF.
    Sx, Sy, Sz = _RS * np.cos(lam_s), _RS * np.sin(lam_s), 0.0
    Px = _RE * np.cos(phi) * np.cos(lam)
    Py = _RE * np.cos(phi) * np.sin(lam)
    Pz = _RE * np.sin(phi)

    Dx, Dy, Dz = Px - Sx, Py - Sy, Pz - Sz
    a = Dx * Dx + Dy * Dy + Dz * Dz
    b = 2.0 * (Sx * Dx + Sy * Dy + Sz * Dz)
    target = _RE + float(height_m)
    c = (Sx * Sx + Sy * Sy + Sz * Sz) - target * target
    disc = np.maximum(b * b - 4.0 * a * c, 0.0)
    t = (-b - np.sqrt(disc)) / (2.0 * a)  # near-side intersection (toward satellite)

    Cx, Cy, Cz = Sx + t * Dx, Sy + t * Dy, Sz + t * Dz
    lon_t = np.degrees(np.arctan2(Cy, Cx))
    lat_t = np.degrees(np.arctan2(Cz, np.hypot(Cx, Cy)))
    if lon_t.ndim == 0:
        return float(lon_t), float(lat_t)
    return lon_t, lat_t
