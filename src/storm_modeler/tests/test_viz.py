"""Pure-logic tests for the Phase B viz layer (no GL / no Qt).

The GL rendering itself is exercised by the off-screen ``render_cell`` tool and
the ``--smoke`` build (Section 8); here we lock in the deterministic pieces: the
LRU grid cache, the viewer-settings projection, and the scene coordinate math.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from storm_modeler.models import GriddedVolume
from storm_modeler.settings.registry import REGISTRY_BY_KEY
from storm_modeler.settings.resolver import ViewerParams, resolve_from_values
from storm_modeler.viz.grid_provider import GridProvider


def _vol(minute: int) -> GriddedVolume:
    t = datetime(2024, 5, 25, 12, minute, tzinfo=timezone.utc)
    z = np.array([500.0, 1000.0, 1500.0])
    y = np.array([-1000.0, 0.0, 1000.0])
    x = np.array([-1000.0, 0.0, 1000.0])
    refl = np.full((3, 3, 3), np.nan, dtype=np.float32)
    refl[0, 1, 1] = 50.0
    return GriddedVolume("KFWS", t, refl, x, y, z, 32.57, -97.30)


# --- GridProvider -----------------------------------------------------------

def test_grid_provider_serves_registered_grids_in_order():
    p = GridProvider(cache_size=8)
    vols = [_vol(m) for m in (12, 6, 0)]  # out of order
    p.register_all(vols)
    times = p.times()
    assert times == sorted(times)  # oldest -> newest
    assert len(p) == 3
    assert p.get_index(0).valid_time.minute == 0
    assert p.index_of(vols[0].valid_time) == 2  # minute 12 is last


def test_grid_provider_lru_evicts_and_refetches():
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return [_vol(0), _vol(6), _vol(12)]

    p = GridProvider(source_factory=factory, cache_size=2)
    p.register_all(factory())  # warms cache (counts as one call)
    # Cache holds at most 2; the oldest was evicted. Re-getting it re-grids.
    evicted = datetime(2024, 5, 25, 12, 0, tzinfo=timezone.utc)
    before = calls["n"]
    got = p.get(evicted)
    assert got.valid_time == evicted
    assert calls["n"] > before  # had to re-fetch via the factory


def test_grid_provider_get_missing_without_factory_raises():
    p = GridProvider(cache_size=1)
    p.register(_vol(0))
    p.register(_vol(6))  # evicts minute 0
    with pytest.raises(KeyError):
        p.get(datetime(2024, 5, 25, 12, 0, tzinfo=timezone.utc))


# --- ViewerParams -----------------------------------------------------------

def test_viewer_params_parse_levels_from_string():
    vp = ViewerParams.from_values({"iso_levels_dbz": "50, 40 ; 40"})
    assert vp.iso_levels_dbz == (40.0, 50.0)  # sorted, de-duped


def test_viewer_params_defaults_and_resolver_projection():
    rs = resolve_from_values({"vert_exag": 3.0, "grid_cache_size": 12})
    vp = rs.viewer
    assert vp.vert_exag == 3.0
    assert vp.grid_cache_size == 12
    assert vp.iso_levels_dbz == (40.0, 50.0)  # registry default


def test_every_viewer_key_is_registered():
    for key in (
        "vol_floor_dbz", "iso_levels_dbz", "vert_exag",
        "xsection_azimuth_source", "xsection_fixed_bearing", "grid_cache_size",
    ):
        assert key in REGISTRY_BY_KEY
