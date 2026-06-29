"""Unit tests for the GOES ABI cloud-top package (detection.cloudtop)."""

from __future__ import annotations

import numpy as np

from storm_modeler.settings.resolver import CloudTopParams
from storm_modeler.data.sites import get_site
from storm_modeler.data.synthetic import make_cold_scene, make_storm_volume
from storm_modeler.detection import cloudtop as ct
from storm_modeler.detection import detection_v2 as scit

KFWS = get_site("KFWS")
# A bbox a little wider than the anvil so the scene has warm margins.
BBOX = (KFWS.lon - 0.6, KFWS.lat - 0.6, KFWS.lon + 0.6, KFWS.lat + 0.6)


def _scene(t="2024-05-25T17:42:00Z", dlon=0.0, dlat=0.0, **kw):
    return make_cold_scene(
        "GOES-19", t, BBOX, KFWS.lon + dlon, KFWS.lat + dlat, **kw
    )


def test_identify_one_cold_object():
    cells = ct.run(_scene())
    assert len(cells) == 1
    c = cells[0]
    # Coldest pixel sits at the synthetic core.
    assert abs(c.cold_lon - KFWS.lon) < 0.05
    assert abs(c.cold_lat - KFWS.lat) < 0.05
    assert c.min_bt_k < 205.0
    assert c.area_km2 >= CloudTopParams().min_area_km2
    assert c.envelope.is_valid and c.envelope.area > 0


def test_overshooting_top_flag_true_and_false():
    # Sharp cold core well below anvil -> OT.
    hot = ct.run(_scene(core_bt_k=196.0, anvil_bt_k=216.0))[0]
    assert hot.overshooting_top is True
    assert hot.ot_depth_k >= CloudTopParams().ot_delta_k

    # Core barely colder than anvil -> no OT (depth below threshold).
    flat = ct.run(_scene(core_bt_k=214.0, anvil_bt_k=215.0))[0]
    assert flat.overshooting_top is False


def test_anviled_out_flag():
    big = ct.run(_scene(anvil_radius_km=60.0))[0]
    assert big.anviled_out is True
    assert big.area_km2 >= CloudTopParams().anvil_area_km2


def test_no_cold_cloud_admits_zero():
    # An all-warm scene (no pixel reaches the cold threshold).
    warm = make_cold_scene(
        "GOES-19", "2024-05-25T17:42:00Z", BBOX, KFWS.lon, KFWS.lat,
        anvil_bt_k=260.0, core_bt_k=255.0, warm_bt_k=290.0,
    )
    assert ct.run(warm) == []


def test_tracking_is_stable_across_scenes():
    tr = ct.Tracker()
    c1 = tr.update(ct.run(_scene("2024-05-25T17:42:00Z")), "2024-05-25T17:42:00Z")
    # Nudge the core a few km NE between scenes.
    c2 = tr.update(
        ct.run(_scene("2024-05-25T17:47:00Z", dlon=0.04, dlat=0.03)),
        "2024-05-25T17:47:00Z",
    )
    assert len(c1) == 1 and len(c2) == 1
    assert c1[0].track_id == c2[0].track_id


def test_radar_association_tilt():
    cells = ct.run(_scene())
    # A radar cell offset ~0.05 deg east of the cold core (~5 km at this lat).
    radar = scit.run(
        make_storm_volume(
            "KFWS", KFWS.lat, KFWS.lon, "2024-05-25T17:42:00Z",
            core_lon=KFWS.lon + 0.05, core_lat=KFWS.lat, echo_top_km=10.0,
        )
    )
    assert len(radar) == 1
    ct.associate_radar(cells, radar, CloudTopParams())
    c = cells[0]
    assert c.radar_track_id == radar[0].track_id
    # Tilt is the residual offset after parallax correction (a few to tens of km).
    assert 0.0 < c.tilt_km < 40.0
    assert np.isfinite(c.tilt_bearing_deg)
    # The radar cell is annotated with cloud-top temp (deg C) + OT for the listing.
    assert radar[0].cloud_top_c is not None
    assert abs(radar[0].cloud_top_c - (c.min_bt_k - 273.15)) < 1e-6
    assert radar[0].overshooting_top == c.overshooting_top

def test_ot_attributed_to_nearest_core_only():
    cells = ct.run(_scene())
    assert cells[0].overshooting_top  # scene fixture has an OT
    near = scit.run(make_storm_volume("KFWS", KFWS.lat, KFWS.lon,
        "2024-05-25T17:42:00Z", core_lon=cells[0].cold_lon + 0.03,
        core_lat=cells[0].cold_lat, echo_top_km=12.0))[0]
    far = scit.run(make_storm_volume("KFWS", KFWS.lat, KFWS.lon,
        "2024-05-25T17:42:00Z", core_lon=cells[0].cold_lon + 0.20,
        core_lat=cells[0].cold_lat, echo_top_km=10.0))[0]
    ct.associate_radar(cells, [near, far], CloudTopParams())
    assert near.overshooting_top and not far.overshooting_top
    assert near.cloud_top_c is not None and far.cloud_top_c is not None  # temp still shared
    
def test_radar_association_skips_distant_cell():
    cells = ct.run(_scene())
    # A radar cell far outside assoc_max_km.
    radar = scit.run(
        make_storm_volume(
            "KFWS", KFWS.lat, KFWS.lon, "2024-05-25T17:42:00Z",
            core_lon=KFWS.lon + 1.0, core_lat=KFWS.lat - 1.0, echo_top_km=10.0,
        )
    )
    ct.associate_radar(cells, radar, CloudTopParams())
    assert cells[0].radar_track_id == -1
    assert np.isnan(cells[0].tilt_km)


def test_parallax_shifts_toward_subsatellite():
    """A tall cloud top corrects toward the sub-satellite point, by ~10-30 km."""
    from storm_modeler.detection.cloudtop.geo import haversine_km
    from storm_modeler.detection.cloudtop.parallax import correct, subsat_lon

    sat_lon = subsat_lon("GOES-19")  # -75.2
    lon_a, lat_a = -97.5, 36.0  # an OUN-area apparent cloud top
    lon_t, lat_t = correct(lon_a, lat_a, 13000.0, sat_lon)  # 13 km top
    # True ground is east (toward the sub-satellite longitude) of the apparent.
    assert lon_t > lon_a
    shift = haversine_km(lon_a, lat_a, lon_t, lat_t)
    assert 8.0 < shift < 35.0
    # Zero height = no shift.
    lon0, lat0 = correct(lon_a, lat_a, 0.0, sat_lon)
    assert abs(lon0 - lon_a) < 1e-6 and abs(lat0 - lat_a) < 1e-6


def test_determinism():
    a = ct.run(_scene())
    b = ct.run(_scene())
    assert [c.to_dict() for c in a] == [c.to_dict() for c in b]
