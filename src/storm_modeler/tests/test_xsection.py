"""Pure-logic tests for the cross-section module (no GL / no Qt).

The panel render is exercised by ``render_cell --view xsection`` (Section 8B);
here we lock in the azimuth selection and slice extraction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np

from storm_modeler.detection.detection_v2 import StormCell
from storm_modeler.settings.resolver import ViewerParams
from storm_modeler.models import GriddedVolume
from storm_modeler.viz import xsection
from shapely.geometry import Point


def _cell(minute: int, sx: float, sy: float, track_id: int = 7) -> StormCell:
    t = datetime(2024, 5, 25, 12, minute, tzinfo=timezone.utc)
    env = Point(-97.3, 32.6).buffer(0.05)  # small footprint, lon/lat
    return StormCell(
        cell_id=1, site="KFWS", valid_time=t, seed_lon=-97.3, seed_lat=32.6,
        seed_x=sx, seed_y=sy, max_dbz=55.0, area_km2=20.0, echo_top_km=10.0,
        base_km=1.0, depth_km=9.0, n_levels=12, envelope=env, track_id=track_id,
    )


def test_section_azimuth_follows_track_heading_due_east():
    c0 = _cell(0, 0.0, 0.0)
    c1 = _cell(6, 5000.0, 0.0)  # moved 5 km due east
    results = [SimpleNamespace(cells=[c0]), SimpleNamespace(cells=[c1])]
    vp = ViewerParams(xsection_azimuth_source="track")
    az = xsection.section_azimuth(c1, results, vp)
    assert abs(az - 90.0) < 1e-6  # east = bearing 90


def test_section_azimuth_due_north():
    c0 = _cell(0, 0.0, 0.0)
    c1 = _cell(6, 0.0, 5000.0)  # moved north
    results = [SimpleNamespace(cells=[c0]), SimpleNamespace(cells=[c1])]
    az = xsection.section_azimuth(c1, results, ViewerParams(xsection_azimuth_source="track"))
    assert abs(az - 0.0) < 1e-6 or abs(az - 360.0) < 1e-6


def test_section_azimuth_fixed_and_fallback():
    c = _cell(0, 0.0, 0.0)
    vp = ViewerParams(xsection_azimuth_source="fixed", xsection_fixed_bearing=135.0)
    assert xsection.section_azimuth(c, [], vp) == 135.0
    # Track source with a single point can't infer motion -> fixed fallback.
    vp2 = ViewerParams(xsection_azimuth_source="track", xsection_fixed_bearing=45.0)
    results = [SimpleNamespace(cells=[c])]
    assert xsection.section_azimuth(c, results, vp2) == 45.0


def test_extract_section_shapes_and_centre():
    z = np.array([500.0, 1000.0, 1500.0, 2000.0])
    y = np.arange(-10000.0, 10001.0, 1000.0)
    x = np.arange(-10000.0, 10001.0, 1000.0)
    refl = np.full((z.size, y.size, x.size), np.nan, dtype=np.float32)
    refl[:, y.size // 2, x.size // 2] = 50.0  # echo at the radar centre
    vol = GriddedVolume("KFWS", datetime(2024, 5, 25, 12, 0, tzinfo=timezone.utc),
                        refl, x, y, z, 32.57, -97.30)
    cell = _cell(0, 0.0, 0.0)
    sec = xsection.extract_section(vol, cell, azimuth_deg=90.0, half_len_km=10.0)
    assert sec.dbz.shape[0] == z.size
    assert sec.z_km.size == z.size
    # Distance 0 is the seed -> the centre column should see the 50 dBZ echo.
    mid = sec.dist_km.size // 2
    assert np.nanmax(sec.dbz[:, mid]) == 50.0
