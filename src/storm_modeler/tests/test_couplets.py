"""Velocity-couplet detection (pure numpy/scipy, no Qt/GL)."""

from __future__ import annotations

import math
import warnings
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
from shapely.geometry import Point

from storm_modeler.detection.couplets import detect_couplets
from storm_modeler.detection.detection_v2 import StormCell
from storm_modeler.models import GriddedVolume
from storm_modeler.settings.resolver import CoupletParams

T0 = datetime(2026, 3, 10, 21, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=6)


def _vortex_volume(
    x0: float,
    y0: float,
    vmax: float = 25.0,
    radius: float = 4000.0,
    ccw: bool = True,
    u_t: float = 0.0,
    v_t: float = 0.0,
    nan_within_km: float | None = None,
    valid_time: datetime = T0,
) -> GriddedVolume:
    """Radar volume whose velocity layer is a (translating) Rankine vortex.

    Same construction as ``test_srm``: build the wind field, project it onto
    the radials. The layer is injected post-construction to keep float64.
    """
    x = np.linspace(-50000.0, 50000.0, 101)
    y = np.linspace(-50000.0, 50000.0, 101)
    z = np.array([500.0, 1000.0])
    xx, yy = np.meshgrid(x, y)
    dx, dy = xx - x0, yy - y0
    s = np.maximum(np.hypot(dx, dy), 1.0)
    vt = vmax * np.where(s < radius, s / radius, radius / s)
    sgn = 1.0 if ccw else -1.0
    uw = sgn * (-vt * dy / s) + u_t
    vw = sgn * (vt * dx / s) + v_t
    r = np.maximum(np.hypot(xx, yy), 1.0)
    vel = (uw * xx + vw * yy) / r
    if nan_within_km is not None:
        vel[np.hypot(xx, yy) < nan_within_km * 1000.0] = np.nan
    refl = np.full((z.size, y.size, x.size), np.nan, dtype=np.float32)
    vol = GriddedVolume("KLOT", valid_time, refl, x, y, z, 41.6044, -88.0847)
    vol.products["velocity"] = vel
    return vol


def _couplet_xy(c) -> tuple[float, float]:
    """Reconstruct a couplet's grid-metre centroid from its range/azimuth."""
    r_m = c.range_km * 1000.0
    az = math.radians(c.center_az_deg)
    return r_m * math.sin(az), r_m * math.cos(az)


def _track_cell(minute: int, sx: float, sy: float, track_id: int = 7) -> StormCell:
    t = T0 + timedelta(minutes=minute)
    env = Point(-88.0, 41.6).buffer(0.05)
    return StormCell(
        cell_id=1, site="KLOT", valid_time=t, seed_lon=-88.0, seed_lat=41.6,
        seed_x=sx, seed_y=sy, max_dbz=55.0, area_km2=20.0, echo_top_km=10.0,
        base_km=1.0, depth_km=9.0, n_levels=12, envelope=env, track_id=track_id,
    )


def test_ccw_rankine_is_one_cyclonic_couplet():
    vol = _vortex_volume(10000.0, 40000.0)
    couplets = detect_couplets(vol, [], CoupletParams())
    assert len(couplets) == 1
    c = couplets[0]
    assert c.cyclonic is True
    cx, cy = _couplet_xy(c)
    assert math.hypot(cx - 10000.0, cy - 40000.0) <= 1500.0  # within a cell
    # SR Vrot within 15% of the imposed tangential max (no motion → SR == GR).
    assert abs(c.vr_sr_ms - 25.0) <= 0.15 * 25.0
    assert c.motion_source == "none"
    assert c.vr_sr_ms == c.vr_ms
    assert c.max_shear_s1 >= CoupletParams().min_shear_s1
    assert c.area_km2 >= CoupletParams().min_area_km2


