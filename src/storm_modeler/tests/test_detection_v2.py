"""Unit tests for the SCIT package (detection_v2)."""

from __future__ import annotations

import numpy as np

from storm_modeler.models import GriddedVolume
from storm_modeler.settings.resolver import DetectionParams
from storm_modeler.data.sites import get_site
from storm_modeler.data.synthetic import make_ap_volume, make_storm_volume
from storm_modeler.detection import detection_v2 as scit


KFWS = get_site("KFWS")
KHGX = get_site("KHGX")


def _storm(t, dlon=0.15, dlat=0.05, **kw):
    return make_storm_volume(
        "KFWS", KFWS.lat, KFWS.lon, t, KFWS.lon + dlon, KFWS.lat + dlat, **kw
    )


def _multicell(t, offsets, **kw):
    """One volume with several convective cores, max-combined onto one grid.

    Cores are spaced close enough to merge into a single base-reflectivity
    (30 dBZ) blob but keep distinct >=40 dBZ peaks — the merged multi-core
    system SCIT must resolve into separate cells.
    """
    vols = [_storm(t, dlon=dx, dlat=dy, **kw) for dx, dy in offsets]
    stacked = np.stack(
        [np.nan_to_num(v.reflectivity, nan=-9999.0) for v in vols]
    )
    refl = stacked.max(axis=0)
    refl = np.where(refl >= 5.0, refl, np.nan).astype(np.float32)
    b = vols[0]
    return GriddedVolume(
        site=b.site, valid_time=b.valid_time, reflectivity=refl,
        x=b.x, y=b.y, z=b.z, lat0=b.lat0, lon0=b.lon0,
    )


def test_storm_admits_one_deep_cell():
    cells = scit.run(_storm("2024-05-25T17:42:00Z", echo_top_km=10.0))
    assert len(cells) == 1
    c = cells[0]
    assert c.max_dbz > 50
    assert c.depth_km > 6.0
    assert c.echo_top_km > 6.0
    assert c.n_levels >= DetectionParams().continuity_levels
    assert c.area_km2 >= DetectionParams().min_area_km2
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


# Five wide cores spaced ~9 km apart: their >=40 dBZ discs overlap into ONE
# seed blob (so connected-component labelling alone yields a single cell), but
# the peaks stay distinct — only the watershed split can separate them.
_MERGED_OFFSETS = [(0.05 + 0.10 * i, 0.05) for i in range(5)]


def test_split_off_keeps_merged_system_whole():
    """With splitting disabled the bridged system stays a single merged cell."""
    vol = _multicell("2024-05-25T17:42:00Z", _MERGED_OFFSETS, echo_top_km=10.0,
                     radius_km=12.0)
    cells = scit.run(vol, DetectionParams(watershed_split=False))
    assert len(cells) == 1


def test_merged_system_splits_into_separate_cells():
    """The same system resolves into distinct cells with disjoint envelopes
    once watershed splitting is on — not N copies of the whole footprint."""
    vol = _multicell("2024-05-25T17:42:00Z", _MERGED_OFFSETS, echo_top_km=10.0,
                     radius_km=12.0)

    # Peak separation well below the ~9 km core spacing so each core seeds.
    cells = scit.run(
        vol, DetectionParams(watershed_split=True, watershed_min_sep_km=3.0)
    )
    assert len(cells) == len(_MERGED_OFFSETS)

    # Footprints are partitioned, not shared: no single cell covers the whole
    # system, and total cell area does not blow up to N x the full blob.
    total = sum(c.area_km2 for c in cells)
    biggest = max(c.area_km2 for c in cells)
    assert biggest < total  # the largest cell is a fraction of the whole

    # Distinct seed centroids (one per core).
    xs = sorted(round(c.seed_x, -2) for c in cells)
    assert len(set(xs)) == len(_MERGED_OFFSETS)


def test_watershed_peak_separation_controls_cell_count():
    """A larger ``watershed_min_sep_km`` merges nearby cores → fewer cells."""
    vol = _multicell("2024-05-25T17:42:00Z", _MERGED_OFFSETS, echo_top_km=10.0,
                     radius_km=12.0)
    fine = scit.run(vol, DetectionParams(watershed_min_sep_km=3.0))
    coarse = scit.run(vol, DetectionParams(watershed_min_sep_km=20.0))
    assert len(coarse) < len(fine)


def test_determinism():
    a = scit.run(_storm("2024-05-25T17:42:00Z", echo_top_km=10.0))
    b = scit.run(_storm("2024-05-25T17:42:00Z", echo_top_km=10.0))
    assert [c.to_dict() for c in a] == [c.to_dict() for c in b]
