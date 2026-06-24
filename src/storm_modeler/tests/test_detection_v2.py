"""Unit tests for the SCIT package (detection_v2)."""

from __future__ import annotations

import numpy as np

from storm_modeler.config import ScitConfig
from storm_modeler.data.sites import get_site
from storm_modeler.data.synthetic import make_ap_volume, make_storm_volume
from storm_modeler.detection import detection_v2 as scit


KFWS = get_site("KFWS")
KHGX = get_site("KHGX")


def _storm(t, dlon=0.15, dlat=0.05, **kw):
    return make_storm_volume(
        "KFWS", KFWS.lat, KFWS.lon, t, KFWS.lon + dlon, KFWS.lat + dlat, **kw
    )


def test_storm_admits_one_deep_cell():
    cells = scit.run(_storm("2024-05-25T17:42:00Z", echo_top_km=10.0))
    assert len(cells) == 1
    c = cells[0]
    assert c.max_dbz > 50
    assert c.depth_km > 6.0
    assert c.echo_top_km > 6.0
    assert c.n_levels >= ScitConfig().min_levels
    assert c.area_km2 >= ScitConfig().min_area_km2
    assert c.envelope.is_valid and c.envelope.area > 0


def test_ap_admits_zero_cells():
    """The Phase-1 guarantee: anomalous propagation never seeds."""
    ap = make_ap_volume(
        "KHGX", KHGX.lat, KHGX.lon, "2026-06-24T11:41:00Z", -95.0, 29.6
    )
    assert scit.run(ap) == []


def test_ap_has_strong_low_level_return():
    """Sanity: the AP case really is strong at the surface (so rejection is
    structural, not just a weak-echo artefact)."""
    ap = make_ap_volume(
        "KHGX", KHGX.lat, KHGX.lon, "2026-06-24T11:41:00Z", -95.0, 29.6
    )
    surface = ap.reflectivity[0]
    assert np.nanmax(surface) >= 40.0
    # nothing above the lowest level
    assert np.all(np.isnan(ap.reflectivity[1:]))


def test_tracking_is_stable_across_volumes():
    tr = scit.Tracker()
    c1 = tr.update(scit.run(_storm("2024-05-25T17:42:00Z", echo_top_km=10.0)), "2024-05-25T17:42:00Z")
    c2 = tr.update(
        scit.run(_storm("2024-05-25T17:48:00Z", dlon=0.20, dlat=0.09, echo_top_km=10.0)),
        "2024-05-25T17:48:00Z",
    )
    assert len(c1) == 1 and len(c2) == 1
    assert c1[0].track_id == c2[0].track_id


def test_determinism():
    a = scit.run(_storm("2024-05-25T17:42:00Z", echo_top_km=10.0))
    b = scit.run(_storm("2024-05-25T17:42:00Z", echo_top_km=10.0))
    assert [c.to_dict() for c in a] == [c.to_dict() for c in b]