def test_frame_invariance_and_sr_correction():
    """Translation along the radial pushes the couplet one-sided: detection
    must not change, SR Vrot must recover, GR Vrot must degrade.

    Velocity is blanked inside 15 km: a uniform flow's own radial projection
    carries azimuthal shear |U|·sin(Δθ)/r, which only drops below the
    detection threshold beyond ~7.5 km for 30 m/s — the same reason the
    module hard-gates r < 5 km.
    """
    still = _vortex_volume(0.0, 45000.0, nan_within_km=15.0)
    ref = detect_couplets(still, [], CoupletParams())
    assert len(ref) == 1

    moving = _vortex_volume(0.0, 45000.0, v_t=30.0, nan_within_km=15.0,
                            valid_time=T1)
    # A track moving north at 30 m/s (10.8 km per 6 min), arriving on the
    # vortex at this volume's time (association is centroid distance).
    c0 = _track_cell(0, 0.0, 45000.0 - 10800.0)
    c1 = _track_cell(6, 0.0, 45000.0)
    results = [SimpleNamespace(cells=[c0]), SimpleNamespace(cells=[c1])]
    got = detect_couplets(moving, [c1], CoupletParams(), results=results)

    assert len(got) == len(ref) == 1  # frame-invariant detection
    r, g = ref[0], got[0]
    assert g.cyclonic is True
    rx, ry = _couplet_xy(r)
    gx, gy = _couplet_xy(g)
    assert math.hypot(gx - rx, gy - ry) <= 1500.0
    assert abs(g.area_km2 - r.area_km2) <= 2.0  # same mask, ± edge pixels
    assert g.motion_source == "track:7"
    # SR Vrot matches the still-frame measurement; GR degrades one-sided.
    assert abs(g.vr_sr_ms - r.vr_sr_ms) <= 1.5
    assert abs(g.vr_sr_ms - 25.0) <= 0.15 * 25.0
    assert abs(g.vr_ms - g.vr_sr_ms) > 1.5


def test_clockwise_vortex_is_anticyclonic():
    vol = _vortex_volume(10000.0, 40000.0, ccw=False)
    couplets = detect_couplets(vol, [], CoupletParams())
    assert len(couplets) == 1
    assert couplets[0].cyclonic is False


def test_all_nan_and_all_zero_are_empty_and_quiet():
    x = np.linspace(-50000.0, 50000.0, 101)
    z = np.array([500.0, 1000.0])
    refl = np.full((z.size, x.size, x.size), np.nan, dtype=np.float32)
    for fill in (np.nan, 0.0):
        vol = GriddedVolume("KLOT", T0, refl, x, x, z, 41.6044, -88.0847)
        vol.products["velocity"] = np.full((x.size, x.size), fill)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            assert detect_couplets(vol, [], CoupletParams()) == []
        assert caught == []


def test_range_gates():
    # A modest vortex tucked inside the 5 km near-radar gate: not detected.
    near = _vortex_volume(0.0, 3000.0, vmax=10.0, radius=1500.0)
    assert detect_couplets(near, [], CoupletParams()) == []
    # The reference vortex beyond a tightened max-range gate: not detected.
    far = _vortex_volume(10000.0, 40000.0)
    assert detect_couplets(far, [], CoupletParams(max_range_km=30.0)) == []


def test_deterministic_ordering_and_ids():
    vol = _vortex_volume(10000.0, 40000.0)
    # Add a second, weaker vortex well away from the first (still above the
    # 0.004 s^-1 gate: 18 m/s over a 3 km core ≈ 0.006 s^-1 peak shear).
    weak = _vortex_volume(-30000.0, -20000.0, vmax=18.0, radius=3000.0)
    vel = np.where(
        np.isfinite(weak.products["velocity"]) & (np.abs(weak.products["velocity"]) > np.abs(vol.products["velocity"])),
        weak.products["velocity"], vol.products["velocity"],
    )
    vol.products["velocity"] = vel
    a = detect_couplets(vol, [], CoupletParams())
    b = detect_couplets(vol, [], CoupletParams())
    assert len(a) == 2
    assert a[0].vr_sr_ms >= a[1].vr_sr_ms  # strongest first
    assert [c.couplet_id for c in a] == [1, 2]
    assert [(c.couplet_id, c.centroid_lon, c.vr_sr_ms) for c in a] == \
           [(c.couplet_id, c.centroid_lon, c.vr_sr_ms) for c in b]
