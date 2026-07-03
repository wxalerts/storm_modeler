"""Unit tests for vault detection (detection.vault) — tower vs 0 °C level."""

from __future__ import annotations

import numpy as np

from storm_modeler.data.sites import get_site
from storm_modeler.data.synthetic import make_freezing_grid, make_storm_volume
from storm_modeler.detection import detection_v2 as scit
from storm_modeler.detection.vault import detect_vault
from storm_modeler.pipeline import annotate_vault_results
from storm_modeler.settings.resolver import VaultParams

KFWS = get_site("KFWS")
BBOX = (KFWS.lon - 2.0, KFWS.lat - 2.0, KFWS.lon + 2.0, KFWS.lat + 2.0)
T = "2024-05-25T17:42:00Z"


def _volume(**kw):
    return make_storm_volume(
        "KFWS", KFWS.lat, KFWS.lon, T,
        core_lon=KFWS.lon + 0.1, core_lat=KFWS.lat + 0.1, **kw,
    )


def _cells(volume):
    cells = scit.run(volume)
    assert cells, "synthetic storm must admit"
    return cells


def test_vault_flags_ot_when_tower_punches_above_freezing_level():
    # peak 57 dBZ at 1.5 km declining to 18.3 dBZ at 10.5 km → the 40 dBZ
    # tower reaches ~5.4 km AGL (~5.6 km MSL at KFWS).
    vol = _volume()
    cells = _cells(vol)
    fl = make_freezing_grid(T, BBOX, level_m=3000.0)  # 3 km MSL
    params = VaultParams(vault_dbz=40.0, vault_min_depth_km=1.5)
    detect_vault(vol, cells, fl, KFWS.elevation_m, params)
    c = cells[0]
    assert c.freezing_level_km is not None and abs(c.freezing_level_km - 3.0) < 1e-6
    assert c.vault_top_km is not None and c.vault_top_km > 4.5
    assert c.vault_depth_km is not None and c.vault_depth_km >= 1.5
    assert c.overshooting_top is True


def test_no_ot_when_freezing_level_is_above_the_tower():
    vol = _volume()
    cells = _cells(vol)
    fl = make_freezing_grid(T, BBOX, level_m=5500.0)  # tower top sits below it
    params = VaultParams(vault_dbz=40.0, vault_min_depth_km=1.5)
    detect_vault(vol, cells, fl, KFWS.elevation_m, params)
    c = cells[0]
    assert c.freezing_level_km is not None
    assert c.vault_top_km is not None
    assert c.vault_depth_km is not None and c.vault_depth_km < 1.5
    assert c.overshooting_top is False


def test_no_tower_at_threshold_leaves_vault_unset_but_level_annotated():
    vol = _volume(peak_dbz=45.0)  # never reaches a 60 dBZ vault threshold
    cells = _cells(vol)
    fl = make_freezing_grid(T, BBOX, level_m=3000.0)
    detect_vault(vol, cells, fl, KFWS.elevation_m,
                 VaultParams(vault_dbz=60.0, vault_min_depth_km=1.5))
    c = cells[0]
    assert c.freezing_level_km is not None
    assert c.vault_top_km is None
    assert c.vault_depth_km is None
    assert c.overshooting_top is False


def test_seed_off_raster_falls_back_to_grid_mean():
    vol = _volume()
    cells = _cells(vol)
    # A raster that does NOT cover the storm (far east), so height_at is NaN.
    far = (KFWS.lon + 5.0, KFWS.lat - 1.0, KFWS.lon + 7.0, KFWS.lat + 1.0)
    fl = make_freezing_grid(T, far, level_m=3200.0)
    detect_vault(vol, cells, fl, KFWS.elevation_m,
                 VaultParams(vault_dbz=40.0, vault_min_depth_km=1.5))
    assert abs(cells[0].freezing_level_km - 3.2) < 1e-6


def test_determinism():
    vol = _volume()
    fl = make_freezing_grid(T, BBOX, level_m=3000.0)
    params = VaultParams(vault_dbz=40.0, vault_min_depth_km=1.5)
    a = detect_vault(vol, _cells(vol), fl, KFWS.elevation_m, params)
    b = detect_vault(vol, _cells(vol), fl, KFWS.elevation_m, params)
    assert [c.to_dict() for c in a] == [c.to_dict() for c in b]


def test_annotate_vault_results_pairs_nearest_grid_in_time():
    from storm_modeler.pipeline import VolumeResult

    vol_a = _volume()
    vol_b = make_storm_volume(
        "KFWS", KFWS.lat, KFWS.lon, "2024-05-25T18:42:00Z",
        core_lon=KFWS.lon + 0.1, core_lat=KFWS.lat + 0.1,
    )
    results = [
        VolumeResult(warning=None, site=KFWS, volume=vol_a, cells=_cells(vol_a)),
        VolumeResult(warning=None, site=KFWS, volume=vol_b, cells=_cells(vol_b)),
    ]
    grids = [
        make_freezing_grid("2024-05-25T17:00:00Z", BBOX, level_m=3000.0),
        make_freezing_grid("2024-05-25T19:00:00Z", BBOX, level_m=4200.0),
    ]
    annotate_vault_results(results, grids, VaultParams(vault_dbz=40.0,
                                                       vault_min_depth_km=1.5))
    # 17:42Z volume pairs with the 17Z grid; 18:42Z with the 19Z grid.
    assert abs(results[0].cells[0].freezing_level_km - 3.0) < 1e-6
    assert abs(results[1].cells[0].freezing_level_km - 4.2) < 1e-6


def test_annotate_without_grids_is_a_noop():
    vol = _volume()
    results_cells = _cells(vol)
    from storm_modeler.pipeline import VolumeResult

    res = VolumeResult(warning=None, site=KFWS, volume=vol, cells=results_cells)
    annotate_vault_results([res], [], VaultParams())
    assert results_cells[0].freezing_level_km is None
    assert results_cells[0].overshooting_top is False
