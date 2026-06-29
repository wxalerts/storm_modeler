"""GOES GLM lightning ingest — hermetic (no network).

Filename parsing and satellite/bucket selection are pure. The NetCDF parse is
exercised against a synthetic LCFA file built on the fly (no fixture ships), so
the bbox / time-window / quality filters are proven without any S3 access.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from storm_modeler.data.lightning import (
    GLM_EPOCH,
    Flash,
    bin_nearest,
    bucket_for_date,
    parse_glm_lcfa,
)
from storm_modeler.data import lightning as lgm


def _flash(minute, second=0):
    return Flash(32.5, -97.2,
                 datetime(2024, 5, 6, 20, minute, second, tzinfo=timezone.utc), 1e-14)


def test_bin_nearest_assigns_each_flash_to_closest_volume():
    # Volumes every 6 minutes; boundaries fall on the 3-minute midpoints.
    vols = [datetime(2024, 5, 6, 20, m, tzinfo=timezone.utc) for m in (0, 6, 12)]
    flashes = [_flash(1), _flash(4), _flash(7), _flash(11), _flash(20)]

    b0 = bin_nearest(flashes, vols[0], vols)   # (-inf, 3]
    b1 = bin_nearest(flashes, vols[1], vols)   # (3, 9]
    b2 = bin_nearest(flashes, vols[2], vols)   # (9, +inf)

    assert [f.time.minute for f in b0] == [1]
    assert [f.time.minute for f in b1] == [4, 7]
    assert [f.time.minute for f in b2] == [11, 20]
    # Partition: every flash lands in exactly one bin.
    assert len(b0) + len(b1) + len(b2) == len(flashes)


def test_bin_nearest_single_volume_keeps_all():
    v = datetime(2024, 5, 6, 20, 6, tzinfo=timezone.utc)
    flashes = [_flash(1), _flash(30)]
    assert bin_nearest(flashes, v, [v]) == flashes
    assert bin_nearest([], v, [v]) == []


def test_bucket_for_date_switches_at_goes19():
    assert bucket_for_date(date(2024, 5, 1))[0] == "noaa-goes16"
    assert bucket_for_date(date(2025, 4, 3))[0] == "noaa-goes16"
    assert bucket_for_date(date(2025, 4, 4))[0] == "noaa-goes19"
    assert bucket_for_date(date(2026, 1, 1))[0] == "noaa-goes19"


def test_filename_time_parsing():
    name = "OR_GLM-L2-LCFA_G16_s20241270000000_e20241270000200_c20241270000220.nc"
    t = lgm._name_time(name)
    # 2024 day-of-year 127 = 2024-05-06, 00:00:00 UTC.
    assert t == datetime(2024, 5, 6, 0, 0, 0, tzinfo=timezone.utc)
    assert lgm._name_time("not-a-glm-file.nc") is None


def _write_lcfa(path, product_time_dt, flashes):
    """Write a minimal LCFA NetCDF: (lat, lon, offset_s, energy, qflag) rows."""
    netCDF4 = pytest.importorskip("netCDF4")
    ds = netCDF4.Dataset(str(path), "w")
    try:
        ds.createDimension("number_of_flashes", len(flashes))
        pt = ds.createVariable("product_time", "f8")
        pt[...] = product_time_dt.timestamp() - GLM_EPOCH.timestamp()
        for var, idx in (("flash_lat", 0), ("flash_lon", 1),
                         ("flash_time_offset_of_first_event", 2),
                         ("flash_energy", 3), ("flash_quality_flag", 4)):
            v = ds.createVariable(var, "f8", ("number_of_flashes",))
            v[:] = [row[idx] for row in flashes]
    finally:
        ds.close()


def test_parse_filters_bbox_time_and_quality(tmp_path):
    pytest.importorskip("netCDF4")
    pt = datetime(2024, 5, 6, 20, 0, 0, tzinfo=timezone.utc)
    # (lat, lon, offset_s, energy, qflag)
    rows = [
        (32.5, -97.2, 10.0, 1e-14, 0),   # inside bbox, in window, good   -> keep
        (32.6, -97.1, 20.0, 2e-14, 0),   # inside bbox, in window, good   -> keep
        (45.0, -97.2, 10.0, 1e-14, 0),   # outside bbox (lat)             -> drop
        (32.5, -97.2, 10.0, 1e-14, 5),   # bad quality flag               -> drop
        (32.5, -97.2, 9000.0, 1e-14, 0), # past the window end            -> drop
    ]
    path = tmp_path / "lcfa.nc"
    _write_lcfa(path, pt, rows)
    content = path.read_bytes()

    bbox = (32.0, -97.5, 33.0, -97.0)
    t0 = datetime(2024, 5, 6, 19, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2024, 5, 6, 20, 30, 0, tzinfo=timezone.utc)
    out = parse_glm_lcfa(content, bbox, t0, t1, good_only=True)

    assert len(out) == 2
    assert all(isinstance(f, Flash) for f in out)
    assert all(32.0 <= f.lat <= 33.0 and -97.5 <= f.lon <= -97.0 for f in out)
    # offset is seconds after product_time -> exact UTC reconstruction.
    assert out[0].time == datetime(2024, 5, 6, 20, 0, 10, tzinfo=timezone.utc)


def test_parse_good_only_false_keeps_flagged(tmp_path):
    pytest.importorskip("netCDF4")
    pt = datetime(2024, 5, 6, 20, 0, 0, tzinfo=timezone.utc)
    rows = [(32.5, -97.2, 10.0, 1e-14, 5)]  # flagged
    path = tmp_path / "lcfa.nc"
    _write_lcfa(path, pt, rows)
    content = path.read_bytes()
    bbox = (32.0, -97.5, 33.0, -97.0)
    assert parse_glm_lcfa(content, bbox, good_only=True) == []
    assert len(parse_glm_lcfa(content, bbox, good_only=False)) == 1
