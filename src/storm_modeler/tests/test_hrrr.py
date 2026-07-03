"""Unit tests for the HRRR freezing-level ingest (data.hrrr) and its model."""

from __future__ import annotations

import numpy as np

from storm_modeler.data.hrrr import (
    HRRRSource,
    freezing_level_key,
    idx_byte_range,
    lcc_forward,
)
from storm_modeler.models import FreezingLevelGrid

# HRRR CONUS grid definition (what the GRIB carries).
HRRR_LON0, HRRR_LAT0 = -97.5, 38.5
HRRR_LAT1 = HRRR_LAT2 = 38.5
HRRR_RADIUS = 6371229.0

_IDX = """\
145:94012901:d=2024052500:TMP:surface:anl:
146:95012901:d=2024052500:HGT:0C isotherm:anl:
147:96543210:d=2024052500:RH:0C isotherm:anl:
148:97000000:d=2024052500:PRES:80 m above ground:anl:
"""


def test_idx_byte_range_finds_field_and_span():
    span = idx_byte_range(_IDX)
    assert span == (95012901, 96543209)


def test_idx_byte_range_last_message_open_ended():
    text = "1:0:d=2024052500:HGT:0C isotherm:anl:\n"
    assert idx_byte_range(text) == (0, None)


def test_idx_byte_range_missing_field():
    assert idx_byte_range("1:0:d=2024052500:TMP:surface:anl:\n") is None


def test_freezing_level_key():
    from datetime import datetime, timezone

    run = datetime(2024, 5, 25, 17, tzinfo=timezone.utc)
    assert freezing_level_key(run) == "hrrr.20240525/conus/hrrr.t17z.wrfsfcf00.grib2"


def test_hourly_runs_span_window():
    from datetime import datetime, timezone

    src = HRRRSource(
        datetime(2024, 5, 25, 16, 41, tzinfo=timezone.utc),
        datetime(2024, 5, 25, 18, 45, tzinfo=timezone.utc),
        bbox=(-98.0, 32.0, -96.0, 34.0),
    )
    runs = src.runs()
    assert [r.hour for r in runs] == [16, 17, 18]
    assert src.estimated_count() == 3


def test_lcc_forward_matches_pyproj():
    """The pure-NumPy LCC forward projection must match PROJ on the HRRR sphere."""
    from pyproj import Transformer

    from storm_modeler.models import PROJ_LOCK

    lcc = (
        f"+proj=lcc +lat_1={HRRR_LAT1} +lat_2={HRRR_LAT2} +lat_0={HRRR_LAT0} "
        f"+lon_0={HRRR_LON0} +R={HRRR_RADIUS} +x_0=0 +y_0=0 +units=m +no_defs"
    )
    with PROJ_LOCK:
        t = Transformer.from_crs("EPSG:4326", lcc, always_xy=True)
    # Sample points across CONUS, including the HRRR grid's SW corner.
    lons = np.array([-122.72, -97.5, -75.0, -105.3, -84.9])
    lats = np.array([21.138, 38.5, 45.0, 31.9, 40.4])
    with PROJ_LOCK:
        x_ref, y_ref = t.transform(lons, lats)
    x, y = lcc_forward(lons, lats, HRRR_LON0, HRRR_LAT0, HRRR_LAT1, HRRR_LAT2,
                       HRRR_RADIUS)
    assert np.allclose(x, x_ref, atol=1.0)  # sub-metre over CONUS
    assert np.allclose(y, y_ref, atol=1.0)


def test_freezing_grid_height_at_and_roundtrip(tmp_path):
    lons = np.arange(-98.0, -96.0 + 0.015, 0.03)
    lats = np.arange(34.0, 32.0 - 0.015, -0.03)
    heights = np.linspace(3500.0, 4500.0, lats.size)[:, None] * np.ones(lons.size)
    grid = FreezingLevelGrid(
        model="HRRR", valid_time="2024-05-25T17:00:00Z",
        heights_m=heights, lons=lons, lats=lats, bbox=(-98.0, 32.0, -96.0, 34.0),
    )
    # Row 0 is north (3500 m), the last row south (4500 m).
    assert abs(grid.height_at(-97.0, 34.0) - 3500.0) < 30.0
    assert abs(grid.height_at(-97.0, 32.0) - 4500.0) < 30.0
    assert np.isnan(grid.height_at(-90.0, 33.0))  # off the raster

    p = tmp_path / "fl.npz"
    grid.save_npz(p)
    back = FreezingLevelGrid.load_npz(p)
    assert back.model == "HRRR"
    assert back.valid_time == grid.valid_time
    assert np.array_equal(back.heights_m, grid.heights_m)
    assert back.bbox == grid.bbox


def test_grid_from_message_resamples_synthetic_lcc_field():
    """A synthetic LCC-gridded field resamples to lon/lat within interpolation
    error — exercised through a stand-in pygrib message object."""
    from storm_modeler.data.hrrr import grid_from_message

    # A small 3-km LCC grid around KFWS. Choose the first grid point by
    # inverse-projecting a known LCC coordinate: instead, pick a lon/lat SW
    # corner and let the grid run NE from it (jScansPositively, like HRRR).
    lon_sw, lat_sw = -99.5, 31.5
    nx = ny = 120
    dx = dy = 3000.0
    x_sw, y_sw = lcc_forward(
        np.float64(lon_sw), np.float64(lat_sw),
        HRRR_LON0, HRRR_LAT0, HRRR_LAT1, HRRR_LAT2, HRRR_RADIUS,
    )
    xs = float(x_sw) + np.arange(nx) * dx
    ys = float(y_sw) + np.arange(ny) * dy
    XX, YY = np.meshgrid(xs, ys)
    # Field linear in LCC space: exactly recoverable by bilinear resampling.
    field = 4000.0 + 1e-4 * (XX - XX.min()) + 2e-4 * (YY - YY.min())

    class FakeMessage:
        values = field
        projparams = {
            "proj": "lcc", "lon_0": HRRR_LON0 + 360.0, "lat_0": HRRR_LAT0,
            "lat_1": HRRR_LAT1, "lat_2": HRRR_LAT2,
            "a": HRRR_RADIUS, "b": HRRR_RADIUS,
        }
        _keys = {
            "longitudeOfFirstGridPointInDegrees": lon_sw + 360.0,
            "latitudeOfFirstGridPointInDegrees": lat_sw,
            "DxInMetres": dx, "DyInMetres": dy,
            "jScansPositively": 1,
        }

        def __getitem__(self, k):
            return self._keys[k]

    bbox = (-99.0, 32.0, -98.0, 33.0)  # comfortably inside the LCC patch
    grid = grid_from_message(FakeMessage(), "2024-05-25T17:00:00Z", bbox, 0.03)
    assert grid.shape == (grid.lats.size, grid.lons.size)
    assert np.isfinite(grid.heights_m).all()

    # Cross-check a few raster nodes against the analytic field.
    for lon, lat in [(-98.9, 32.1), (-98.5, 32.5), (-98.1, 32.9)]:
        xq, yq = lcc_forward(
            np.float64(lon), np.float64(lat),
            HRRR_LON0, HRRR_LAT0, HRRR_LAT1, HRRR_LAT2, HRRR_RADIUS,
        )
        expected = 4000.0 + 1e-4 * (float(xq) - XX.min()) + 2e-4 * (float(yq) - YY.min())
        assert abs(grid.height_at(lon, lat) - expected) < 2.0
